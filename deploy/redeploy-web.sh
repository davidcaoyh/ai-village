#!/usr/bin/env bash
# Push only the spectator page. Safe while a session is running: the page is a
# static file the session process never reads, and server/main.py serves it with
# FileResponse on every request, so nothing needs restarting.
#   bash deploy/redeploy-web.sh root@143.198.1.2
#
# Deliberately not `rsync -a`: -o -g -p preserve the laptop's uid and mode, and
# a file owned by your mac uid with no group-read bit is one uvicorn runs as
# `village` and cannot open - a 500 on / while every /api route stays healthy.
set -euo pipefail
TARGET="${1:?usage: redeploy-web.sh user@host}"
HOST="${TARGET#*@}"

rsync -z --no-owner --no-group --no-perms web/index.html "$TARGET:/opt/village/web/index.html"
ssh "$TARGET" 'chown village:village /opt/village/web/index.html &&
               chmod 644 /opt/village/web/index.html &&
               ls -l /opt/village/web/index.html'

code=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/")
echo "GET / -> $code"
[ "$code" = "200" ] || { echo "page is not serving; check: journalctl -u village-web -n 30"; exit 1; }
