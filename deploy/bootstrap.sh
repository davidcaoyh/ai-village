#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 box. Run once, as root, from a clone:
#   git clone <repo> /tmp/village && bash /tmp/village/deploy/bootstrap.sh
# It installs no secrets. Stage 2 in docs/deploy.md is where keys go in.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/village
ENV_FILE=/etc/village.env

apt-get update -qq
apt-get install -y -qq python3-venv python3-pip rsync ufw curl \
  debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
fi

id village >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin village
mkdir -p "$APP/runs"
# .env is excluded on purpose. It is never in git, so a cloned $SRC cannot carry
# it - but a $SRC copied up with rsync can, and secrets belong only in
# /etc/village.env at mode 600, not in a directory the app user can read.
rsync -a --delete --exclude runs --exclude .venv --exclude .git --exclude _private \
      --exclude .env --exclude __pycache__ --exclude '*.egg-info' \
      --exclude .pytest_cache --exclude .ruff_cache --exclude .DS_Store \
      "$SRC"/ "$APP"/

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -e "$APP"
chown -R village:village "$APP"

# The admin token is generated here rather than left as a placeholder, because a
# default token on a public host is the same as no token (D28).
if [ ! -f "$ENV_FILE" ]; then
  sed "s/^VILLAGE_ADMIN_TOKEN=.*/VILLAGE_ADMIN_TOKEN=$(openssl rand -hex 24)/;
       s#^VILLAGE_DB_PATH=.*#VILLAGE_DB_PATH=$APP/runs/village.db#" \
      "$SRC/deploy/village.env.example" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

install -m644 "$SRC/deploy/village-web.service" /etc/systemd/system/
install -m644 "$SRC/deploy/village-session.service" /etc/systemd/system/
install -m644 "$SRC/deploy/village-session.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now village-web

ufw allow 22/tcp >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

cat <<TXT

bootstrap done. Remaining:
  1. nano $ENV_FILE                 add OPENROUTER_API_KEY (and TAVILY_API_KEY)
  2. systemctl restart village-web && curl -s localhost:8000/healthz
  3. edit the domain in $APP/deploy/Caddyfile, copy to /etc/caddy/Caddyfile, reload caddy
  4. sudo -u village $APP/.venv/bin/python -m scripts.preflight
  5. systemctl start village-session   (watch: journalctl -fu village-session)
  6. only after reading a transcript: systemctl enable --now village-session.timer
TXT
