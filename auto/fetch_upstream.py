import argparse
import io
import os
import sys
import tarfile
import urllib.error
import urllib.request

from common import BASE, setup_stdout

UA = "tomorrow-auto/1.0"
DEFAULT_BRANCH = "master"
GHPROXY_PREFIX = "https://gh-proxy.com/"


def normalize_repo(url):
    url = (url or "").strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def archive_url(repo_url, branch, ghproxy):
    base = normalize_repo(repo_url)
    if not base:
        raise ValueError("empty repo url")
    if ghproxy and "gh-proxy.com" not in base:
        base = GHPROXY_PREFIX + base
    return "%s/archive/refs/heads/%s.tar.gz" % (base, (branch or DEFAULT_BRANCH).strip())


def head_etag(url, timeout=30):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.headers.get("ETag") or "").strip().strip('"')
    except Exception as error:
        print("   (etag check failed: %s)" % error)
        return ""


def download_tree(url, out_dir, timeout=180):
    print("   downloading %s" % url)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    print("   downloaded %.1f MB" % (len(data) / 1048576.0))
    os.makedirs(out_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            member.name = parts[1]
            members.append(member)
        try:
            archive.extractall(out_dir, members=members, filter="data")
        except TypeError:
            archive.extractall(out_dir, members=members)
    return out_dir


def main():
    parser = argparse.ArgumentParser(description="Fetch an upstream repo tarball into a tree dir (no git needed)")
    parser.add_argument("--url", required=True, help="repo url, e.g. https://github.com/owner/repo")
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--no-ghproxy", action="store_true", help="fetch directly from github.com")
    parser.add_argument("--out", default=os.path.join(BASE, ".sync_tmp", "upstream"))
    parser.add_argument("--etag-only", action="store_true", help="only print the remote etag (for change detection)")
    args = parser.parse_args()
    setup_stdout()

    url = archive_url(args.url, args.branch, not args.no_ghproxy)
    if args.etag_only:
        etag = head_etag(url)
        if not etag:
            return 1
        print(etag)
        return 0
    etag = head_etag(url)
    download_tree(url, args.out)
    print("etag: %s" % etag)
    print("tree: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
