import json
import os
import sys

from common import (BASE, DB_PATH, build_meta, derive_targets, extract_entries,
                    http_fetch, item_key, load_json, now_iso, save_json,
                    setup_stdout, strip_meta)

FEEDS_PATH = os.path.join(BASE, "feeds.json")
OWN_PATH = os.path.join(BASE, "own_sources.json")
MAX_FEED_BYTES = 4 * 1048576


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


def upsert_items(db, group, entries, origin, stats):
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


def upsert_parses(db, parses, origin, stats):
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


def merge_feed_payload(db, payload, origin, stats):
    sites, repos = extract_entries(payload)
    upsert_items(db, "sites", sites, origin, stats)
    if repos:
        upsert_items(db, "repos", repos, origin, stats)
    parses = payload.get("parses") if isinstance(payload, dict) else None
    if isinstance(parses, list):
        upsert_parses(db, parses, origin, stats)


def apply_own_sources(db, stats):
    data = load_json(OWN_PATH, {})
    if not isinstance(data, dict):
        return
    for group in ("sites", "caiji", "repos"):
        entries = data.get(group)
        if isinstance(entries, list) and entries:
            upsert_items(db, group, entries, "own", stats)
    parses = data.get("parses")
    if isinstance(parses, list) and parses:
        upsert_parses(db, parses, "own", stats)


def main():
    setup_stdout()
    db = load_json(DB_PATH)
    if db is None:
        print("sources.json not found")
        return 1
    stats = {"added": 0, "updated": 0}
    feeds = load_json(FEEDS_PATH, {})
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
            before = dict(stats)
            merge_feed_payload(db, payload, "feed", stats)
            print("[feeds] %s: added=%d updated=%d"
                  % (name, stats["added"] - before["added"], stats["updated"] - before["updated"]))
    apply_own_sources(db, stats)
    if stats["added"] or stats["updated"]:
        save_json(DB_PATH, db)
        print("[feeds] saved sources.json (added=%d updated=%d)" % (stats["added"], stats["updated"]))
    else:
        print("[feeds] no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
