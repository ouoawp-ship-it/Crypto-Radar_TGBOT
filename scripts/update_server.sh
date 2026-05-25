#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

cd "$APP_DIR"

git pull --ff-only

"${APP_DIR}/.venv/bin/pip" install -r requirements.txt
"${APP_DIR}/.venv/bin/python" -m py_compile \
  main.py \
  paopao_radar/cli.py \
  paopao_radar/config.py \
  paopao_radar/storage.py \
  paopao_radar/data_sources.py \
  paopao_radar/telegram.py \
  paopao_radar/radar.py \
  paopao_radar/maintenance.py
"${APP_DIR}/.venv/bin/python" -m unittest discover -s tests -v

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
  run_root systemctl restart "$SERVICE_NAME"
  run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
else
  printf 'systemd service not found; update completed without restart.\n'
fi
