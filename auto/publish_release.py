import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "tomorrow-release/1.0"
SSL_CTX = ssl.create_default_context()


class HttpError(Exception):
    def __init__(self, code, body):
        super().__init__("HTTP %s: %s" % (code, body[:300]))
        self.code = code
        self.body = body


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def mask(token):
    if not token:
        return "-"
    return (token[:6] + "..." + token[-4:]) if len(token) > 12 else "***"


def parse_cred_file(path):
    tokens = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if "://" not in line or "@" not in line:
                    continue
                rest = line.split("://", 1)[1]
                userinfo, _, host = rest.rpartition("@")
                if not userinfo or not host:
                    continue
                host = host.split("/", 1)[0].split(":", 1)[0].lower()
                user, _, password = userinfo.partition(":")
                if password and host not in tokens:
                    tokens[host] = (urllib.parse.unquote(user), urllib.parse.unquote(password))
    except OSError:
        pass
    return tokens


def http_call(method, url, data=None, headers=None, timeout=600):
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=SSL_CTX) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as error:
        body = ""
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise HttpError(error.code, body)


def github_publish(repo, token, tag, name, body, asset_paths):
    api = "https://api.github.com/repos/%s" % repo
    headers = {"Authorization": "Bearer %s" % token,
               "Accept": "application/vnd.github+json",
               "User-Agent": UA}
    payload = json.dumps({"tag_name": tag, "name": name, "body": body}).encode("utf-8")
    try:
        _, data = http_call("POST", api + "/releases", payload,
                            dict(headers, **{"Content-Type": "application/json"}))
        release = json.loads(data.decode("utf-8"))
    except HttpError as error:
        if error.code != 422:
            raise
        _, data = http_call("GET", api + "/releases/tags/" + urllib.parse.quote(tag), None, headers)
        release = json.loads(data.decode("utf-8"))
        print("   github: release %s already exists, reusing" % tag)
    upload_base = release.get("upload_url", "").split("{")[0]
    for path in asset_paths:
        asset_name = os.path.basename(path)
        with open(path, "rb") as handle:
            blob = handle.read()
        try:
            http_call("POST", upload_base + "?name=" + urllib.parse.quote(asset_name), blob,
                      dict(headers, **{"Content-Type": "application/octet-stream"}))
            print("   github: uploaded %s (%.1f MB)" % (asset_name, len(blob) / 1048576.0))
        except HttpError as error:
            print("   github: upload %s failed: %s" % (asset_name, error))
    return release.get("html_url", "")


def multipart_body(field, filename, content, boundary):
    head = ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, field, filename))
    tail = "\r\n--%s--\r\n" % boundary
    return head.encode("utf-8") + content + tail.encode("utf-8")


def gitee_publish(repo, token, tag, name, body, asset_paths):
    api = "https://gitee.com/api/v5/repos/%s" % repo
    payload = urllib.parse.urlencode({"access_token": token, "tag_name": tag, "name": name,
                                      "body": body, "target_commitish": "main"}).encode("utf-8")
    try:
        _, data = http_call("POST", api + "/releases", payload,
                            {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
        release = json.loads(data.decode("utf-8"))
    except HttpError as error:
        if error.code not in (400, 403, 422):
            raise
        url = api + "/releases/tags/" + urllib.parse.quote(tag) + "?access_token=" + urllib.parse.quote(token)
        _, data = http_call("GET", url, None, {"User-Agent": UA})
        release = json.loads(data.decode("utf-8"))
        print("   gitee: release %s already exists, reusing" % tag)
    release_id = release.get("id")
    if not release_id:
        raise RuntimeError("gitee: no release id in response")
    for path in asset_paths:
        asset_name = os.path.basename(path)
        with open(path, "rb") as handle:
            blob = handle.read()
        boundary = "----tomorrow%08x" % (int(time.time() * 1000) & 0xffffffff)
        body_bytes = multipart_body("file", asset_name, blob, boundary)
        url = api + "/releases/%s/attach_files?access_token=%s" % (release_id, urllib.parse.quote(token))
        try:
            http_call("POST", url, body_bytes,
                      {"Content-Type": "multipart/form-data; boundary=%s" % boundary, "User-Agent": UA})
            print("   gitee: uploaded %s (%.1f MB)" % (asset_name, len(blob) / 1048576.0))
        except HttpError as error:
            print("   gitee: upload %s failed: %s" % (asset_name, error))
    return release.get("html_url") or ("https://gitee.com/%s/releases" % repo)


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Publish release assets (bundle/image/readme) to GitHub + Gitee")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v20261027-105702")
    parser.add_argument("--name", default="", help="release title (default: tag)")
    parser.add_argument("--bundle", default="", help="path to update.bundle")
    parser.add_argument("--image", default="", help="path to docker image tar.gz")
    parser.add_argument("--image-id", default="", help="docker image id (saved to state on success)")
    parser.add_argument("--readme", default="", help="path to README.md (used as release body + asset)")
    parser.add_argument("--github", default="", help="owner/repo on github.com")
    parser.add_argument("--gitee", default="", help="owner/repo on gitee.com")
    parser.add_argument("--github-token", default="")
    parser.add_argument("--gitee-token", default="")
    parser.add_argument("--cred-file", default="/root/.git-credentials")
    parser.add_argument("--state", default="", help="state file to record release/image id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    creds = parse_cred_file(args.cred_file)
    if creds:
        print("credentials: %s" % ", ".join("%s=%s" % (host, mask(value[1]))
                                            for host, value in sorted(creds.items())))
    else:
        print("credentials: none at %s (use --cred-file or GITHUB_TOKEN/GITEE_TOKEN)" % args.cred_file)
    gh_token = args.github_token or os.environ.get("GITHUB_TOKEN") or (creds.get("github.com") or ("", ""))[1]
    gt_token = args.gitee_token or os.environ.get("GITEE_TOKEN") or (creds.get("gitee.com") or ("", ""))[1]

    assets = []
    for path in (args.bundle, args.readme, args.image):
        if not path:
            continue
        if not os.path.exists(path):
            print("asset missing, skipped: %s" % path)
            continue
        assets.append(os.path.abspath(path))
    if not assets:
        print("no assets to publish")
        return 0
    body = ""
    if args.readme and os.path.exists(args.readme):
        with open(args.readme, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    name = args.name or args.tag
    print("release: %s | assets: %s" % (args.tag, ", ".join(os.path.basename(p) for p in assets)))
    if args.dry_run:
        print("dry-run: github=%s token=%s | gitee=%s token=%s"
              % (args.github or "-", mask(gh_token), args.gitee or "-", mask(gt_token)))
        return 0

    results = {}
    if args.github and gh_token:
        try:
            results["github"] = github_publish(args.github, gh_token, args.tag, name, body, assets) or "ok"
        except Exception as error:
            results["github"] = "failed: %s" % error
    elif args.github:
        print("github: no token, skipped")
    if args.gitee and gt_token:
        try:
            results["gitee"] = gitee_publish(args.gitee, gt_token, args.tag, name, body, assets) or "ok"
        except Exception as error:
            results["gitee"] = "failed: %s" % error
    elif args.gitee:
        print("gitee: no token, skipped")

    if not results:
        print("nothing published (no repo/token configured)")
        return 0
    ok = False
    for platform, value in results.items():
        print("   %s: %s" % (platform, value))
        if not str(value).startswith("failed"):
            ok = True
    if ok and args.state:
        state = load_state(args.state)
        state["last_release"] = args.tag
        state["last_release_at"] = now_iso()
        if args.image and args.image_id:
            state["image_id"] = args.image_id
        save_state(args.state, state)
        print("state saved: %s" % args.state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
