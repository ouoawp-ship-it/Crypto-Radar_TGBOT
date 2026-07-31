#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-/home/ubuntu/paopao-crypto-radar}"
UNIT_NAME="${MAIN_BOT_SERVICE_NAME:-paopao-radar}"
SYSTEMD_DIR="${PAOPAO_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT_TARGET="${SYSTEMD_DIR}/${UNIT_NAME}.service"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-ubuntu}}"
RADAR_MEMORY_HIGH="${RADAR_MEMORY_HIGH:-450M}"
RADAR_MEMORY_MAX="${RADAR_MEMORY_MAX:-650M}"
START_MAIN_BOT="${START_MAIN_BOT:-0}"

if [ "${1:-}" = "--start" ]; then
  START_MAIN_BOT=1
elif [ "$#" -gt 0 ]; then
  printf 'usage: %s [--start]\n' "$0" >&2
  exit 2
fi

run_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

validate_host() {
  command -v systemctl >/dev/null 2>&1 || fail "systemd_required"
  [ -d "$APP_DIR" ] || fail "project_directory_missing"
  [ -x "${APP_DIR}/.venv/bin/python" ] || fail "project_venv_missing"
  [ -x "${APP_DIR}/scripts/run_main_bot.sh" ] || \
    fail "main_bot_runner_missing_or_not_executable"
  if [ -f "${APP_DIR}/.env.oi" ]; then
    [ "$(stat -c '%a' "${APP_DIR}/.env.oi")" = "600" ] || \
      fail "main_bot_env_permissions_must_be_600"
  fi
}

render_unit() {
  cat <<EOF
[Unit]
Description=Paopao Telegram Signal Radar
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=20

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/.env.oi
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=${APP_DIR}/scripts/run_main_bot.sh
Restart=on-failure
RestartPreventExitStatus=2
RestartSec=10
KillSignal=SIGINT
SuccessExitStatus=130
TimeoutStopSec=30
MemoryHigh=${RADAR_MEMORY_HIGH}
MemoryMax=${RADAR_MEMORY_MAX}
LimitNOFILE=65536
TasksMax=256
OOMPolicy=stop
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
}

install_unit() {
  local rendered timestamp backup=""
  rendered="$(mktemp)"
  trap "rm -f '$rendered'" EXIT
  render_unit >"$rendered"
  run_root mkdir -p "$SYSTEMD_DIR"
  if run_root test -f "$UNIT_TARGET" && ! run_root cmp -s "$rendered" "$UNIT_TARGET"; then
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup="${UNIT_TARGET}.bak.${timestamp}"
    run_root cp --preserve=mode,ownership,timestamps "$UNIT_TARGET" "$backup"
  fi
  if ! run_root test -f "$UNIT_TARGET" || ! run_root cmp -s "$rendered" "$UNIT_TARGET"; then
    run_root install -m 0644 "$rendered" "$UNIT_TARGET"
  fi
  run_root systemctl daemon-reload
  printf 'installed=%s\n' "$UNIT_TARGET"
  [ -z "$backup" ] || printf 'backup=%s\n' "$backup"
  if [ "$START_MAIN_BOT" = "1" ]; then
    run_root systemctl enable --now "$UNIT_NAME"
    printf 'service_started=true\n'
  else
    printf 'service_started=false\n'
  fi
  rm -f "$rendered"
  trap - EXIT
}

validate_host
install_unit
