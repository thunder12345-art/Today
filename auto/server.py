import argparse
import json
import os
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from common import BASE, DB_PATH, REPORT_JSON, ROOT, load_json, now_iso, setup_stdout

STATE = {"running": False, "last_run": None, "last_duration": None, "last_result": None}
LOCK = threading.Lock()


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def ensure_seed():
    if not os.path.exists(DB_PATH):
        print("[init] sources.json missing, running seed.py")
        subprocess.run([sys.executable, os.path.join(BASE, "seed.py")])


def run_pipeline(scope, drop_dead, timeout, workers):
    if not LOCK.acquire(blocking=False):
        print("[pipeline] previous run still active, skip")
        return
    try:
        STATE["running"] = True
        started = time.time()
        command = [sys.executable, os.path.join(BASE, "run.py"),
                   "--scope", scope, "--timeout", str(timeout),
                   "--workers", str(workers), "--inplace"]
        if drop_dead:
            command.append("--drop-dead")
        print("[pipeline] started: %s" % " ".join(command))
        result = subprocess.run(command)
        STATE["last_run"] = now_iso()
        STATE["last_duration"] = round(time.time() - started, 1)
        STATE["last_result"] = "ok" if result.returncode == 0 else "failed (%d)" % result.returncode
        print("[pipeline] finished: %s in %.1fs" % (STATE["last_result"], STATE["last_duration"]))
    finally:
        STATE["running"] = False
        LOCK.release()


def pipeline_loop(scope, drop_dead, timeout, workers, interval_hours, run_on_start):
    if run_on_start:
        run_pipeline(scope, drop_dead, timeout, workers)
    while True:
        time.sleep(max(300.0, interval_hours * 3600.0))
        run_pipeline(scope, drop_dead, timeout, workers)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/status", "/__status"):
            self.send_json({"state": STATE, "report": load_json(REPORT_JSON, {})})
            return
        if path.startswith("/auto") or path.startswith("/.git"):
            self.send_error(404)
            return
        super().do_GET()

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description="Self-hosted subscription server with scheduled refresh")
    parser.add_argument("--port", type=int, default=env_int("PORT", 8080))
    parser.add_argument("--interval-hours", type=float, default=env_float("REFRESH_INTERVAL_HOURS", 12))
    parser.add_argument("--scope", choices=["local", "all"], default=os.environ.get("BUILD_SCOPE", "all"))
    parser.add_argument("--drop-dead", action="store_true", default=env_bool("DROP_DEAD", False))
    parser.add_argument("--timeout", type=float, default=env_float("CHECK_TIMEOUT", 8))
    parser.add_argument("--workers", type=int, default=env_int("CHECK_WORKERS", 12))
    parser.add_argument("--no-run-on-start", action="store_true", default=False)
    args = parser.parse_args()
    setup_stdout()

    ensure_seed()

    run_on_start = env_bool("RUN_ON_START", True) and not args.no_run_on_start
    worker = threading.Thread(
        target=pipeline_loop,
        args=(args.scope, args.drop_dead, args.timeout, args.workers,
              max(0.1, args.interval_hours), run_on_start),
        daemon=True)
    worker.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), partial(Handler, directory=ROOT))
    print("[server] serving %s at http://0.0.0.0:%d" % (ROOT, args.port))
    print("[server] tvbox.json:  http://<host>:%d/tvbox.json" % args.port)
    print("[server] lmw.json:    http://<host>:%d/lmw.json" % args.port)
    print("[server] status:      http://<host>:%d/status" % args.port)
    print("[server] refresh: every %.1fh | scope=%s | drop_dead=%s | run_on_start=%s"
          % (args.interval_hours, args.scope, args.drop_dead, run_on_start))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
