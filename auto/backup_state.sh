#!/bin/sh
set -eu

ROOT=${ROOT:-/mnt/sata1-2/docker/Tomorrow}
BACKUP_DIR=${BACKUP_DIR:-/mnt/sata1-2/docker/backups}
KEY_FILE=${BACKUP_KEY_FILE:-/root/.backup_key}
KEEP=${BACKUP_KEEP:-14}
BACKUP_REPO=${BACKUP_REPO:-thunder12345-art/Today-backup}
BACKUP_GIT=${BACKUP_GIT:-1}

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
if [ ! -s "$KEY_FILE" ]; then
    umask 077
    head -c 32 /dev/urandom | openssl base64 -A > "$KEY_FILE"
fi
chmod 600 "$KEY_FILE"

TS=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
for path in auto/settings.json center/token.local.json auto/feeds.json \
            auto/upstreams.json auto/removed.json auto/whitelist.json \
            docker-compose.yml .env; do
    [ -f "$ROOT/$path" ] && cp "$ROOT/$path" "$TMP/$(echo "$path" | tr / _)"
done
tar -C "$TMP" -czf - . \
    | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$KEY_FILE" \
        -out "$BACKUP_DIR/state-$TS.enc"
chmod 600 "$BACKUP_DIR/state-$TS.enc"
cp "$BACKUP_DIR/state-$TS.enc" "$BACKUP_DIR/latest.enc"

ls -1t "$BACKUP_DIR"/state-*.enc 2>/dev/null \
    | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; done
ls -lh "$BACKUP_DIR/latest.enc"

if [ "$BACKUP_GIT" = "1" ] && [ -f /root/.git-credentials ]; then
    CRED=$(grep 'github.com' /root/.git-credentials | head -1 || true)
    TOKEN=$(printf '%s' "$CRED" | sed -n 's#https://[^:]*:\([^@]*\)@github.com#\1#p')
    if [ -n "$TOKEN" ]; then
        curl -s -m 15 -o /dev/null -H "Authorization: token $TOKEN" \
            -H "Accept: application/vnd.github+json" \
            -d "{\"name\":\"$(basename "$BACKUP_REPO")\",\"private\":true}" \
            https://api.github.com/user/repos || true
        cd "$BACKUP_DIR"
        [ -d .git ] || git init -q
        git config user.name "Tomorrow NAS" 2>/dev/null || true
        git config user.email "nas@local" 2>/dev/null || true
        git remote remove origin 2>/dev/null || true
        git remote add origin "https://$TOKEN@github.com/$BACKUP_REPO.git"
        git add latest.enc
        if ! git diff --cached --quiet; then
            git commit -q -m "backup $TS"
            for i in 1 2 3; do
                if git -c http.version=HTTP/1.1 push -q origin HEAD:main; then
                    echo "[backup] pushed to $BACKUP_REPO"
                    break
                fi
                echo "[backup] push retry $i failed"
                sleep 5
            done
        fi
    fi
fi
