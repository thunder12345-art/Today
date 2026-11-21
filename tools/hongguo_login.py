#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Open a dedicated Edge profile, let the user sign in, then save cookies."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import websocket
except ImportError:
    print("缺少 websocket-client，请先执行: python -m pip install websocket-client")
    raise SystemExit(2)

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
LOGIN_NAMES = ("sessionid", "sid_guard", "sid_tt", "uid_tt")
DOMAIN = "hongguoduanju.com"


def find_edge():
    for path in EDGE_PATHS:
        if os.path.exists(path):
            return path
    raise SystemExit("找不到 msedge.exe")


def page_target(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=3) as resp:
                targets = json.load(resp)
        except Exception:
            time.sleep(0.5)
            continue
        pages = [t for t in targets if t.get("type") == "page"]
        for target in pages:
            if DOMAIN in (target.get("url") or ""):
                return target
        if pages:
            return pages[0]
        time.sleep(0.5)
    return None


def cdp_call(ws, msg_id, method, params=None):
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == msg_id:
            return message


def read_cookies(port):
    target = page_target(port)
    if not target:
        return []
    ws = websocket.create_connection(target["webSocketDebuggerUrl"],
                                     timeout=15, origin="http://localhost")
    try:
        cdp_call(ws, 1, "Network.enable")
        result = cdp_call(ws, 2, "Network.getAllCookies")
        return (result.get("result") or {}).get("cookies") or []
    finally:
        ws.close()


def cookie_header(cookies):
    now = time.time()
    selected = []
    for cookie in cookies:
        domain = cookie.get("domain") or ""
        if DOMAIN not in domain:
            continue
        if cookie.get("expires") and cookie.get("expires") < now:
            continue
        name = cookie.get("name") or ""
        value = cookie.get("value") or ""
        if name and value:
            selected.append((domain, name, value))
    selected.sort(key=lambda item: (0 if item[0].startswith(".") else 1,
                                    item[1]))
    seen = {}
    for _domain, name, value in selected:
        seen[name] = value
    return "; ".join("%s=%s" % (name, value)
                     for name, value in seen.items()), selected


def upload(server, proxy_base, cookie):
    body = json.dumps({"cookie": cookie, "base": proxy_base},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        server.rstrip("/") + "/api/hongguo", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=45) as resp:
        return resp.status, json.load(resp)


def main():
    parser = argparse.ArgumentParser(description="红果扫码登录并保存 Cookie")
    parser.add_argument("--server", default="http://192.168.1.254:8896",
                        help="Tomorrow 面板地址（用于自动上传登录态）")
    parser.add_argument("--proxy-base", default="http://192.168.1.254:8899",
                        help="TVBox 播放代理地址")
    parser.add_argument("--profile",
                        default=os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                             "Tomorrow", "EdgeProfile"),
                        help="专用 Edge 配置目录")
    parser.add_argument("--port", type=int, default=9226,
                        help="本地 DevTools 端口")
    parser.add_argument("--timeout", type=int, default=300,
                        help="等待扫码登录的秒数")
    parser.add_argument("--out", default=os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "hongguo_cookie.txt"),
                        help="Cookie 保存路径")
    parser.add_argument("--no-upload", action="store_true",
                        help="只保存本地 txt，不上传面板")
    parser.add_argument("--keep-open", action="store_true",
                        help="登录成功后不关闭 Edge")
    args = parser.parse_args()

    edge = find_edge()
    profile = os.path.abspath(args.profile)
    os.makedirs(profile, exist_ok=True)
    url = "https://hongguoduanju.com/"
    command = [
        edge,
        "--user-data-dir=" + profile,
        "--remote-debugging-port=%d" % args.port,
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    print("[hongguo] 启动专用 Edge 配置：%s" % profile)
    print("[hongguo] 请在打开的窗口里点击登录并扫码，程序会自动等待 Cookie。")
    proc = subprocess.Popen(command)
    deadline = time.time() + args.timeout
    header = ""
    selected = []
    try:
        while time.time() < deadline:
            try:
                cookies = read_cookies(args.port)
            except Exception:
                time.sleep(1.0)
                continue
            header, selected = cookie_header(cookies)
            names = {item[1] for item in selected}
            if all(name in names for name in ("sessionid", "sid_guard")):
                break
            time.sleep(2.0)
        if not header or not all(
                name in {item[1] for item in selected}
                for name in ("sessionid", "sid_guard")):
            print("[hongguo] 超时：没有检测到登录 Cookie。")
            return 1

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(header + "\n", encoding="utf-8")
        print("[hongguo] 已保存 %d 个 Cookie 到 %s" % (len(selected), out))
        print("[hongguo] Cookie 名称：%s" %
              ", ".join(item[1] for item in selected))

        if not args.no_upload:
            try:
                code, data = upload(args.server, args.proxy_base, header)
                print("[hongguo] 上传面板：HTTP %s" % code)
                test = data.get("last_test") or {}
                print("[hongguo] 登录测试：%s" % (test.get("detail") or "未测试"))
            except Exception as error:
                print("[hongguo] 上传失败：%s" % error)
        return 0
    finally:
        if not args.keep_open and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            else:
                proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
