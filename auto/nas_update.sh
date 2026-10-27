#!/bin/sh
set -e

cd "$(dirname "$0")/.."

BUNDLE="${1:-/mnt/sata1-2/docker/update.bundle}"
PY="${PYTHON:-python3}"
IMAGE="${IMAGE:-tomorrow-auto:latest}"
TS="$(date +%Y%m%d-%H%M%S)"
export GIT_TERMINAL_PROMPT=0

run_py() {
    script="$1"
    shift
    if command -v "$PY" >/dev/null 2>&1; then
        "$PY" "$script" "$@"
    else
        echo "   (host $PY not found, using container image: $IMAGE)"
        docker run --rm -v "$PWD:/app" -w /app "$IMAGE" python "$script" "$@"
    fi
}

echo "== [0/8] ensure git identity =="
git config user.email >/dev/null 2>&1 || git config user.email "nas@local"
git config user.name >/dev/null 2>&1 || git config user.name "iStoreOS NAS"
echo "   identity: $(git config user.name) <$(git config user.email)>"

echo "== [1/8] stop container (freeze runtime files) =="
docker stop tomorrow-auto >/dev/null 2>&1 || true

echo "== [2/8] snapshot runtime state =="
git add -A
if git diff --cached --quiet; then
    echo "   (nothing to commit)"
else
    git commit -m "runtime snapshot $TS"
fi
cp auto/sources.json auto/.upd_runtime.json
if [ -f auto/whitelist.json ]; then
    cp auto/whitelist.json auto/.upd_whitelist.json
fi
echo "   saved auto/.upd_runtime.json"

echo "== [3/8] fetch bundle: $BUNDLE =="
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

echo "== [4/8] patch tvbox.jar (idempotent) =="
run_py auto/patch_jar.py --jar tvbox.jar || true

echo "== [5/8] merge runtime metadata into sources.json =="
run_py auto/merge_runtime.py --runtime auto/.upd_runtime.json \
    --whitelist auto/.upd_whitelist.json

echo "== [6/8] commit + push =="
git add -A
if git diff --cached --quiet; then
    echo "   (nothing to commit)"
else
    git commit -m "sync upstream $TS"
fi
git push origin main || echo "   !! push failed (check credentials), local commit is safe"
if git remote get-url gitee >/dev/null 2>&1; then
    git push gitee main || echo "   !! gitee push failed (check credentials/remote), local commit is safe"
else
    echo "   (gitee remote not set; enable once: git remote add gitee https://gitee.com/<user>/<repo>.git)"
fi

echo "== [7/8] publish release (bundle + image + readme) =="
if [ "${RELEASE_PUBLISH:-1}" = "1" ]; then
    repo_slug() {
        echo "$1" | sed -e 's#^git@[^:]*:##' -e 's#^[a-z]*://[^/]*/##' -e 's#\.git$##'
    }
    ORIGIN_SLUG=$(repo_slug "$(git remote get-url origin 2>/dev/null || true)")
    GITEE_SLUG=$(repo_slug "$(git remote get-url gitee 2>/dev/null || true)")
    REL_DIR=$(dirname "$BUNDLE")
    RSTATE="$REL_DIR/.release_state.json"
    IMAGE_TAR="$REL_DIR/tomorrow-auto-image.tar.gz"
    CREDS="/root/.git-credentials"
    IMAGE_ARGS=""
    CUR_ID=$(docker inspect -f '{{.Id}}' tomorrow-auto:latest 2>/dev/null || true)
    LAST_ID=$(sed -n 's/.*"image_id": *"\([^"]*\)".*/\1/p' "$RSTATE" 2>/dev/null | head -n 1)
    if [ -n "$CUR_ID" ]; then
        if [ "$CUR_ID" != "$LAST_ID" ]; then
            echo "   image changed -> saving to $IMAGE_TAR"
            if docker save tomorrow-auto:latest | gzip > "$IMAGE_TAR"; then
                IMAGE_ARGS="--image $IMAGE_TAR --image-id $CUR_ID"
            else
                echo "   !! docker save failed, image asset skipped"
                rm -f "$IMAGE_TAR"
            fi
        else
            echo "   image unchanged, skip image asset"
        fi
    fi
    if command -v "$PY" >/dev/null 2>&1; then
        "$PY" auto/publish_release.py --tag "v$TS" --bundle "$BUNDLE" --readme README.md \
            --state "$RSTATE" --github "$ORIGIN_SLUG" --gitee "$GITEE_SLUG" --cred-file "$CREDS" \
            $IMAGE_ARGS || echo "   !! release publish had errors (deploy continues)"
    else
        docker run --rm -v "$PWD:/app" -v "/root:/hostroot:ro" -w /app "$IMAGE" python auto/publish_release.py \
            --tag "v$TS" --bundle "$BUNDLE" --readme README.md --state "$RSTATE" \
            --github "$ORIGIN_SLUG" --gitee "$GITEE_SLUG" --cred-file /hostroot/.git-credentials \
            $IMAGE_ARGS || echo "   !! release publish had errors (deploy continues)"
    fi
    rm -f "$IMAGE_TAR"
else
    echo "   (skipped: RELEASE_PUBLISH != 1)"
fi

echo "== [8/8] start container =="
docker start tomorrow-auto

echo ""
echo "done. panel:  http://<nas-ip>:8899/panel"
echo "      check:  docker logs --tail 30 tomorrow-auto"
