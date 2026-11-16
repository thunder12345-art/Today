#!/bin/sh
set -eu
curl -s -m 15 -H "Content-Type: application/json" \
    -d '{"groups":"all"}' http://127.0.0.1:8896/api/run
echo
