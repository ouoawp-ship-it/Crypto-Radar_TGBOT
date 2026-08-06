#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"
LOCK_FILE="${TG_PRIVATE_CONTROL_LOCK_FILE:-/run/paopao-private-control/worker.lock}"

if [[ "${TG_PRIVATE_CONTROL_ENABLE:-false}" != "true" ]]; then
  printf 'private_control_disabled\n' >&2
  exit 2
fi

if [[ -z "${TG_BOT_TOKEN:-}" || \
      ! "${TG_PRIVATE_CONTROL_ADMIN_USER_ID:-}" =~ ^[1-9][0-9]{0,18}$ ]]; then
  printf 'private_control_gate_blocked\n' >&2
  exit 2
fi

if ! command -v flock >/dev/null 2>&1; then
  printf 'private_control_lock_unavailable\n' >&2
  exit 2
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  printf 'private_control_worker_already_running\n' >&2
  exit 2
fi

args=("$PYTHON_BIN" "${APP_DIR}/main.py" "private-control")
exec "${args[@]}"
