import argparse
import os

from common import (DB_PATH, ROOT, build_meta, derive_targets, item_key,
                    load_json, load_whitelist, norm_key, norm_target, now_iso,
                    save_json, save_whitelist, setup_stdout, strip_meta)

UPSTREAM_ORIGINS = (None, "local", "upstream")


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


def match_upstream(upstream_item, by_key, by_target, used):
    key = norm_key(upstream_item)
    if key and key in by_key and by_key[key] not in used:
        return by_key[key]
    for target in derive_targets(upstream_item):
        index = by_target.get(norm_target(target))
        if index is not None and index not in used:
            return index
    return None


def sync_group(db, group, upstream_items, dry_run, stats):
    items = db.get(group, [])
    by_key, by_target = index_items(items)
    used = set()
    result = []
    now = now_iso()
    for upstream in upstream_items:
        if not isinstance(upstream, dict):
            continue
        content = strip_meta(upstream)
        index = match_upstream(content, by_key, by_target, used)
        if index is not None:
            used.add(index)
            old = items[index]
            meta = dict(old.get("_meta") or {})
            meta["targets"] = derive_targets(content)
            meta.setdefault("origin", "local")
            meta["synced_at"] = now
            if strip_meta(old) != content:
                stats["updated"] += 1
            result.append(dict(content, _meta=meta))
        else:
            meta = build_meta(None, "local", derive_targets(content))
            meta["synced_at"] = now
            result.append(dict(content, _meta=meta))
            stats["added"] += 1
    for index, item in enumerate(items):
        if index in used:
            continue
        origin = (item.get("_meta") or {}).get("origin")
        if origin in UPSTREAM_ORIGINS:
            stats["removed"] += 1
            stats["removed_names"].append("%s:%s" % (group, item_key(item)))
            continue
        result.append(item)
    if not dry_run:
        db[group] = result
    stats["total"] = len(result)
    return result


def sync_repos(db, upstream_repos, dry_run, stats):
    items = db.get("repos", [])
    by_url = {}
    for index, item in enumerate(items):
        url = (item.get("url") or "").strip()
        if url:
            by_url.setdefault(norm_target(url), index)
    used = set()
    result = []
    now = now_iso()
    for upstream in upstream_repos:
        url = (upstream.get("url") or "").strip()
        if not url:
            continue
        index = by_url.get(norm_target(url))
        if index is not None and index not in used:
            used.add(index)
            old = items[index]
            meta = dict(old.get("_meta") or {})
            meta.setdefault("origin", "local")
            meta["synced_at"] = now
            content = {"url": url, "name": (upstream.get("name") or "").strip()}
            if strip_meta(old) != content:
                stats["updated"] += 1
            result.append(dict(content, _meta=meta))
        else:
            meta = build_meta(None, "local", None)
            meta["synced_at"] = now
            result.append({"url": url, "name": (upstream.get("name") or "").strip(), "_meta": meta})
            stats["added"] += 1
    for index, item in enumerate(items):
        if index in used:
            continue
        origin = (item.get("_meta") or {}).get("origin")
        if origin in UPSTREAM_ORIGINS:
            stats["removed"] += 1
            stats["removed_names"].append("repos:%s" % item_key(item))
            continue
        result.append(item)
    if not dry_run:
        db["repos"] = result
    stats["total"] = len(result)
    return result


def prune_whitelist(db):
    whitelist = load_whitelist()
    removed = []
    for group in ("sites", "caiji", "repos"):
        items = db.get(group) or []
        fields = set()
        for item in items:
            for field in ("key", "name", "url"):
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    fields.add(value.strip())
        keep = []
        for value in whitelist.get(group) or []:
            if value in fields:
                keep.append(value)
            else:
                removed.append("%s:%s" % (group, value))
        whitelist[group] = keep
    if removed:
        save_whitelist(whitelist)
    return removed


def run(dry_run, prune, db_path, root):
    db = load_json(db_path)
    if db is None:
        print("sources.json not found, run first: python auto/seed.py")
        return 1
    tvbox = load_json(os.path.join(root, "tvbox.json"), {})
    caiji = load_json(os.path.join(root, "caiji.json"), {})
    lmw = load_json(os.path.join(root, "lmw.json"), {})

    upstream_sites = [item for item in tvbox.get("sites", []) if isinstance(item, dict)]
    upstream_caiji = [item for item in caiji.get("sites", []) if isinstance(item, dict)]
    upstream_repos = []
    for item in lmw.get("urls", []):
        if isinstance(item, dict) and item.get("url"):
            upstream_repos.append(item)
        elif isinstance(item, str) and item.startswith(("http://", "https://")):
            upstream_repos.append({"url": item, "name": ""})

    stats = {"updated": 0, "added": 0, "removed": 0, "removed_names": []}
    print("== sync sites (upstream=%d, db=%d) ==" % (len(upstream_sites), len(db.get("sites", []))))
    sync_group(db, "sites", upstream_sites, dry_run, stats)
    print("   updated=%d added=%d removed=%d total=%d" % (stats["updated"], stats["added"],
                                                           stats["removed"], stats["total"]))
    stats.update({"updated": 0, "added": 0, "removed": 0})
    print("== sync caiji (upstream=%d, db=%d) ==" % (len(upstream_caiji), len(db.get("caiji", []))))
    sync_group(db, "caiji", upstream_caiji, dry_run, stats)
    print("   updated=%d added=%d removed=%d total=%d" % (stats["updated"], stats["added"],
                                                           stats["removed"], stats["total"]))
    stats.update({"updated": 0, "added": 0, "removed": 0})
    print("== sync repos (upstream=%d, db=%d) ==" % (len(upstream_repos), len(db.get("repos", []))))
    sync_repos(db, upstream_repos, dry_run, stats)
    print("   updated=%d added=%d removed=%d total=%d" % (stats["updated"], stats["added"],
                                                           stats["removed"], stats["total"]))

    removed_origins = []
    for item in db.get("sites", []) + db.get("caiji", []) + db.get("repos", []):
        origin = (item.get("_meta") or {}).get("origin")
        if origin not in UPSTREAM_ORIGINS and origin not in removed_origins:
            removed_origins.append(origin)
    if removed_origins:
        print("kept non-upstream entries with origins: %s" % ", ".join(sorted(str(o) for o in removed_origins)))

    if prune and not dry_run:
        removed = prune_whitelist(db)
        if removed:
            print("whitelist pruned: %s" % ", ".join(removed))
        else:
            print("whitelist: nothing to prune")

    if not dry_run:
        db["updated"] = now_iso()
        save_json(db_path, db)
        print("saved %s" % db_path)
    else:
        print("dry-run: nothing written")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Reconcile upstream config files into auto/sources.json")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prune-whitelist", action="store_true")
    args = parser.parse_args()
    setup_stdout()
    raise SystemExit(run(args.dry_run, args.prune_whitelist, args.db, args.root))


if __name__ == "__main__":
    main()
