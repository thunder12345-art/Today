#!/bin/sh
# 上游同步（手动/兜底用；日常推荐在面板"调度与范围"设置运行周期并勾选"每轮先自动同步上游"）。
# 如仍想用系统级定时兜底（可选，与面板调度不冲突，etag 检查幂等）：
#   30 5 * * * sh /mnt/sata1-2/docker/Tomorrow/auto/upstream_sync.sh >> /mnt/sata1-2/docker/Tomorrow/auto/upstream_sync.log 2>&1
# 同步列表在 auto/upstreams.json（也可以在面板"上游同步库"里可视化编辑）。
# 无更新时仅做一次 etag 检查直接退出；有更新时同步内容→提交→重启容器跑流水线。
set -e

cd "$(dirname "$0")/.."

CONTAINER="${CONTAINER:-tomorrow-auto}"
IMAGE="${IMAGE:-tomorrow-auto:latest}"
PY="${PYTHON:-python3}"
TS="$(date +%Y%m%d-%H%M%S)"
export GIT_TERMINAL_PROMPT=0
export PATH="/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

run_py() {
    script="$1"
    shift
    if command -v "$PY" >/dev/null 2>&1; then
        "$PY" "$script" "$@"
    elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
        docker exec "$CONTAINER" python "/app/$script" "$@"
    else
        docker run --rm -v "$PWD:/app" -w /app "$IMAGE" python "$script" "$@"
    fi
}

echo "== [0/4] ensure git identity =="
git config user.email >/dev/null 2>&1 || git config user.email "nas@local"
git config user.name >/dev/null 2>&1 || git config user.name "iStoreOS NAS"

echo "== [1/4] start container if needed =="
docker start "$CONTAINER" >/dev/null 2>&1 || true

echo "== [2/4] sync upstream repos (auto/upstreams.json) =="
run_py auto/upstream_sync.py

echo "== [3/4] commit =="
git add -A
if git diff --cached --quiet; then
    echo "   (no file changes)"
    echo "done: $(date '+%F %T')"
    exit 0
fi
git commit -m "sync upstream $TS"

echo "== [4/4] restart container (rebuild configs) =="
docker restart "$CONTAINER" >/dev/null
echo "done: $(date '+%F %T')"
