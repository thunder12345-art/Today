import argparse
import os

from common import (DB_PATH, WHITELIST_PATH, derive_targets, item_key,
                    load_json, norm_key, norm_target, now_iso, save_json,
                    save_whitelist, setup_stdout, strip_meta)

RUNTIME_FIELDS = ("last_check", "fail_streak", "ok_streak", "recent",
                  "blocked", "blocked_reason", "blocked_at", "class_missing")


def index_items(items):
    by_key = {}
    by_target = {}
    for index, item in enumerate(items):
        key = norm_key(item)
        if key:
            by_key.setdefault(key, index)
        for target in derive_targets(item):
            by_target.setdefault(norm_target(target), index)
    return by_key, by_target


def match_runtime(incoming_item, by_key, by_target, used):
    key = norm_key(incoming_item)
    if key and key in by_key and by_key[key] not in used:
        return by_key[key]
    for target in derive_targets(incoming_item):
        index = by_target.get(norm_target(target))
        if index is not None and index not in used:
            return index
    return None


def merge_group(incoming, runtime, group, stats):
    incoming_items = incoming.get(group) or []
    runtime_items = runtime.get(group) or []
    by_key, by_target = index_items(runtime_items)
    used = set()
    result = []
    for item in incoming_items:
        if not isinstance(item, dict):
            continue
        runtime_index = match_runtime(item, by_key, by_target, used)
        if runtime_index is None:
            result.append(item)
            continue
        used.add(runtime_index)
        old = runtime_items[runtime_index]
        old_meta = old.get("_meta") or {}
        meta = dict(item.get("_meta") or {})
        for field in RUNTIME_FIELDS:
            if field in old_meta:
                meta[field] = old_meta[field]
        meta.setdefault("origin", old_meta.get("origin"))
        meta.setdefault("added_at", old_meta.get("added_at"))
        meta["targets"] = derive_targets(item)
        meta["merged_at"] = now_iso()
        result.append(dict(strip_meta(item), _meta=meta))
        stats["matched"] += 1
    kept = 0
    for index, item in enumerate(runtime_items):
        if index in used:
            continue
        result.append(item)
        kept += 1
        stats["runtime_only"].append("%s:%s" % (group, item_key(item)))
    incoming[group] = result
    stats["kept"] += kept
    return result


def merge_parses(incoming, runtime, stats):
    incoming_blocked = set(incoming.get("blocked_parses") or [])
    runtime_blocked = set(runtime.get("blocked_parses") or [])
    merged_blocked = sorted(incoming_blocked | runtime_blocked)
    incoming["blocked_parses"] = merged_blocked
    stats["blocked_parses"] = len(merged_blocked)
    runtime_parses = runtime.get("_parses") or []
    incoming_parses = incoming.get("_parses") or []
    if runtime_parses:
        incoming["_parses"] = runtime_parses
    elif incoming_parses:
        incoming["_parses"] = incoming_parses


def merge_whitelist(path, runtime_path):
    incoming = load_whitelist_runtime(path)
    runtime = load_whitelist_runtime(runtime_path) if runtime_path else {}
    merged = {}
    added = 0
    for group in ("sites", "caiji", "repos", "parses"):
        values = set(incoming.get(group) or []) | set(runtime.get(group) or [])
        merged[group] = values
        added += len(values - set(incoming.get(group) or []))
    save_whitelist(merged)
    return added


def load_whitelist_runtime(path):
    if not path or not os.path.exists(path):
        return {}
    data = load_json(path, {}) or {}
    return {group: [str(value).strip() for value in (data.get(group) or []) if str(value).strip()]
            for group in ("sites", "caiji", "repos", "parses")}


def run(db_path, runtime_path, whitelist_runtime, out_path):
    incoming = load_json(db_path)
    runtime = load_json(runtime_path)
    if incoming is None or runtime is None:
        print("[merge] missing db: %s or runtime snapshot: %s" % (db_path, runtime_path))
        return 1
    stats = {"matched": 0, "kept": 0, "runtime_only": [], "blocked_parses": 0}
    for group in ("sites", "caiji", "repos"):
        merge_group(incoming, runtime, group, stats)
    merge_parses(incoming, runtime, stats)
    merged_whitelist = merge_whitelist(WHITELIST_PATH, whitelist_runtime)

    incoming["updated"] = now_iso()
    incoming["merged_from_runtime_at"] = now_iso()
    save_json(out_path, incoming)

    print("[merge] matched runtime metadata: %d" % stats["matched"])
    print("[merge] runtime-only entries kept: %d" % stats["kept"])
    if stats["runtime_only"]:
        for name in stats["runtime_only"]:
            print("   keep %s" % name)
    print("[merge] blocked_parses: %d | whitelist merged-in: %d" % (stats["blocked_parses"], merged_whitelist))
    print("[merge] saved %s" % out_path)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Merge NAS runtime metadata into merged sources.json")
    parser.add_argument("--db", default=DB_PATH, help="worktree sources.json (after git merge)")
    parser.add_argument("--runtime", required=True, help="runtime snapshot copied before merge")
    parser.add_argument("--whitelist", default=None, help="runtime whitelist snapshot")
    parser.add_argument("--out", default=None, help="output path (default: overwrite --db)")
    args = parser.parse_args()
    setup_stdout()
    raise SystemExit(run(args.db, args.runtime, args.whitelist, args.out or args.db))


if __name__ == "__main__":
    main()
