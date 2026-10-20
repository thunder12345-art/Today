import argparse
import os

from common import (DB_PATH, OUT_DIR, ROOT, is_whitelisted, load_json,
                    load_whitelist, save_json, setup_stdout, strip_meta)


def collect_entries(db, group, scope, drop_dead, whitelist):
    result = []
    excluded = 0
    for item in db.get(group, []):
        meta = item.get("_meta") or {}
        origin = meta.get("origin")
        if scope == "local" and origin not in (None, "local"):
            continue
        if meta.get("blocked"):
            excluded += 1
            continue
        if drop_dead and (meta.get("last_check") or {}).get("ok") is False \
                and not is_whitelisted(whitelist, group, item):
            excluded += 1
            continue
        result.append(strip_meta(item))
    return result, excluded


def main():
    parser = argparse.ArgumentParser(description="Regenerate tvbox.json / lmw.json / caiji.json from auto/sources.json")
    parser.add_argument("--out", default=OUT_DIR, help="output directory (default auto/out)")
    parser.add_argument("--inplace", action="store_true", help="overwrite the repo files directly")
    parser.add_argument("--scope", choices=["local", "all"], default="local",
                        help="local = repo originals only, all = include submitted entries")
    parser.add_argument("--drop-dead", action="store_true", help="exclude entries whose last check failed")
    args = parser.parse_args()
    setup_stdout()

    db = load_json(DB_PATH)
    if db is None:
        print("sources.json not found, run first: python auto/seed.py")
        return

    whitelist = load_whitelist()

    tvbox = load_json(os.path.join(ROOT, "tvbox.json"), {})
    caiji = load_json(os.path.join(ROOT, "caiji.json"), {})

    sites, blocked_sites = collect_entries(db, "sites", args.scope, args.drop_dead, whitelist)
    caiji_sites, blocked_caiji = collect_entries(db, "caiji", args.scope, args.drop_dead, whitelist)
    tvbox["sites"] = sites
    caiji["sites"] = caiji_sites

    blocked_parses = set(db.get("blocked_parses") or [])
    parses = tvbox.get("parses") or []
    if blocked_parses:
        tvbox["parses"] = [item for item in parses
                           if not (isinstance(item, dict) and item.get("url") in blocked_parses)]
    parses_excluded = len(parses) - len(tvbox.get("parses") or [])

    repos = []
    blocked_repos = 0
    for item in db.get("repos", []):
        meta = item.get("_meta") or {}
        origin = meta.get("origin")
        if args.scope == "local" and origin not in (None, "local"):
            continue
        if meta.get("blocked"):
            blocked_repos += 1
            continue
        if args.drop_dead and (meta.get("last_check") or {}).get("ok") is False \
                and not is_whitelisted(whitelist, "repos", item):
            blocked_repos += 1
            continue
        repos.append({"url": item.get("url", ""), "name": item.get("name", "")})
    lmw = {"urls": repos}

    out_dir = ROOT if args.inplace else args.out
    os.makedirs(out_dir, exist_ok=True)
    save_json(os.path.join(out_dir, "tvbox.json"), tvbox)
    save_json(os.path.join(out_dir, "caiji.json"), caiji)
    save_json(os.path.join(out_dir, "lmw.json"), lmw)

    print("built -> %s" % out_dir)
    print("tvbox.json sites=%d | caiji.json sites=%d | lmw.json urls=%d"
          % (len(tvbox["sites"]), len(caiji["sites"]), len(repos)))
    if blocked_sites or blocked_caiji or blocked_repos or parses_excluded:
        print("excluded: sites=%d caiji=%d repos=%d parses=%d (blocked / drop-dead)"
              % (blocked_sites, blocked_caiji, blocked_repos, parses_excluded))


if __name__ == "__main__":
    main()
