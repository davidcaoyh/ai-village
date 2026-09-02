#!/usr/bin/env bash
# Push code from your laptop to a box bootstrap.sh has already prepared.
#   bash deploy/redeploy.sh root@143.198.1.2
# Never touches /etc/village.env or runs/ - secrets and history stay on the box.
set -euo pipefail
TARGET="${1:?usage: redeploy.sh user@host}"
rsync -az --delete --exclude runs --exclude .venv --exclude .git --exclude _private \
      --exclude .env --exclude __pycache__ --exclude '*.egg-info' \
      --exclude .pytest_cache --exclude .ruff_cache --exclude .DS_Store \
      ./ "$TARGET:/opt/village/"
ssh "$TARGET" 'chown -R village:village /opt/village &&
               /opt/village/.venv/bin/pip install -q -e /opt/village &&
               systemctl restart village-web &&
               systemctl --no-pager -n 3 status village-web'
