#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-/home/ubuntu/paopao-crypto-radar}"
UNIT_NAME="${OAR_SERVICE_NAME:-paopao-oar-watch}"
UNIT_SOURCE="${APP_DIR}/ops/systemd/${UNIT_NAME}.service"
SYSTEMD_DIR="${PAOPAO_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_TARGET="${SYSTEMD_DIR}/${UNIT_NAME}.service"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

systemd_main_pid() {
  local main_pid
  main_pid="$(systemctl show "$UNIT_NAME" \
    --property MainPID --value 2>/dev/null || true)"
  if [[ "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s\n' "$main_pid"
  else
    printf '0\n'
  fi
}

assert_no_conflicting_writer() {
  local main_pid matches line pid
  main_pid="$(systemd_main_pid)"
  matches="$(pgrep -af '[o]nchain_main.py.*watch-live' 2>/dev/null || true)"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pid="${line%% *}"
    if [ "$pid" != "$main_pid" ]; then
      printf 'duplicate_writer_risk\n%s\n' "$line" >&2
      return 1
    fi
  done <<<"$matches"
}

validate_host() {
  command -v systemctl >/dev/null 2>&1 || fail "systemd_required"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "${ID:-}" = "ubuntu" ] || fail "ubuntu_required"
  else
    fail "ubuntu_required"
  fi
  [ -d "$APP_DIR" ] || fail "project_directory_missing"
  [ -x "${APP_DIR}/.venv/bin/python" ] || fail "project_venv_missing"
  [ -f "${APP_DIR}/.env.onchain" ] || fail "onchain_env_missing"
  [ "$(stat -c '%a' "${APP_DIR}/.env.onchain")" = "600" ] || \
    fail "onchain_env_permissions_must_be_600"
  [ -f "$UNIT_SOURCE" ] || fail "oar_unit_source_missing"
  assert_no_conflicting_writer || exit 1
}

install_unit() {
  local timestamp backup=""
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  run_root mkdir -p "$SYSTEMD_DIR"
  if run_root test -f "$UNIT_TARGET"; then
    backup="${UNIT_TARGET}.bak.${timestamp}"
    run_root cp --preserve=mode,ownership,timestamps \
      "$UNIT_TARGET" "$backup"
  fi
  run_root install -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
  run_root systemctl daemon-reload
  printf 'installed=%s\n' "$UNIT_TARGET"
  if [ -n "$backup" ]; then
    printf 'backup=%s\n' "$backup"
  fi
  printf 'service_started=false\n'
}

validate_host
install_unit
