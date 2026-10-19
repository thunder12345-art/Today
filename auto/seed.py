import argparse
import os

from common import (DB_PATH, ROOT, build_meta, derive_targets, load_json,
                    norm_key, now_iso, save_json, setup_stdout)


def main():
    parser = argparse.ArgumentParser(description="Bootstrap auto/sources.json from repo config files")
    parser.add_argument("--force", action="store_true", help="overwrite existing sources.json")
    args = parser.parse_args()
    setup_stdout()

    if os.path.exists(DB_PATH) and not args.force:
        print("sources.json already exists, use --force to rebuild it (manual entries will be dropped)")
        return

    tvbox = load_json(os.path.join(ROOT, "tvbox.json"), {})
    lmw = load_json(os.path.join(ROOT, "lmw.json"), {})
    caiji = load_json(os.path.join(ROOT, "caiji.json"), {})
    old = load_json(DB_PATH, {}) or {}

    old_meta = {}
    for group in ("sites", "caiji"):
        for item in old.get(group, []):
            key = norm_key(item)
            if key and isinstance(item.get("_meta"), dict):
                old_meta[key] = item["_meta"]

    old_repo_meta = {}
    for item in old.get("repos", []):
        if isinstance(item, dict) and item.get("url"):
            old_repo_meta[item["url"].strip()] = item.get("_meta")

    db = {"version": 1, "updated": now_iso(), "sites": [], "repos": [], "caiji": []}

    for site in tvbox.get("sites", []):
        if isinstance(site, dict):
            meta = build_meta(old_meta.get(norm_key(site)), "local", derive_targets(site))
            db["sites"].append(dict(site, _meta=meta))

    for site in caiji.get("sites", []):
        if isinstance(site, dict):
            meta = build_meta(old_meta.get(norm_key(site)), "local", derive_targets(site))
            db["caiji"].append(dict(site, _meta=meta))

    for item in lmw.get("urls", []):
        if isinstance(item, dict) and item.get("url"):
            url = item["url"].strip()
            meta = build_meta(old_repo_meta.get(url), "local")
            db["repos"].append({"url": url, "name": item.get("name", ""), "_meta": meta})

    save_json(DB_PATH, db)
    print("sources.json created: sites=%d repos=%d caiji=%d" % (len(db["sites"]), len(db["repos"]), len(db["caiji"])))


if __name__ == "__main__":
    main()
