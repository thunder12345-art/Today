import argparse
import os
import shutil
import sys

from common import BASE, load_json, now_iso, save_json, setup_stdout
import fetch_upstream
import pull_upstream

UPSTREAMS_PATH = os.path.join(BASE, "upstreams.json")
STATE_PATH = os.path.join(BASE, ".upstream_state.json")
TREE_DIR = os.path.join(BASE, ".sync_tmp", "upstream")

DEFAULT_UPSTREAMS = {
    "items": [
        {"name": "tushen6/Tomorrow", "url": "https://github.com/tushen6/Tomorrow",
         "branch": "master", "ghproxy": True, "enabled": True},
    ]
}


def load_upstreams():
    data = load_json(UPSTREAMS_PATH)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"items": list(DEFAULT_UPSTREAMS["items"])}
    return data


def load_state():
    state = load_json(STATE_PATH, {})
    return state if isinstance(state, dict) else {}


def state_key(item):
    return (item.get("url") or item.get("name") or "?").strip()


def sync_one(item, state, force):
    url = fetch_upstream.archive_url(item.get("url"), item.get("branch"), item.get("ghproxy", True))
    info = state.setdefault(state_key(item), {})
    etag = fetch_upstream.head_etag(url)
    if etag and not force and etag == info.get("etag"):
        print("   up to date (etag %s)" % etag)
        return "skipped"
    shutil.rmtree(TREE_DIR, ignore_errors=True)
    fetch_upstream.download_tree(url, TREE_DIR)
    namespace = argparse.Namespace(apply=True, skip_patch=False, skip_sync=False)
    rc = pull_upstream.run_tree(TREE_DIR, namespace)
    if rc != 0:
        raise RuntimeError("pull_upstream exited with %d" % rc)
    if etag:
        info["etag"] = etag
    return "synced"


def main():
    parser = argparse.ArgumentParser(
        description="Sync all enabled upstream GitHub repos (tarball mode, no git required)")
    parser.add_argument("--only", default="", help="sync only the item with this name")
    parser.add_argument("--force", action="store_true", help="ignore stored etag (always sync)")
    args = parser.parse_args()
    setup_stdout()

    items = [item for item in load_upstreams().get("items", [])
             if isinstance(item, dict) and item.get("enabled", True)]
    if args.only:
        items = [item for item in items if item.get("name") == args.only]
        if not items:
            print("no enabled upstream named %r" % args.only)
            return 1
    if not items:
        print("no enabled upstreams configured")
        return 0

    state = load_state()
    rc = 0
    for item in items:
        print("== upstream: %s (%s) ==" % (item.get("name") or item.get("url"), item.get("url")))
        info = state.setdefault(state_key(item), {})
        try:
            result = sync_one(item, state, args.force)
            info["last_sync_at"] = now_iso()
            info["last_result"] = result
            print("   result: %s" % result)
        except Exception as error:
            info["last_sync_at"] = now_iso()
            info["last_result"] = "failed: %s" % error
            print("   FAILED: %s" % error)
            rc = 1
    save_json(STATE_PATH, state)
    print("state saved: %s" % STATE_PATH)
    return rc


if __name__ == "__main__":
    sys.exit(main())
