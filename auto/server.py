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
    "upstream_running": False,
    "upstream_last_at": None,
    "upstream_last_result": None,
    "last_run_ts": None,
    "last_cycle_ts": None,
}
LOCK = threading.Lock()

PANEL_FILE = os.path.join(BASE, "panel.html")
UPSTREAMS_PATH = os.path.join(BASE, "upstreams.json")
UPSTREAM_SCRIPT = os.path.join(BASE, "upstream_sync.py")
UPSTREAM_LOG = os.path.join(BASE, "upstream_sync.log")
UPSTREAM_STATE = os.path.join(BASE, ".upstream_state.json")
SETTINGS_PATH = os.path.join(BASE, "settings.json")
TOKEN = os.environ.get("PANEL_TOKEN", "").strip()

SETTINGS_DEFAULTS = {
    "interval_hours": 12.0,
    "auto_sync": True,
    "check_groups": {"sites": True, "caiji": True, "repos": True, "parses": True},
}


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


def load_settings():
    data = load_json(SETTINGS_PATH, {})
    if not isinstance(data, dict):
        data = {}
    settings = {"interval_hours": SETTINGS_DEFAULTS["interval_hours"],
                "auto_sync": SETTINGS_DEFAULTS["auto_sync"],
                "check_groups": dict(SETTINGS_DEFAULTS["check_groups"])}
    try:
        settings["interval_hours"] = float(data.get("interval_hours", settings["interval_hours"]))
    except (TypeError, ValueError):
        pass
    settings["interval_hours"] = min(max(settings["interval_hours"], 0.0), 168.0)
    if "auto_sync" in data:
        settings["auto_sync"] = bool(data.get("auto_sync"))
    raw_groups = data.get("check_groups")
    if isinstance(raw_groups, dict):
        for key in settings["check_groups"]:
            if key in raw_groups:
                settings["check_groups"][key] = bool(raw_groups.get(key))
    if not any(settings["check_groups"][name] for name in ("sites", "caiji", "repos")):
        settings["check_groups"] = dict(SETTINGS_DEFAULTS["check_groups"])
    return settings


def ensure_seed():
    if not os.path.exists(DB_PATH):
        print("[init] sources.json missing, running seed.py")
        subprocess.run([sys.executable, os.path.join(BASE, "seed.py")])


def run_pipeline(scope, drop_dead, timeout, workers):
    if not LOCK.acquire(blocking=False):
        print("[pipeline] previous run still active, skip")
        return False
    try:
        STATE["running"] = True
        started = time.time()
        command = build_pipeline_command(scope, drop_dead, timeout, workers, load_settings())
        print("[pipeline] started: %s" % " ".join(command))
        result = subprocess.run(command)
        STATE["last_run"] = now_iso()
        STATE["last_run_ts"] = time.time()
        STATE["last_duration"] = round(time.time() - started, 1)
        STATE["last_result"] = "ok" if result.returncode == 0 else "failed (%d)" % result.returncode
        print("[pipeline] finished: %s in %.1fs" % (STATE["last_result"], STATE["last_duration"]))
    finally:
        STATE["running"] = False
        LOCK.release()
    return True


def build_pipeline_command(scope, drop_dead, timeout, workers, settings):
    command = [sys.executable, os.path.join(BASE, "run.py"),
               "--scope", scope, "--timeout", str(timeout),
               "--workers", str(workers), "--inplace"]
    if drop_dead:
        command.append("--drop-dead")
    if not STATE["auto_block"]:
        command.append("--no-auto-block")
    else:
        command.extend(["--auto-block-after", str(STATE["auto_block_after"])])
    groups = settings.get("check_groups") or {}
    enabled = [name for name in ("sites", "caiji", "repos") if groups.get(name, True)]
    if len(enabled) != 3 and enabled:
        command.extend(["--only-groups", ",".join(enabled)])
    if not groups.get("parses", True):
        command.append("--skip-parses")
    return command


def schedule_loop(scope, drop_dead, timeout, workers, run_on_start):
    if run_on_start:
        run_pipeline(scope, drop_dead, timeout, workers)
        STATE["last_cycle_ts"] = time.time()
    while True:
        time.sleep(30)
        try:
            settings = load_settings()
            interval = float(settings.get("interval_hours") or 0)
            if interval <= 0:
                continue
            last = STATE.get("last_cycle_ts") or STATE.get("last_run_ts")
            if last is None:
                STATE["last_cycle_ts"] = time.time()
                continue
            if time.time() - last < interval * 3600:
                continue
            if STATE["running"] or STATE["upstream_running"]:
                continue
            print("[schedule] cycle due (interval=%sh, auto_sync=%s)" % (interval, settings.get("auto_sync")))
            if settings.get("auto_sync", True):
                run_upstream_sync("", then_pipeline=False)
            if run_pipeline(scope, drop_dead, timeout, workers):
                STATE["last_cycle_ts"] = time.time()
        except Exception as error:
            print("[schedule] error: %s" % error)


def load_upstreams():
    data = load_json(UPSTREAMS_PATH)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def upstream_state():
    state = load_json(UPSTREAM_STATE, {})
    return state if isinstance(state, dict) else {}


def build_upstreams():
    data = load_upstreams()
    state = upstream_state()
    items = []
    for index, item in enumerate(data.get("items", [])):
        if not isinstance(item, dict):
            continue
        info = state.get((item.get("url") or "").strip()) or {}
        entry = dict(item)
        entry["index"] = index
        entry["last_sync_at"] = info.get("last_sync_at") or ""
        entry["last_result"] = info.get("last_result") or ""
        items.append(entry)
    log_tail = ""
    if os.path.exists(UPSTREAM_LOG):
        try:
            with open(UPSTREAM_LOG, "r", encoding="utf-8", errors="replace") as handle:
                log_tail = handle.read()[-4000:]
        except OSError:
            pass
    return {"items": items, "running": bool(STATE.get("upstream_running")),
            "last_result": STATE.get("upstream_last_result"),
            "last_at": STATE.get("upstream_last_at"), "log": log_tail}


def run_upstream_sync(only, then_pipeline=True):
    if not os.path.exists(UPSTREAM_SCRIPT):
        print("[upstream] script not found: %s" % UPSTREAM_SCRIPT)
        return
    if not LOCK.acquire(blocking=False):
        print("[upstream] pipeline running, skip")
        return
    try:
        STATE["upstream_running"] = True
        STATE["upstream_last_at"] = now_iso()
        command = [sys.executable, UPSTREAM_SCRIPT]
        if only:
            command.extend(["--only", only])
        print("[upstream] started: %s" % " ".join(command))
        with open(UPSTREAM_LOG, "w", encoding="utf-8") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        STATE["upstream_last_result"] = "ok" if result.returncode == 0 else "failed (%d)" % result.returncode
        print("[upstream] finished: %s" % STATE["upstream_last_result"])
    finally:
        STATE["upstream_running"] = False
        LOCK.release()
    if then_pipeline and STATE.get("upstream_last_result") == "ok":
        run_pipeline(STATE["scope"], STATE["drop_dead"], STATE["timeout"], STATE["workers"])


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
    parses = db.get("parses")
    if parses is None:
        tvbox = load_json(os.path.join(ROOT, "tvbox.json"), {}) or {}
        parses = tvbox.get("parses", [])
    return [{"url": item.get("url"), "name": item.get("name", ""), "ok": None}
            for item in parses
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
            "settings": load_settings(),
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
            elif path == "/api/upstreams":
                self.send_json(build_upstreams())
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
        elif parts.path == "/api/upstreams":
            self.handle_upstreams(data)
        elif parts.path == "/api/settings":
            self.handle_settings(data)
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

    def handle_upstreams(self, data):
        action = str(data.get("action") or "")
        if action == "sync":
            if STATE["upstream_running"]:
                self.send_json({"error": "上游同步已在运行"}, status=409)
                return
            thread = threading.Thread(target=run_upstream_sync,
                                      args=(str(data.get("name") or "").strip(),),
                                      daemon=True)
            thread.start()
            self.send_json({"ok": True, "started": True})
            return
        if action not in ("add", "remove", "toggle"):
            self.send_json({"error": "unknown action"}, status=400)
            return
        if not LOCK.acquire(blocking=False):
            self.send_json({"error": "pipeline running, try later"}, status=409)
            return
        try:
            store = load_upstreams()
            items = store["items"]
            if action == "add":
                url = str(data.get("url") or "").strip()
                if "://" not in url:
                    self.send_json({"error": "请输入完整的仓库 URL（https://github.com/owner/repo）"}, status=400)
                    return
                if any(isinstance(item, dict) and item.get("url") == url for item in items):
                    self.send_json({"error": "该仓库已在列表中"}, status=400)
                    return
                name = str(data.get("name") or "").strip() or url.rstrip("/").rsplit("/", 1)[-1]
                items.append({"name": name, "url": url,
                              "branch": str(data.get("branch") or "").strip() or "master",
                              "ghproxy": bool(data.get("ghproxy", True)), "enabled": True})
            else:
                try:
                    index = int(data.get("index", -1))
                except (TypeError, ValueError):
                    index = -1
                if index < 0 or index >= len(items):
                    self.send_json({"error": "无效序号"}, status=400)
                    return
                if action == "remove":
                    items.pop(index)
                else:
                    item = items[index]
                    if not isinstance(item, dict):
                        self.send_json({"error": "无效条目"}, status=400)
                        return
                    item["enabled"] = not item.get("enabled", True)
            save_json(UPSTREAMS_PATH, store)
            self.send_json({"ok": True})
        finally:
            LOCK.release()

    def handle_settings(self, data):
        if not isinstance(data, dict):
            self.send_json({"error": "bad payload"}, status=400)
            return
        if not LOCK.acquire(blocking=False):
            self.send_json({"error": "pipeline running, try later"}, status=409)
            return
        try:
            settings = load_settings()
            if "interval_hours" in data:
                try:
                    value = float(data.get("interval_hours"))
                except (TypeError, ValueError):
                    self.send_json({"error": "周期必须是数字（小时）"}, status=400)
                    return
                settings["interval_hours"] = min(max(value, 0.0), 168.0)
            if "auto_sync" in data:
                settings["auto_sync"] = bool(data.get("auto_sync"))
            incoming_groups = data.get("check_groups")
            if isinstance(incoming_groups, dict):
                groups = dict(settings["check_groups"])
                for key in groups:
                    if key in incoming_groups:
                        groups[key] = bool(incoming_groups.get(key))
                if not any(groups.get(name) for name in ("sites", "caiji", "repos")):
                    self.send_json({"error": "站点 / 采集 / 仓库 至少保留一个"}, status=400)
                    return
                settings["check_groups"] = groups
            save_json(SETTINGS_PATH, settings)
            self.send_json({"ok": True, "settings": settings})
        finally:
            LOCK.release()

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
        target=schedule_loop,
        args=(args.scope, args.drop_dead, args.timeout, args.workers, run_on_start),
        daemon=True)
    worker.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), partial(Handler, directory=ROOT))
    print("[server] serving %s at http://0.0.0.0:%d" % (ROOT, args.port))
    print("[server] tvbox.json:  http://<host>:%d/tvbox.json" % args.port)
    print("[server] lmw.json:    http://<host>:%d/lmw.json" % args.port)
    print("[server] status:      http://<host>:%d/status" % args.port)
    print("[server] panel:       http://<host>:%d/panel%s" % (args.port, " (token required)" if TOKEN else ""))
    settings = load_settings()
    print("[server] schedule: interval=%sh auto_sync=%s (auto/settings.json, panel editable) | scope=%s | drop_dead=%s | auto_block=%s after=%d | run_on_start=%s"
          % (settings["interval_hours"], settings["auto_sync"], args.scope, args.drop_dead,
             STATE["auto_block"], STATE["auto_block_after"], run_on_start))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
