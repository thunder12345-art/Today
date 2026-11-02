import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from check import class_index, retry_passed, run_checks
from common import (BASE, DB_PATH, build_meta, derive_targets, extract_entries,
                    http_check, http_fetch, is_removed, item_key, load_json,
                    load_removed, now_iso, save_json, save_removed, setup_stdout,
                    strip_meta)
from jarscan import load_jarscan

FEEDS_PATH = os.path.join(BASE, "feeds.json")
OWN_PATH = os.path.join(BASE, "own_sources.json")
SETTINGS_PATH = os.path.join(BASE, "settings.json")
MAX_FEED_BYTES = 4 * 1048576


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def strict_from_settings():
    data = load_json(SETTINGS_PATH, {})
    return bool(isinstance(data, dict) and data.get("feed_strict"))


def feed_url(item):
    url = (item.get("url") or "").strip()
    if not url:
        return ""
    if item.get("ghproxy", True) and not url.lower().startswith("https://gh-proxy.com/"):
        if url.startswith(("http://", "https://")):
            return "https://gh-proxy.com/" + url
    return url


def fetch_feed_json(item, timeout=20):
    url = feed_url(item)
    if not url:
        return None
    code, body = http_fetch(url, timeout=timeout, max_bytes=MAX_FEED_BYTES)
    return json.loads(body.decode("utf-8", "replace"))


def existing_keys(db, group):
    keys = set()
    for item in db.get(group, []):
        if isinstance(item, dict):
            key = item_key(item).strip().casefold()
            if key:
                keys.add(key)
    return keys


def precheck_candidates(db, group, entries, precheck, stats):
    known = existing_keys(db, group)
    candidates = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = strip_meta(entry)
        key = item_key(content).strip().casefold()
        if key and key not in known and key not in candidates:
            candidates[key] = dict(content, _meta={})
    if not candidates:
        return set()
    items = list(candidates.values())
    jar_index = precheck.get("jar_index") if group in ("sites", "caiji") else None
    run_checks(items, group, precheck["workers"], precheck["timeout"], jar_index)
    strict = precheck.get("strict")
    reject = set()
    for key, item in candidates.items():
        state = (item.get("_meta") or {}).get("last_check") or {}
        ok = state.get("ok")
        if ok is False or (strict and ok is not True):
            reject.add(key)
            stats["rejected"] = int(stats.get("rejected") or 0) + 1
    return reject


def precheck_parses(db, parses, precheck, stats):
    known = set()
    for item in db.get("parses", []):
        if isinstance(item, dict) and item.get("url"):
            known.add(item["url"].strip().casefold())
    candidates = {}
    for entry in parses:
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        content = strip_meta(entry)
        url = content["url"].strip()
        key = url.casefold()
        if key not in known and key not in candidates:
            candidates[key] = url
    if not candidates:
        return set()
    urls = list(candidates.values())
    results = {}
    with ThreadPoolExecutor(max_workers=precheck["workers"]) as pool:
        futures = {pool.submit(http_check, url, precheck["timeout"]): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                result = future.result()
            except Exception:
                result = {"ok": False, "code": None}
            results[url.casefold()] = result.get("code") is not None and result.get("code") < 500
    reject = set()
    for key, ok in results.items():
        if not ok:
            reject.add(key)
            stats["rejected"] = int(stats.get("rejected") or 0) + 1
    return reject


def upsert_items(db, group, entries, origin, stats, reject=None):
    existing = db.setdefault(group, [])
    if not isinstance(existing, list):
        existing = []
        db[group] = existing
    by_key = {}
    for index, item in enumerate(existing):
        if not isinstance(item, dict):
            continue
        key = item_key(item).strip().casefold()
        if key:
            by_key.setdefault(key, index)
    ordered = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = strip_meta(entry)
        key = item_key(content).strip().casefold()
        if key:
            ordered[key] = content
    for key, content in ordered.items():
        if reject and key in reject:
            continue
        index = by_key.get(key)
        if index is not None:
            old = existing[index]
            meta = dict(old.get("_meta") or {})
            meta["origin"] = meta.get("origin") or origin
            meta["synced_at"] = now_iso()
            if group in ("sites", "caiji"):
                meta["targets"] = derive_targets(content)
            if strip_meta(old) != content:
                stats["updated"] += 1
            existing[index] = dict(content, _meta=meta)
        else:
            meta = build_meta(None, origin,
                              derive_targets(content) if group in ("sites", "caiji") else None)
            meta["synced_at"] = now_iso()
            existing.append(dict(content, _meta=meta))
            by_key[key] = len(existing) - 1
            stats["added"] += 1


def upsert_parses(db, parses, origin, stats, reject=None):
    existing = db.setdefault("parses", [])
    if not isinstance(existing, list):
        existing = []
        db["parses"] = existing
    by_url = {}
    for index, item in enumerate(existing):
        if isinstance(item, dict) and item.get("url"):
            by_url.setdefault(item["url"].strip().casefold(), index)
    unique = {}
    for entry in parses:
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        content = strip_meta(entry)
        unique[content["url"].strip().casefold()] = content
    for url, content in unique.items():
        if reject and url in reject:
            continue
        index = by_url.get(url)
        if index is not None:
            old = existing[index]
            meta = dict(old.get("_meta") or {})
            meta["origin"] = meta.get("origin") or origin
            meta["synced_at"] = now_iso()
            if strip_meta(old) != content:
                stats["updated"] += 1
            existing[index] = dict(content, _meta=meta)
        else:
            existing.append(dict(content, _meta={"origin": origin, "synced_at": now_iso()}))
            by_url[url] = len(existing) - 1
            stats["added"] += 1


def retry_from_payload(payload, removed, precheck):
    sites, repos = extract_entries(payload)
    pairs = []
    if removed.get("sites"):
        pairs.extend(("sites", entry) for entry in sites
                     if is_removed(removed, "sites", entry))
    if removed.get("repos"):
        pairs.extend(("repos", entry) for entry in repos
                     if is_removed(removed, "repos", entry))
    if not pairs:
        return 0
    if precheck:
        jar_index = precheck.get("jar_index")
        workers = precheck["workers"]
        timeout = precheck["timeout"]
    else:
        jar_index = class_index(load_jarscan())
        workers = env_int("CHECK_WORKERS", 12)
        timeout = env_float("CHECK_TIMEOUT", 8)
    passed = retry_passed(pairs, timeout=timeout, workers=workers, jar_index=jar_index)
    for group in ("sites", "repos"):
        keys = {key for grp, key in passed if grp == group}
        if keys:
            removed[group] = [value for value in removed.get(group) or []
                              if str(value).strip().casefold() not in keys]
    return len(passed)


def merge_feed_payload(db, payload, origin, stats, precheck=None, removed=None):
    sites, repos = extract_entries(payload)
    if removed:
        sites = [entry for entry in sites if not is_removed(removed, "sites", entry)]
        repos = [entry for entry in repos if not is_removed(removed, "repos", entry)]
    reject_sites = precheck_candidates(db, "sites", sites, precheck, stats) if precheck else None
    upsert_items(db, "sites", sites, origin, stats, reject=reject_sites)
    if repos:
        reject_repos = precheck_candidates(db, "repos", repos, precheck, stats) if precheck else None
        upsert_items(db, "repos", repos, origin, stats, reject=reject_repos)
    parses = payload.get("parses") if isinstance(payload, dict) else None
    if isinstance(parses, list):
        reject_parses = precheck_parses(db, parses, precheck, stats) if precheck else None
        upsert_parses(db, parses, origin, stats, reject=reject_parses)


def apply_own_sources(db, stats, removed=None):
    data = load_json(OWN_PATH, {})
    if not isinstance(data, dict):
        return
    for group in ("sites", "caiji", "repos"):
        entries = data.get(group)
        if isinstance(entries, list) and entries:
            if removed:
                entries = [entry for entry in entries if not is_removed(removed, group, entry)]
            upsert_items(db, group, entries, "own", stats)
    parses = data.get("parses")
    if isinstance(parses, list) and parses:
        upsert_parses(db, parses, "own", stats)


def main():
    parser = argparse.ArgumentParser(description="Merge external feed configs + own sources into sources.json")
    parser.add_argument("--no-precheck", action="store_true",
                        help="disable pre-filter: add all feed entries without testing")
    parser.add_argument("--only-pass", action="store_true",
                        help="strict mode: only add entries that fully pass (unverifiable items skipped)")
    parser.add_argument("--precheck-workers", type=int, default=env_int("CHECK_WORKERS", 12))
    parser.add_argument("--precheck-timeout", type=float, default=env_float("CHECK_TIMEOUT", 8))
    args = parser.parse_args()
    setup_stdout()

    db = load_json(DB_PATH)
    if db is None:
        print("sources.json not found")
        return 1
    stats = {"added": 0, "updated": 0, "rejected": 0}
    precheck = None
    if not args.no_precheck:
        scan = load_jarscan()
        precheck = {"workers": max(1, args.precheck_workers),
                    "timeout": max(1.0, args.precheck_timeout),
                    "strict": args.only_pass or strict_from_settings(),
                    "jar_index": class_index(scan)}
    feeds = load_json(FEEDS_PATH, {})
    removed = load_removed()
    total_restored = 0
    items = feeds.get("items") if isinstance(feeds, dict) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            name = (item.get("name") or item.get("url") or "?").strip()
            try:
                payload = fetch_feed_json(item)
            except Exception as error:
                print("[feeds] %s failed: %s" % (name, error))
                continue
            if payload is None:
                continue
            restored = 0
            if removed:
                restored = retry_from_payload(payload, removed, precheck)
                total_restored += restored
            before = dict(stats)
            merge_feed_payload(db, payload, "feed", stats, precheck, removed)
            print("[feeds] %s: added=%d updated=%d rejected=%d restored=%d"
                  % (name, stats["added"] - before["added"],
                     stats["updated"] - before["updated"],
                     stats["rejected"] - before["rejected"], restored))
    apply_own_sources(db, stats, removed)
    if total_restored:
        save_removed(removed)
        print("[feeds] tombstone restored: %d" % total_restored)
    if stats["added"] or stats["updated"] or stats["rejected"]:
        save_json(DB_PATH, db)
        print("[feeds] saved sources.json (added=%d updated=%d rejected=%d)"
              % (stats["added"], stats["updated"], stats["rejected"]))
    else:
        print("[feeds] no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
