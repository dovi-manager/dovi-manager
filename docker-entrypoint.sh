#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID:$PGID" in
  *[!0-9:]*|:*|*:) echo "PUID and PGID must be positive numeric IDs" >&2; exit 1 ;;
esac
if [ "$PUID" -eq 0 ] || [ "$PGID" -eq 0 ]; then
  echo "PUID and PGID must identify an unprivileged user and group" >&2
  exit 1
fi

export HOME=/tmp
export XDG_CACHE_HOME=/tmp/.cache

exec gosu "${PUID}:${PGID}" \
  /opt/dovi-manager/.venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
