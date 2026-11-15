#!/bin/sh
set -eu
TOKEN_FILE=${PANEL_TOKEN_FILE:-/root/.panel_token}
TOKEN=""
[ -s "$TOKEN_FILE" ] && TOKEN=$(cat "$TOKEN_FILE")
curl -s -m 15 -H "X-Panel-Token: $TOKEN" -H "Content-Type: application/json" \
    -d '{"groups":"all"}' http://127.0.0.1:8898/api/run
echo
