import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from common import (BASE, DB_PATH, REPORT_JSON, REPORT_TXT, ROOT, http_check,
                    is_http, is_whitelisted, item_key, load_json,
                    load_whitelist, now_iso, save_json, save_whitelist,
                    setup_stdout)
from check import apply_policy_to_item, check_item, class_index
from jarscan import load_jarscan
from submit import fetch_payload, parse_payload, submit_payloads

STATE = {
    "running": False,
    "last_run": None,
    "last_duration": None,
    "last_result": None,
    "started_at": now_iso(),
    "interval_hours": 12.0,
    "scope": "all",
    "drop_dead": False,
    "auto_block": True,
    "auto_block_after": 2,
    "timeout": 8,
    "workers": 12,
    "panel_token_required": False,
}
LOCK = threading.Lock()

PANEL_FILE = os.path.join(BASE, "panel.html")
TOKEN = os.environ.get("PANEL_TOKEN", "").strip()


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
        if not STATE["auto_block"]:
            command.append("--no-auto-block")
        else:
            command.extend(["--auto-block-after", str(STATE["auto_block_after"])])
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


def find_item(db, group, key):
    for item in db.get(group) or []:
        for field in ("key", "name", "url"):
            value = item.get(field)
            if isinstance(value, str) and value.strip() == key:
                return item
    return None


def check_detail(check):
    if not check:
        return ""
    if check.get("detail"):
        return str(check["detail"])
    if check.get("code") is not None:
        return "HTTP %s" % check["code"]
    return ""


def parse_items(db):
    results = db.get("_parses") or []
    if results:
        return results
    tvbox = load_json(os.path.join(ROOT, "tvbox.json"), {}) or {}
    return [{"url": item.get("url"), "name": item.get("name", ""), "ok": None}
            for item in tvbox.get("parses", [])
            if isinstance(item, dict) and is_http(item.get("url"))]


def build_status():
    db = load_json(DB_PATH, {}) or {}
    whitelist = load_whitelist()
    groups = {}
    for group in ("sites", "caiji", "repos"):
        items = db.get(group) or []
        info = {"total": len(items), "ok": 0, "fail": 0, "skip": 0, "blocked": 0, "whitelisted": 0}
        for item in items:
            meta = item.get("_meta") or {}
            state = (meta.get("last_check") or {}).get("ok")
            if state is True:
                info["ok"] += 1
            elif state is False:
                info["fail"] += 1
            else:
                info["skip"] += 1
            if meta.get("blocked"):
                info["blocked"] += 1
            if is_whitelisted(whitelist, group, item):
                info["whitelisted"] += 1
        groups[group] = info
    parses = parse_items(db)
    blocked_parses = set(db.get("blocked_parses") or [])
    parses_info = {"total": len(parses), "ok": 0, "fail": 0, "blocked": len(blocked_parses)}
    for item in parses:
        if item.get("ok"):
            parses_info["ok"] += 1
        else:
            parses_info["fail"] += 1
    return {"state": STATE, "server_time": now_iso(), "groups": groups,
            "parses": parses_info, "whitelist": whitelist,
            "db_updated": db.get("updated") or "",
            "report": load_json(REPORT_JSON, {})}


def build_sources():
    db = load_json(DB_PATH, {}) or {}
    whitelist = load_whitelist()
    blocked_parses = set(db.get("blocked_parses") or [])
    out = {"sites": [], "caiji": [], "repos": [], "parses": []}
    for group in ("sites", "caiji", "repos"):
        for item in db.get(group) or []:
            meta = item.get("_meta") or {}
            check = meta.get("last_check") or {}
            out[group].append({
                "key": item_key(item),
                "name": item.get("name") or "",
                "target": item.get("api") or item.get("url") or "",
                "origin": meta.get("origin") or "",
                "ok": check.get("ok"),
                "detail": check_detail(check),
                "checked_at": check.get("checked_at") or "",
                "fail_streak": int(meta.get("fail_streak") or 0),
                "ok_streak": int(meta.get("ok_streak") or 0),
                "blocked": bool(meta.get("blocked")),
                "blocked_reason": meta.get("blocked_reason") or "",
                "blocked_at": meta.get("blocked_at") or "",
                "static": bool(check.get("static")),
                "class_missing": bool(meta.get("class_missing")),
                "whitelisted": is_whitelisted(whitelist, group, item),
            })
    for item in parse_items(db):
        url = item.get("url") or ""
        out["parses"].append({
            "key": url,
            "name": item.get("name") or "",
            "target": url,
            "origin": "",
            "ok": item.get("ok"),
            "detail": check_detail(item),
            "checked_at": item.get("checked_at") or "",
            "fail_streak": 0,
            "ok_streak": 0,
            "blocked": url in blocked_parses,
            "blocked_reason": "manual" if url in blocked_parses else "",
            "blocked_at": "",
            "static": False,
            "class_missing": False,
            "whitelisted": False,
        })
    return out


class Handler(SimpleHTTPRequestHandler):
    server_version = "tomorrow-auto"

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path in ("/status", "/__status"):
            self.send_json({"state": STATE, "report": load_json(REPORT_JSON, {})})
            return
        if path == "/panel" or path.startswith("/panel/"):
            if not self.authorized(parts):
                self.send_login()
                return
            self.send_file(PANEL_FILE, "text/html; charset=utf-8")
            return
        if path.startswith("/api/"):
            if not self.authorized(parts):
                self.send_json({"error": "unauthorized"}, status=401)
                return
            if path == "/api/status":
                self.send_json(build_status())
            elif path == "/api/sources":
                self.send_json(build_sources())
            elif path == "/api/report.txt":
                self.send_file(REPORT_TXT, "text/plain; charset=utf-8")
            else:
                self.send_json({"error": "not found"}, status=404)
            return
        if path.startswith("/auto") or path.startswith("/.git"):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        parts = urllib.parse.urlsplit(self.path)
        if not parts.path.startswith("/api/"):
            self.send_error(404)
            return
        if not self.authorized(parts):
            self.send_json({"error": "unauthorized"}, status=401)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception as error:
            self.send_json({"error": "bad json: %s" % error}, status=400)
            return
        if parts.path == "/api/action":
            self.handle_action(data)
        elif parts.path == "/api/submit":
            self.handle_submit(data)
        elif parts.path == "/api/run":
            self.handle_run()
        else:
            self.send_json({"error": "not found"}, status=404)

    def authorized(self, parts):
        if not TOKEN:
            return True
        if self.headers.get("X-Panel-Token") == TOKEN:
            return True
        for chunk in (self.headers.get("Cookie") or "").split(";"):
            if chunk.strip() == "panel_token=" + TOKEN:
                return True
        query = urllib.parse.parse_qs(parts.query)
        if (query.get("token") or [""])[0] == TOKEN:
            self._set_panel_cookie = True
            return True
        return False

    def handle_action(self, data):
        action = str(data.get("action") or "")
        group = str(data.get("group") or "")
        key = str(data.get("key") or "").strip()
        if action not in ("block", "unblock", "whitelist", "unwhitelist", "recheck"):
            self.send_json({"error": "unknown action"}, status=400)
            return
        if group not in ("sites", "caiji", "repos", "parses"):
            self.send_json({"error": "unknown group"}, status=400)
            return
        if not key:
            self.send_json({"error": "missing key"}, status=400)
            return
        if not LOCK.acquire(blocking=False):
            self.send_json({"error": "pipeline running, try later"}, status=409)
            return
        try:
            whitelist = load_whitelist()
            db = load_json(DB_PATH, {}) or {}
            db.setdefault("_parses", [])
            if group == "parses":
                blocked = set(db.get("blocked_parses") or [])
                event = ""
                if action == "block":
                    blocked.add(key)
                    db["blocked_parses"] = sorted(blocked)
                elif action == "unblock":
                    blocked.discard(key)
                    db["blocked_parses"] = sorted(blocked)
                elif action == "recheck":
                    result = http_check(key, timeout=STATE["timeout"])
                    result["ok"] = result.get("code") is not None and result["code"] < 500
                    result["checked_at"] = now_iso()
                    parses = db.get("_parses") or parse_items(db)
                    for index, item in enumerate(parses):
                        if item.get("url") == key:
                            result["name"] = item.get("name", "")
                            parses[index] = result
                            break
                    else:
                        parses.append(result)
                    db["_parses"] = parses
                else:
                    self.send_json({"error": "parses 仅支持 屏蔽 / 解除 / 重测"}, status=400)
                    return
                db["updated"] = now_iso()
                save_json(DB_PATH, db)
                self.send_json({"ok": True, "event": event,
                                "blocked": key in set(db.get("blocked_parses") or [])})
                return
            item = find_item(db, group, key)
            if item is None and action == "whitelist":
                whitelist[group] = sorted(set(whitelist[group]) | {key})
                save_whitelist(whitelist)
                self.send_json({"ok": True, "event": "whitelisted"})
                return
            if item is None:
                self.send_json({"error": "item not found"}, status=404)
                return
            meta = item.setdefault("_meta", {})
            event = ""
            if action == "block":
                meta["blocked"] = True
                meta["blocked_reason"] = "manual"
                meta["blocked_at"] = now_iso()
            elif action == "unblock":
                meta["blocked"] = False
                meta.pop("blocked_reason", None)
                meta.pop("blocked_at", None)
            elif action == "whitelist":
                values = set(whitelist[group]) | {key}
                for field in ("key", "name"):
                    value = item.get(field)
                    if isinstance(value, str) and value.strip():
                        values.add(value.strip())
                whitelist[group] = sorted(values)
                save_whitelist(whitelist)
                if meta.get("blocked") and meta.get("blocked_reason") == "auto":
                    meta["blocked"] = False
                    meta.pop("blocked_reason", None)
                    meta.pop("blocked_at", None)
                    event = "unblocked"
            elif action == "unwhitelist":
                remove = {key}
                for field in ("key", "name", "url"):
                    value = item.get(field)
                    if isinstance(value, str) and value.strip():
                        remove.add(value.strip())
                whitelist[group] = sorted(set(whitelist[group]) - remove)
                save_whitelist(whitelist)
            elif action == "recheck":
                jar_index = class_index(load_jarscan())
                result = check_item(item, group, jar_index, timeout=STATE["timeout"])
                meta["last_check"] = result
                event = apply_policy_to_item(item, group, whitelist,
                                             STATE["auto_block_after"], STATE["auto_block"])
            db["updated"] = now_iso()
            save_json(DB_PATH, db)
            check = meta.get("last_check") or {}
            self.send_json({"ok": True, "event": event,
                            "item": {"key": item_key(item), "ok": check.get("ok"),
                                     "detail": check_detail(check),
                                     "blocked": bool(meta.get("blocked")),
                                     "blocked_reason": meta.get("blocked_reason") or "",
                                     "fail_streak": int(meta.get("fail_streak") or 0)}})
        finally:
            LOCK.release()

    def handle_submit(self, data):
        text = str(data.get("text") or "").strip()
        url = str(data.get("url") or "").strip()
        if not text and not url:
            self.send_json({"error": "需要 JSON 内容或 URL"}, status=400)
            return
        if not LOCK.acquire(blocking=False):
            self.send_json({"error": "pipeline running, try later"}, status=409)
            return
        try:
            payloads = []
            if text:
                sites, repos = parse_payload(text)
                payloads.append(("panel", sites, repos))
            if url:
                sites, repos = fetch_payload(url, STATE["timeout"])
                payloads.append(("panel-url", sites, repos))
            result = submit_payloads(payloads, caiji=bool(data.get("caiji")),
                                     timeout=STATE["timeout"],
                                     do_check=not bool(data.get("no_check")))
        except Exception as error:
            result = {"error": "提交失败: %s" % error}
        finally:
            LOCK.release()
        self.send_json(result)

    def handle_run(self):
        if STATE["running"]:
            self.send_json({"error": "pipeline already running"}, status=409)
            return
        thread = threading.Thread(target=run_pipeline,
                                  args=(STATE["scope"], STATE["drop_dead"],
                                        STATE["timeout"], STATE["workers"]),
                                  daemon=True)
        thread.start()
        self.send_json({"ok": True, "started": True})

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "_set_panel_cookie", False):
            self.send_header("Set-Cookie", "panel_token=%s; Path=/; SameSite=Lax; Max-Age=2592000" % TOKEN)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        if not os.path.exists(path):
            self.send_error(404)
            return
        with open(path, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "_set_panel_cookie", False):
            self.send_header("Set-Cookie", "panel_token=%s; Path=/; SameSite=Lax; Max-Age=2592000" % TOKEN)
        self.end_headers()
        self.wfile.write(body)

    def send_login(self):
        body = ("<!doctype html><meta charset=\"utf-8\"><title>控制面板</title>"
                "<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
                "form{background:#1c1c1e;padding:32px;border-radius:12px;min-width:280px}"
                "input{width:100%;padding:10px;margin:12px 0;border-radius:8px;border:1px solid #444;"
                "background:#2a2a2c;color:#eee;box-sizing:border-box}"
                "button{width:100%;padding:10px;border-radius:8px;border:0;background:#3b82f6;"
                "color:#fff;font-size:15px;cursor:pointer}"
                "p{margin:0 0 4px;color:#aaa;font-size:13px}</style>"
                "<form method=\"get\" action=\"/panel\">"
                "<p>需要访问令牌（PANEL_TOKEN）</p>"
                "<input name=\"token\" type=\"password\" placeholder=\"输入令牌\" autofocus>"
                "<button type=\"submit\">进入控制面板</button></form>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description="Self-hosted subscription server with scheduled refresh and panel")
    parser.add_argument("--port", type=int, default=env_int("PORT", 8080))
    parser.add_argument("--interval-hours", type=float, default=env_float("REFRESH_INTERVAL_HOURS", 12))
    parser.add_argument("--scope", choices=["local", "all"], default=os.environ.get("BUILD_SCOPE", "all"))
    parser.add_argument("--drop-dead", action="store_true", default=env_bool("DROP_DEAD", False))
    parser.add_argument("--timeout", type=float, default=env_float("CHECK_TIMEOUT", 8))
    parser.add_argument("--workers", type=int, default=env_int("CHECK_WORKERS", 12))
    parser.add_argument("--auto-block-after", type=int, default=env_int("AUTO_BLOCK_AFTER", 2))
    parser.add_argument("--no-auto-block", action="store_true", default=not env_bool("AUTO_BLOCK", True))
    parser.add_argument("--no-run-on-start", action="store_true", default=False)
    args = parser.parse_args()
    setup_stdout()

    ensure_seed()

    STATE["interval_hours"] = args.interval_hours
    STATE["scope"] = args.scope
    STATE["drop_dead"] = args.drop_dead
    STATE["auto_block"] = not args.no_auto_block
    STATE["auto_block_after"] = args.auto_block_after
    STATE["timeout"] = args.timeout
    STATE["workers"] = args.workers
    STATE["panel_token_required"] = bool(TOKEN)

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
    print("[server] panel:       http://<host>:%d/panel%s" % (args.port, " (token required)" if TOKEN else ""))
    print("[server] refresh: every %.1fh | scope=%s | drop_dead=%s | auto_block=%s after=%d | run_on_start=%s"
          % (args.interval_hours, args.scope, args.drop_dead,
             STATE["auto_block"], STATE["auto_block_after"], run_on_start))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
