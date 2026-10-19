import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import (DB_PATH, REPORT_JSON, REPORT_TXT, ROOT, check_entry,
                    check_repo, derive_targets, http_check, is_http, load_json,
                    now_iso, save_json, setup_stdout)
from jarscan import load_jarscan, short_name


def class_index(scan):
    by_name = {}
    for descriptor in scan.get("classes", []):
        by_name[short_name(descriptor).lower()] = descriptor
    urls_by_name = {}
    for descriptor, urls in scan.get("urls_by_class", {}).items():
        urls_by_name[short_name(descriptor).lower()] = urls
    return by_name, urls_by_name


def run_checks(items, checker, workers, timeout, jar_index=None):
    by_name, urls_by_name = jar_index or ({}, {})
    stats = {"probed": 0, "static_fail": 0, "jar_targets": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for item in items:
            item.setdefault("_meta", {})
            api = item.get("api")
            if by_name and isinstance(api, str) and api.startswith("csp_"):
                target_name = api[4:].lower()
                if target_name not in by_name:
                    item["_meta"]["last_check"] = {
                        "ok": False, "static": True, "checked_at": now_iso(),
                        "detail": "class %s missing in tvbox.jar" % api}
                    stats["static_fail"] += 1
                    continue
                if not derive_targets(item) and urls_by_name.get(target_name):
                    stats["jar_targets"] += 1
                    futures[pool.submit(check_entry, item, timeout, 3,
                                        urls_by_name[target_name])] = item
                    continue
            stats["probed"] += 1
            futures[pool.submit(checker, item, timeout)] = item
        for future in as_completed(futures):
            item = futures[future]
            item["_meta"]["last_check"] = future.result()
    return items, stats


def probe_parses(timeout, workers):
    tvbox = load_json(os.path.join(ROOT, "tvbox.json"), {})
    parses = [item for item in tvbox.get("parses", [])
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
    parser.add_argument("--only", choices=["all", "sites", "caiji", "repos"], default="all")
    parser.add_argument("--limit", type=int, default=0, help="max items per group")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--skip-parses", action="store_true")
    args = parser.parse_args()
    setup_stdout()

    db = load_json(DB_PATH)
    if db is None:
        print("sources.json not found, run first: python auto/seed.py")
        return

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

    groups = []
    if args.only in ("all", "sites"):
        groups.append(("sites", "sites", check_entry, (by_name, urls_by_name)))
    if args.only in ("all", "caiji"):
        groups.append(("caiji", "caiji", check_entry, None))
    if args.only in ("all", "repos"):
        groups.append(("repos", "repos", check_repo, None))

    total_ok = total_fail = total_skip = 0
    for group, title, checker, jar_index in groups:
        items = db.get(group, [])
        if args.limit:
            items = items[:args.limit]
        items, stats = run_checks(items, checker, args.workers, args.timeout, jar_index)
        ok, fail, skipped, dead = summarize(items)
        total_ok += ok
        total_fail += fail
        total_skip += skipped
        text_lines.append("%-6s: ok=%-4d fail=%-4d skip=%-4d total=%d" % (title, ok, fail, skipped, len(items)))
        if group == "sites":
            text_lines.append("        jar-target probed=%d | static class fail=%d" % (stats["jar_targets"], stats["static_fail"]))
        report[title] = {"ok": ok, "fail": fail, "skipped": skipped, "dead": []}
        for item in dead:
            check = item["_meta"]["last_check"]
            source = check.get("target_source", "")
            text_lines.append("  DEAD [%s] %s | %s%s" % (
                title, label(item), check.get("detail") or check.get("url") or "",
                " (target from %s)" % source if source == "jar" else ""))
            report[title]["dead"].append({
                "name": label(item),
                "key": item.get("key", ""),
                "url": check.get("url", ""),
                "detail": check.get("detail", ""),
                "target_source": source,
            })

    if not args.skip_parses:
        parse_results = probe_parses(args.timeout, args.workers)
        parse_ok = sum(1 for item in parse_results if item["ok"])
        parse_fail = len(parse_results) - parse_ok
        text_lines.append("parses: ok=%-4d fail=%-4d total=%d" % (parse_ok, parse_fail, len(parse_results)))
        report["parses"] = {"ok": parse_ok, "fail": parse_fail, "dead": []}
        for item in parse_results:
            if not item["ok"]:
                text_lines.append("  DEAD [parses] %s | %s" % (item.get("name", ""), item.get("detail") or item.get("code")))
                report["parses"]["dead"].append({"name": item.get("name"), "url": item.get("url"),
                                                 "detail": item.get("detail", "")})

    report["summary"] = {"ok": total_ok, "fail": total_fail, "skipped": total_skip}
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
