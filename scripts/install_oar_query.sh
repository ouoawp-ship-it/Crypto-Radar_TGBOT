#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-/home/ubuntu/paopao-crypto-radar}"
UNIT_NAME="${OAR_QUERY_SERVICE_NAME:-paopao-oar-query}"
UNIT_SOURCE="${APP_DIR}/ops/systemd/${UNIT_NAME}.service"
RUNNER_SOURCE="${APP_DIR}/scripts/run_oar_query.sh"
SYSTEMD_DIR="${PAOPAO_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_TARGET="${SYSTEMD_DIR}/${UNIT_NAME}.service"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

assert_no_conflicting_worker() {
  local main_pid matches line pid
  main_pid="$(systemctl show "$UNIT_NAME" --property MainPID --value 2>/dev/null || true)"
  [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || main_pid="0"
  matches="$(pgrep -af '[o]nchain_main.py.*telegram-query-live' 2>/dev/null || true)"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pid="${line%% *}"
    if [ "$pid" != "$main_pid" ]; then
      printf 'duplicate_telegram_query_worker_risk\n' >&2
      return 1
    fi
  done <<<"$matches"
}

command -v systemctl >/dev/null 2>&1 || fail "systemd_required"
[ -d "$APP_DIR" ] || fail "project_directory_missing"
[ -x "${APP_DIR}/.venv/bin/python" ] || fail "project_venv_missing"
[ -f "${APP_DIR}/.env.onchain" ] || fail "onchain_env_missing"
[ -f "${APP_DIR}/.env.oi" ] || fail "shared_env_missing"
[ "$(stat -c '%a' "${APP_DIR}/.env.onchain")" = "600" ] || fail "onchain_env_permissions_must_be_600"
[ "$(stat -c '%a' "${APP_DIR}/.env.oi")" = "600" ] || fail "shared_env_permissions_must_be_600"
[ -f "$UNIT_SOURCE" ] || fail "oar_query_unit_source_missing"
[ -x "$RUNNER_SOURCE" ] || fail "oar_query_runner_missing_or_not_executable"
assert_no_conflicting_worker || exit 1

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_root mkdir -p "$SYSTEMD_DIR"
if run_root test -f "$UNIT_TARGET"; then
  run_root cp --preserve=mode,ownership,timestamps \
    "$UNIT_TARGET" "${UNIT_TARGET}.bak.${timestamp}"
fi
run_root install -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
run_root systemctl daemon-reload
printf 'installed=%s\nservice_started=false\n' "$UNIT_TARGET"
