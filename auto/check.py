import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import (DB_PATH, REPORT_JSON, REPORT_TXT, ROOT, check_entry,
                    check_repo, derive_targets, http_check, is_http,
                    is_whitelisted, item_key, load_json, load_whitelist, now_iso,
                    save_json, setup_stdout)
from jarscan import load_jarscan, short_name


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


def class_index(scan):
    by_name = {}
    for descriptor in scan.get("classes", []):
        by_name[short_name(descriptor).lower()] = descriptor
    urls_by_name = {}
    for descriptor, urls in scan.get("urls_by_class", {}).items():
        urls_by_name[short_name(descriptor).lower()] = urls
    return by_name, urls_by_name


def check_item(item, group, jar_index=None, timeout=8):
    by_name, urls_by_name = jar_index or ({}, {})
    if group == "repos":
        return check_repo(item, timeout=timeout)
    api = item.get("api")
    if by_name and isinstance(api, str) and api.startswith("csp_"):
        target_name = api[4:].lower()
        if target_name not in by_name:
            return {"ok": False, "static": True, "checked_at": now_iso(),
                    "detail": "class %s missing in tvbox.jar" % api}
        if not derive_targets(item) and urls_by_name.get(target_name):
            return check_entry(item, timeout, 3, urls_by_name[target_name])
    return check_entry(item, timeout=timeout)


def run_checks(items, group, workers, timeout, jar_index=None):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for item in items:
            item.setdefault("_meta", {})
            futures[pool.submit(check_item, item, group, jar_index, timeout)] = item
        for future in as_completed(futures):
            item = futures[future]
            item["_meta"]["last_check"] = future.result()
    stats = {"probed": 0, "static_fail": 0, "jar_targets": 0}
    for item in items:
        check = item["_meta"].get("last_check") or {}
        if check.get("static"):
            stats["static_fail"] += 1
            continue
        stats["probed"] += 1
        if check.get("target_source") == "jar":
            stats["jar_targets"] += 1
    return items, stats


def apply_policy_to_item(item, group, whitelist, threshold, auto_enabled):
    meta = item.setdefault("_meta", {})
    check = meta.get("last_check") or {}
    state = check.get("ok")
    event = ""
    if state is True:
        meta["fail_streak"] = 0
        meta["ok_streak"] = int(meta.get("ok_streak") or 0) + 1
    elif state is False:
        meta["fail_streak"] = int(meta.get("fail_streak") or 0) + 1
        meta["ok_streak"] = 0
    recent = meta.get("recent")
    if not isinstance(recent, list):
        recent = []
    recent.append({"at": check.get("checked_at") or now_iso(), "ok": state,
                   "detail": str(check.get("detail") or check.get("code") or "")[:160]})
    meta["recent"] = recent[-10:]
    if meta.get("blocked"):
        if state is True and meta.get("blocked_reason") == "auto":
            meta["blocked"] = False
            meta.pop("blocked_reason", None)
            meta.pop("blocked_at", None)
            event = "unblocked"
        return event
    if (auto_enabled and state is False
            and int(meta.get("fail_streak") or 0) >= threshold
            and not is_whitelisted(whitelist, group, item)):
        meta["blocked"] = True
        meta["blocked_reason"] = "auto"
        meta["blocked_at"] = now_iso()
        event = "blocked"
    return event


def probe_parses(db, timeout, workers):
    parses = db.get("parses")
    if parses is None:
        tvbox = load_json(os.path.join(ROOT, "tvbox.json"), {})
        parses = tvbox.get("parses", [])
    parses = [item for item in parses
              if isinstance(item, dict) and is_http(item.get("url"))]
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(http_check, item["url"], timeout=timeout): item for item in parses}
        for future in as_completed(futures):
            item = futures[future]
            result = future.result()
            result["ok"] = result.get("code") is not None and result["code"] < 500
            result["name"] = item.get("name", "")
            results.append(result)
    return results


def summarize(items):
    ok = fail = skipped = 0
    dead = []
    for item in items:
        check = (item.get("_meta") or {}).get("last_check") or {}
        state = check.get("ok")
        if state is True:
            ok += 1
        elif state is False:
            fail += 1
            dead.append(item)
        else:
            skipped += 1
    return ok, fail, skipped, dead


def label(item):
    return str(item.get("name") or item.get("url") or item.get("key") or "?")


def main():
    parser = argparse.ArgumentParser(description="Health check all sources in auto/sources.json")
    parser.add_argument("--only", default="all",
                        help="all or comma list: sites,caiji,repos")
    parser.add_argument("--limit", type=int, default=0, help="max items per group")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--skip-parses", action="store_true")
    parser.add_argument("--auto-block-after", type=int, default=env_int("AUTO_BLOCK_AFTER", 2),
                        help="auto block after N consecutive failed checks")
    parser.add_argument("--no-auto-block", action="store_true", default=not env_bool("AUTO_BLOCK", True),
                        help="disable auto blocking")
    args = parser.parse_args()
    setup_stdout()

    db = load_json(DB_PATH)
    if db is None:
        print("sources.json not found, run first: python auto/seed.py")
        return

    auto_enabled = not args.no_auto_block
    whitelist = load_whitelist()

    scan = load_jarscan()
    by_name, urls_by_name = class_index(scan)

    referenced = set()
    for item in db.get("sites", []):
        api = item.get("api")
        if isinstance(api, str) and api.startswith("csp_"):
            referenced.add(api)
    missing_classes = sorted(name for name in referenced if name[4:].lower() not in by_name)

    checked_at = now_iso()
    report = {"checked_at": checked_at}
    text_lines = ["== check report ==  %s" % checked_at, ""]
    text_lines.append("jar   : classes=%-5d csp referenced=%-3d missing=%d | classes with embedded urls=%d"
                      % (len(scan.get("classes", [])), len(referenced), len(missing_classes),
                         len(scan.get("urls_by_class", {}))))
    if missing_classes:
        text_lines.append("  MISSING: %s" % ", ".join(missing_classes))
    report["jar"] = {"classes": len(scan.get("classes", [])),
                     "referenced": len(referenced),
                     "missing": missing_classes}

    config = load_json(os.path.join(ROOT, "tvbox.json"), {}) or {}
    config_missing = [key for key in ("spider", "sites", "parses", "lives")
                      if key not in config]
    if config_missing:
        text_lines.append("CONFIG WARN: tvbox.json missing top-level keys: %s"
                          % ", ".join(config_missing))
        print("[CONFIG WARN] tvbox.json missing top-level keys: %s"
              % ", ".join(config_missing))
    report["config"] = {"missing_keys": config_missing}

    selected = [part.strip() for part in str(args.only).split(",") if part.strip()]
    if "all" in selected or not selected:
        selected = ["sites", "caiji", "repos"]
    invalid = [name for name in selected if name not in ("sites", "caiji", "repos")]
    if invalid:
        parser.error("--only: unknown group(s): %s" % ", ".join(invalid))
    groups = []
    if "sites" in selected:
        groups.append(("sites", "sites", (by_name, urls_by_name)))
    if "caiji" in selected:
        groups.append(("caiji", "caiji", None))
    if "repos" in selected:
        groups.append(("repos", "repos", None))

    total_ok = total_fail = total_skip = 0
    events = []
    blocked_items = []
    total_blocked = 0
    for group, title, jar_index in groups:
        items = db.get(group, [])
        if args.limit:
            items = items[:args.limit]
        items, stats = run_checks(items, group, args.workers, args.timeout, jar_index)
        ok, fail, skipped, dead = summarize(items)
        total_ok += ok
        total_fail += fail
        total_skip += skipped
        group_blocked = group_whitelisted = 0
        for item in items:
            event = apply_policy_to_item(item, group, whitelist, args.auto_block_after, auto_enabled)
            meta = item.get("_meta") or {}
            if event:
                events.append({"group": group, "event": event, "name": label(item),
                               "key": item_key(item)})
            if meta.get("blocked"):
                group_blocked += 1
                total_blocked += 1
                check = meta.get("last_check") or {}
                blocked_items.append({"group": group, "name": label(item), "key": item_key(item),
                                      "reason": meta.get("blocked_reason") or "",
                                      "blocked_at": meta.get("blocked_at") or "",
                                      "detail": str(check.get("detail") or check.get("code") or "")})
            if is_whitelisted(whitelist, group, item):
                group_whitelisted += 1
        text_lines.append("%-6s: ok=%-4d fail=%-4d skip=%-4d total=%-4d blocked=%-3d whitelist=%d"
                          % (title, ok, fail, skipped, len(items), group_blocked, group_whitelisted))
        if group == "sites":
            text_lines.append("        jar-target probed=%d | static class fail=%d" % (stats["jar_targets"], stats["static_fail"]))
        report[title] = {"ok": ok, "fail": fail, "skipped": skipped,
                         "blocked": group_blocked, "whitelisted": group_whitelisted, "dead": []}
        for item in dead:
            check = item["_meta"]["last_check"]
            source = check.get("target_source", "")
            meta = item.get("_meta") or {}
            suffix = " [BLOCKED]" if meta.get("blocked") else ""
            text_lines.append("  DEAD [%s] %s | %s%s%s" % (
                title, label(item), check.get("detail") or check.get("url") or "",
                " (target from %s)" % source if source == "jar" else "", suffix))
            report[title]["dead"].append({
                "name": label(item),
                "key": item.get("key", ""),
                "url": check.get("url", ""),
                "detail": check.get("detail", ""),
                "target_source": source,
                "blocked": bool(meta.get("blocked")),
            })

    if not args.skip_parses:
        parse_results = probe_parses(db, args.timeout, args.workers)
        parse_ok = sum(1 for item in parse_results if item["ok"])
        parse_fail = len(parse_results) - parse_ok
        blocked_parses = set(db.get("blocked_parses") or [])
        text_lines.append("parses: ok=%-4d fail=%-4d total=%-4d blocked=%d"
                          % (parse_ok, parse_fail, len(parse_results), len(blocked_parses)))
        report["parses"] = {"ok": parse_ok, "fail": parse_fail, "dead": []}
        db["_parses"] = parse_results
        for item in parse_results:
            if not item["ok"]:
                suffix = " [BLOCKED]" if item.get("url") in blocked_parses else ""
                text_lines.append("  DEAD [parses] %s | %s%s" % (item.get("name", ""),
                                 " " + str(item.get("detail") or item.get("code")),
                                 suffix))
                report["parses"]["dead"].append({"name": item.get("name"), "url": item.get("url"),
                                                 "detail": item.get("detail", "")})
        report["parses"]["blocked"] = len(blocked_parses)

    text_lines.append("")
    text_lines.append("policy: auto_block=%s after=%d | blocked=%d" % (
        "on" if auto_enabled else "off", args.auto_block_after, total_blocked))
    if events:
        text_lines.append("events:")
        for event in events:
            text_lines.append("  %s [%s] %s" % (event["event"].upper(), event["group"], event["name"]))
    if blocked_items:
        text_lines.append("")
        text_lines.append("-- blocked (%d) --" % len(blocked_items))
        for item in blocked_items:
            text_lines.append("  BLOCKED [%s] %s | reason=%s | %s"
                              % (item["group"], item["name"], item["reason"], item["detail"]))

    report["summary"] = {"ok": total_ok, "fail": total_fail, "skipped": total_skip}
    report["policy"] = {"auto_block": auto_enabled, "auto_block_after": args.auto_block_after,
                        "blocked_total": total_blocked}
    report["events"] = events
    report["blocked"] = blocked_items
    text_lines.append("")
    text_lines.append("total : ok=%-4d fail=%-4d skip=%-4d" % (total_ok, total_fail, total_skip))

    db["updated"] = checked_at
    save_json(DB_PATH, db)
    save_json(REPORT_JSON, report)
    with open(REPORT_TXT, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(text_lines) + "\n")

    print("\n".join(text_lines))
    print("")
    print("report: %s" % REPORT_TXT)


if __name__ == "__main__":
    main()
