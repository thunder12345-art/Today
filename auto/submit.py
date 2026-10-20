import argparse
import json
import os
import time
import types

from common import (DB_PATH, INBOX_DIR, check_entry, check_repo, coerce_site,
                    derive_targets, extract_entries, find_duplicate, http_fetch,
                    item_key, load_json, norm_key, norm_target, now_iso,
                    save_json, setup_stdout)
from jarscan import load_jarscan, short_name


def parse_payload(text):
    data = json.loads(text.lstrip("\ufeff"))
    return extract_entries(data)


def fetch_payload(url, timeout):
    _, body = http_fetch(url, timeout=timeout)
    text = body.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
    return parse_payload(text)


def status_text(check):
    state = check.get("ok")
    if state is True:
        return "OK %sms" % check.get("latency_ms")
    if state is False:
        return "FAIL %s" % (check.get("detail") or check.get("code"))
    return "SKIP %s" % check.get("detail", "")


def process(payloads, db, caiji=False, allow_dup=False):
    db.setdefault("sites", [])
    db.setdefault("repos", [])
    db.setdefault("caiji", [])

    known_keys = set()
    known_targets = set()
    known_repos = set()
    for group in ("sites", "caiji"):
        for item in db.get(group, []):
            key = norm_key(item)
            if key:
                known_keys.add(key)
            for target in derive_targets(item):
                known_targets.add(norm_target(target))
    for item in db.get("repos", []):
        if isinstance(item, dict) and item.get("url"):
            known_repos.add(item["url"].strip())

    added_sites = []
    added_repos = []
    duplicates = []
    errors = []

    for origin, sites, repos in payloads:
        for repo in repos:
            url = repo["url"].strip()
            if url in known_repos:
                duplicates.append((origin, url, "repo already in db"))
                continue
            repo["_meta"] = {"origin": origin, "added_at": now_iso()}
            db["repos"].append(repo)
            known_repos.add(url)
            added_repos.append(repo)
        for raw in sites:
            site, warning = coerce_site(raw)
            if site is None:
                label = raw.get("name") or raw.get("key") or "?"
                errors.append((origin, str(label), warning))
                continue
            key = norm_key(site)
            targets = derive_targets(site)
            if not allow_dup:
                dup = find_duplicate(db, site)
                if dup:
                    _, existing, reason = dup
                    duplicates.append((origin, site.get("name", "?"),
                                       "%s -> existing <%s>" % (reason, existing.get("name") or existing.get("url") or existing.get("key", "?"))))
                    continue
                if key in known_keys or (targets and any(norm_target(item) in known_targets for item in targets)):
                    duplicates.append((origin, site.get("name", "?"), "duplicate within this submission"))
                    continue
            group = "caiji" if caiji else "sites"
            meta = {"origin": origin, "added_at": now_iso(), "targets": targets}
            if warning:
                meta["notes"] = warning
            db[group].append(dict(site, _meta=meta))
            known_keys.add(key)
            for target in targets:
                known_targets.add(norm_target(target))
            added_sites.append((db[group][-1], group))

    return added_sites, added_repos, duplicates, errors


def finalize_added(db, added_sites, added_repos, timeout=8, do_check=True):
    if not (added_sites or added_repos):
        return []
    scan = load_jarscan()
    jar_names = {short_name(descriptor).lower() for descriptor in scan.get("classes", [])}
    results = []
    for item, group in added_sites:
        api = item.get("api")
        missing = (isinstance(api, str) and api.startswith("csp_")
                   and api[4:].lower() not in jar_names)
        if missing:
            item["_meta"]["class_missing"] = True
        check = None
        if do_check:
            check = check_entry(item, timeout=timeout)
            item["_meta"]["last_check"] = check
        results.append({"group": group, "name": item.get("name", "?"),
                        "key": item_key(item), "api": api or "",
                        "check": check, "missing": missing,
                        "notes": item["_meta"].get("notes")})
    for repo in added_repos:
        check = None
        if do_check:
            check = check_repo(repo, timeout=timeout)
            repo["_meta"]["last_check"] = check
        results.append({"group": "repos", "name": repo.get("name") or repo.get("url"),
                        "key": item_key(repo), "api": repo.get("url") or "",
                        "check": check, "missing": False, "notes": None})
    return results


def submit_payloads(payloads, caiji=False, allow_dup=False, timeout=8, do_check=True):
    db = load_json(DB_PATH)
    if db is None:
        return {"error": "sources.json not found, run first: python auto/seed.py"}
    added_sites, added_repos, duplicates, errors = process(payloads, db, caiji=caiji,
                                                           allow_dup=allow_dup)
    added = finalize_added(db, added_sites, added_repos, timeout=timeout, do_check=do_check)
    db["updated"] = now_iso()
    save_json(DB_PATH, db)
    return {"added": added,
            "duplicates": [{"origin": origin, "label": label, "reason": reason}
                           for origin, label, reason in duplicates],
            "errors": [{"origin": origin, "label": label, "reason": reason}
                       for origin, label, reason in errors]}


def main():
    parser = argparse.ArgumentParser(description="Submit new sources into auto/sources.json")
    parser.add_argument("--file", action="append", default=[], help="JSON file: full config / single site / array of sites")
    parser.add_argument("--json", action="append", default=[], help="inline JSON string")
    parser.add_argument("--url", action="append", default=[], help="remote config URL to import")
    parser.add_argument("--inbox", action="store_true", help="process all auto/inbox/*.json")
    parser.add_argument("--caiji", action="store_true", help="put new sites into the caiji group")
    parser.add_argument("--no-check", action="store_true", help="skip immediate health check")
    parser.add_argument("--allow-dup", action="store_true", help="add even when a duplicate is detected")
    parser.add_argument("--timeout", type=float, default=8)
    args = parser.parse_args()
    setup_stdout()

    payloads = []
    inbox_files = []

    if args.inbox:
        os.makedirs(INBOX_DIR, exist_ok=True)
        for name in sorted(os.listdir(INBOX_DIR)):
            if not name.lower().endswith(".json") or name.startswith("_"):
                continue
            path = os.path.join(INBOX_DIR, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    sites, repos = parse_payload(handle.read())
                payloads.append(("inbox:%s" % name, sites, repos))
                inbox_files.append(path)
            except Exception as error:
                print("[ERROR] inbox/%s: %s" % (name, error))

    for path in args.file:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                sites, repos = parse_payload(handle.read())
            payloads.append(("file:%s" % os.path.basename(path), sites, repos))
        except Exception as error:
            print("[ERROR] %s: %s" % (path, error))

    for text in args.json:
        try:
            sites, repos = parse_payload(text)
            payloads.append(("cli", sites, repos))
        except Exception as error:
            print("[ERROR] inline json: %s" % error)

    for url in args.url:
        try:
            sites, repos = fetch_payload(url, args.timeout)
            payloads.append(("url:%s" % url, sites, repos))
        except Exception as error:
            print("[ERROR] %s: %s" % (url, error))

    if not payloads:
        print("nothing to submit: use --file / --json / --url / --inbox")
        return

    result = submit_payloads(payloads, caiji=args.caiji, allow_dup=args.allow_dup,
                             timeout=args.timeout, do_check=not args.no_check)
    if result.get("error"):
        print(result["error"])
        return

    added_sites = 0
    added_repos = 0
    for item in result["added"]:
        if item["group"] == "repos":
            added_repos += 1
        else:
            added_sites += 1
        if item.get("check") is not None:
            print("[ADDED %s] %-28s %s" % (item["group"], item["name"], status_text(item["check"])))
        else:
            print("[ADDED %s] %-28s" % (item["group"], item["name"]))
        if item.get("notes"):
            print("    note: %s" % item["notes"])
        if item.get("missing"):
            print("    WARN: class %s not found in tvbox.jar" % item.get("api"))

    for dup in result["duplicates"]:
        print("[DUPLICATE] %-28s %s (%s)" % (dup["label"], dup["reason"], dup["origin"]))
    for err in result["errors"]:
        print("[ERROR]     %-28s %s (%s)" % (err["label"], err["reason"], err["origin"]))

    if inbox_files:
        done_dir = os.path.join(INBOX_DIR, "done")
        os.makedirs(done_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for path in inbox_files:
            os.replace(path, os.path.join(done_dir, "%s_%s" % (stamp, os.path.basename(path))))

    print("")
    print("added: sites=%d repos=%d | duplicates=%d | errors=%d | db: %s"
          % (added_sites, added_repos, len(result["duplicates"]), len(result["errors"]), DB_PATH))


if __name__ == "__main__":
    main()
