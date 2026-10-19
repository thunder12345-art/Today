import argparse
import base64
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

from common import ROOT, setup_stdout

CURL = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "curl.exe")
UPSTREAM_HOST = "github.com"
FORWARD_HEADERS = ("User-Agent", "Accept", "Accept-Encoding", "Content-Type", "Pragma", "Git-Protocol")
KEEP_RESPONSE_HEADERS = ("content-type", "cache-control", "expires", "pragma")


class RelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    auth_header = ""
    temp_dir = ""
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            print("[relay] " + (fmt % args))

    def _read_body(self):
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            chunks = []
            while True:
                size_line = self.rfile.readline().split(b";")[0].strip()
                if not size_line:
                    break
                size = int(size_line, 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _forward(self, method, body):
        target = "https://%s%s" % (UPSTREAM_HOST, self.path)
        body_path = os.path.join(self.temp_dir, "body.bin")
        resp_path = os.path.join(self.temp_dir, "resp.bin")
        hdr_path = os.path.join(self.temp_dir, "resp.hdr")
        with open(body_path, "wb") as handle:
            handle.write(body)
        args = [CURL, "-sS", "--http1.1", "--max-time", "600", "-X", method,
                "-D", hdr_path, "-o", resp_path, "-H", self.auth_header]
        for key in FORWARD_HEADERS:
            value = self.headers.get(key)
            if value:
                args += ["-H", "%s: %s" % (key, value)]
        if body:
            args += ["--data-binary", "@" + body_path]
        args.append(target)
        print("[relay] %s %s (%d bytes up)" % (method, self.path.split("?")[0], len(body)))
        proc = subprocess.run(args, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError("curl exit %d: %s" % (proc.returncode, proc.stderr.decode("utf-8", "replace")[:300]))
        with open(hdr_path, "rb") as handle:
            blocks = handle.read().split(b"\r\n\r\n")
        header_block = blocks[-2] if len(blocks) > 1 else blocks[-1]
        lines = [line for line in header_block.split(b"\r\n") if line]
        status_line = lines[0].decode("latin-1") if lines else "HTTP/1.1 502 Relay"
        parts = status_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) > 1 else 502
        out_headers = []
        for line in lines[1:]:
            if b":" not in line:
                continue
            name = line.split(b":", 1)[0].strip().lower().decode("latin-1")
            if name in KEEP_RESPONSE_HEADERS:
                out_headers.append(line.decode("latin-1"))
        with open(resp_path, "rb") as handle:
            response_body = handle.read()
        print("[relay] upstream %d (%d bytes down)" % (status_code, len(response_body)))
        return status_code, parts[2] if len(parts) > 2 else "", out_headers, response_body

    def _relay(self, method):
        if (self.headers.get("Expect") or "").lower() == "100-continue":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()
        body = self._read_body()
        try:
            status_code, reason, headers, response_body = self._forward(method, body)
        except Exception as error:
            self.send_error(502, str(error)[:200])
            self.close_connection = True
            return
        self.send_response_only(status_code, reason)
        for header in headers:
            name, _, value = header.partition(":")
            self.send_header(name.strip(), value.strip())
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(response_body)
        self.close_connection = True

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def do_HEAD(self):
        self._relay("HEAD")


class RelayServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def get_credentials():
    proc = subprocess.run(["git", "credential", "fill"],
                          input=b"protocol=https\nhost=github.com\n\n", capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("git credential fill failed: %s" % proc.stderr.decode("utf-8", "replace")[:200])
    data = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value
    if not data.get("password"):
        raise RuntimeError("no cached GitHub credential found (run git push over HTTPS once to cache it)")
    return data.get("username", "git"), data["password"]


def origin_slug(repo_dir):
    proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_dir, capture_output=True)
    url = proc.stdout.decode("utf-8", "replace").strip()
    match = re.search(r"github\.com[:/]+([^/]+/[^/\s]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def start_server(port, auth_header, temp_dir, verbose):
    RelayHandler.auth_header = auth_header
    RelayHandler.temp_dir = temp_dir
    RelayHandler.verbose = verbose
    last_error = None
    for candidate in range(port, port + 10):
        try:
            server = RelayServer(("127.0.0.1", candidate), RelayHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, candidate
        except OSError as error:
            last_error = error
    raise RuntimeError("cannot bind relay port: %s" % last_error)


def main():
    parser = argparse.ArgumentParser(
        description="Push to GitHub through a local relay that forwards via curl.exe (for machines where security software blocks git/ssh uploads)")
    parser.add_argument("--repo", default=ROOT, help="repository directory (default: repo root)")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--verbose", action="store_true", help="log every relayed request")
    args = parser.parse_args()
    setup_stdout()

    if not os.path.exists(CURL):
        print("curl.exe not found at %s" % CURL)
        sys.exit(1)

    slug = origin_slug(args.repo)
    if not slug:
        print("cannot detect GitHub owner/repo from origin remote")
        sys.exit(1)

    try:
        username, token = get_credentials()
    except RuntimeError as error:
        print("credential error: %s" % error)
        sys.exit(1)
    auth_header = "Authorization: Basic " + base64.b64encode(("%s:%s" % (username, token)).encode()).decode()

    temp_dir = tempfile.mkdtemp(prefix="gitrelay-")
    server, port = start_server(args.port, auth_header, temp_dir, args.verbose)
    local_url = "http://127.0.0.1:%d/%s.git" % (port, slug)
    print("[relay] listening on port %d -> https://%s/%s.git" % (port, UPSTREAM_HOST, slug))
    print("[relay] pushing %s to %s ..." % (args.branch, local_url))

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    command = ["git", "-c", "http.extraHeader=%s" % auth_header,
               "push", local_url, args.branch]
    proc = subprocess.run(command, cwd=args.repo, env=env)
    ok = proc.returncode == 0

    if ok:
        print("[relay] push ok, updating remote tracking ...")
        subprocess.run(["git", "fetch", "origin"], cwd=args.repo, env=env, capture_output=True)
        subprocess.run(["git", "branch", "--set-upstream-to=origin/%s" % args.branch, args.branch],
                       cwd=args.repo, capture_output=True)

    server.shutdown()
    shutil.rmtree(temp_dir, ignore_errors=True)
    print("[relay] done, push %s" % ("succeeded" if ok else "FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
