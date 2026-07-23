#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APP_DIR}/.env.oi"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
CLEANUP_SERVICE_NAME="${CLEANUP_SERVICE_NAME:-paopao-cleanup}"
HEALTH_SERVICE_NAME="${HEALTH_SERVICE_NAME:-paopao-health}"
RADAR_MEMORY_HIGH="${RADAR_MEMORY_HIGH:-450M}"
RADAR_MEMORY_MAX="${RADAR_MEMORY_MAX:-650M}"
MARKET_STREAM_MEMORY_HIGH="${MARKET_STREAM_MEMORY_HIGH:-128M}"
MARKET_STREAM_MEMORY_MAX="${MARKET_STREAM_MEMORY_MAX:-256M}"
AUTO_START="${AUTO_START:-1}"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

log() {
  printf '\n[paopao-install] %s\n' "$*"
}

install_os_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update
    run_root apt-get install -y git python3 python3-venv python3-pip ca-certificates
  fi
}

ensure_env_file() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "${APP_DIR}/.env.oi.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE" || true
    printf '已创建 %s，请填写 TG_BOT_TOKEN 和 TG_CHAT_ID 后重新执行安装。\n' "$ENV_FILE" >&2
    exit 2
  fi
}

install_python_runtime() {
  if [ ! -x "${APP_DIR}/.venv/bin/python" ]; then
    "$PYTHON_BIN" -m venv "${APP_DIR}/.venv"
  fi
  "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
  "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
}

run_checks() {
  cd "$APP_DIR"
  "${APP_DIR}/.venv/bin/python" -m compileall -q paopao_radar tests scripts main.py
  "${APP_DIR}/.venv/bin/python" -m unittest discover -s tests -p 'test_*.py'
}

validate_bot_config() {
  set +e
  "${APP_DIR}/.venv/bin/python" main.py stable-check --no-save
  local code=$?
  set -e
  if [ "$code" -eq 2 ]; then
    printf 'Telegram 配置无效，请修正 .env.oi 后重新执行安装。\n' >&2
    exit 2
  fi
}

write_service() {
  local name="$1"
  local description="$2"
  local command="$3"
  local memory_high="$4"
  local memory_max="$5"
  run_root tee "/etc/systemd/system/${name}.service" >/dev/null <<EOF
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=20

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py ${command}
Restart=always
RestartSec=10
MemoryHigh=${memory_high}
MemoryMax=${memory_max}
LimitNOFILE=65536
TasksMax=256
TimeoutStopSec=30
OOMPolicy=stop
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
}

install_services() {
  command -v systemctl >/dev/null 2>&1 || return 0
  write_service "$SERVICE_NAME" "Paopao Telegram Signal Radar" "live --send --confirm-real-send" "$RADAR_MEMORY_HIGH" "$RADAR_MEMORY_MAX"
  write_service "$MARKET_STREAM_SERVICE_NAME" "Paopao Realtime Market Stream" "market-stream" "$MARKET_STREAM_MEMORY_HIGH" "$MARKET_STREAM_MEMORY_MAX"

  run_root tee "/etc/systemd/system/${CLEANUP_SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Paopao Runtime Cleanup

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py cleanup --force-cleanup
EOF

  run_root tee "/etc/systemd/system/${CLEANUP_SERVICE_NAME}.timer" >/dev/null <<EOF
[Unit]
Description=Run Paopao Runtime Cleanup Hourly

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

  run_root tee "/etc/systemd/system/${HEALTH_SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Paopao Runtime Health Check
After=${SERVICE_NAME}.service ${MARKET_STREAM_SERVICE_NAME}.service

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/bin/bash ${APP_DIR}/scripts/systemd_health_check.sh ${APP_DIR}
Nice=10
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
EOF

  run_root tee "/etc/systemd/system/${HEALTH_SERVICE_NAME}.timer" >/dev/null <<EOF
[Unit]
Description=Check Paopao Runtime Health Every Five Minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" "${CLEANUP_SERVICE_NAME}.timer" "${HEALTH_SERVICE_NAME}.timer"
  if [ "$AUTO_START" = "1" ]; then
    run_root systemctl restart "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME"
    run_root systemctl restart "${CLEANUP_SERVICE_NAME}.timer" "${HEALTH_SERVICE_NAME}.timer"
  fi
}

install_shortcut() {
  run_root tee /usr/local/bin/paopao >/dev/null <<EOF
#!/usr/bin/env bash
export PAOPAO_APP_DIR="${APP_DIR}"
exec bash "${APP_DIR}/scripts/paopao_menu.sh" "\$@"
EOF
  run_root chmod +x /usr/local/bin/paopao
}

main() {
  log "安装 BOT-only 运行环境"
  install_os_packages
  ensure_env_file
  install_python_runtime
  cd "$APP_DIR"
  "${APP_DIR}/.venv/bin/python" scripts/sync_env.py --env .env.oi --example .env.oi.example
  run_checks
  validate_bot_config
  install_services
  install_shortcut
  log "安装完成。使用 paopao status 或 journalctl -u ${SERVICE_NAME} -f 查看运行状态。"
}

main "$@"
