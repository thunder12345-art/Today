#!/bin/sh
set -e

cd "$(dirname "$0")/.."

BUNDLE="${1:-/mnt/sata1-2/docker/update.bundle}"
PY="${PYTHON:-python3}"
TS="$(date +%Y%m%d-%H%M%S)"

echo "== [1/7] stop container (freeze runtime files) =="
docker stop tomorrow-auto >/dev/null 2>&1 || true

echo "== [2/7] snapshot runtime state =="
git add -A
git commit -m "runtime snapshot $TS" >/dev/null 2>&1 || echo "   (nothing to commit)"
cp auto/sources.json auto/.upd_runtime.json
if [ -f auto/whitelist.json ]; then
    cp auto/whitelist.json auto/.upd_whitelist.json
fi
echo "   saved auto/.upd_runtime.json"

echo "== [3/7] fetch bundle: $BUNDLE =="
if [ ! -f "$BUNDLE" ]; then
    echo "bundle not found: $BUNDLE"
    docker start tomorrow-auto >/dev/null 2>&1 || true
    exit 1
fi
git fetch "$BUNDLE" "+refs/heads/main:refs/heads/incoming"
git merge --no-commit -X theirs incoming
if git ls-files -u | grep -q .; then
    echo "unresolved conflicts, aborting merge:"
    git diff --name-only --diff-filter=U
    git merge --abort || true
    docker start tomorrow-auto >/dev/null 2>&1 || true
    exit 1
fi

echo "== [4/7] patch tvbox.jar (idempotent) =="
$PY auto/patch_jar.py --jar tvbox.jar || true

echo "== [5/7] merge runtime metadata into sources.json =="
$PY auto/merge_runtime.py --runtime auto/.upd_runtime.json \
    --whitelist auto/.upd_whitelist.json

echo "== [6/7] commit + push =="
git add -A
git commit -m "sync upstream $TS" || echo "   (nothing to commit)"
git push origin main || echo "   !! push failed (check credentials), local commit is safe"

echo "== [7/7] start container =="
docker start tomorrow-auto

echo ""
echo "done. panel:  http://<nas-ip>:8899/panel"
echo "      check:  docker logs --tail 30 tomorrow-auto"
