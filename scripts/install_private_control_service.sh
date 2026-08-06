#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${APP_DIR}/config/.env.oi"
SERVICE_NAME="${PRIVATE_CONTROL_SERVICE_NAME:-paopao-private-control}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
START_PRIVATE_CONTROL="${START_PRIVATE_CONTROL:-0}"
ENABLE_PRIVATE_CONTROL="${ENABLE_PRIVATE_CONTROL:-0}"

run_root() {
  if [[ "$(id -u)" -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

command -v systemctl >/dev/null 2>&1 || exit 0

run_root tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Paopao Telegram Private Control
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/bin/bash ${APP_DIR}/scripts/run_private_control.sh
Restart=on-failure
RestartPreventExitStatus=2
RestartSec=10
KillSignal=SIGINT
SuccessExitStatus=130
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
RuntimeDirectory=paopao-private-control
RuntimeDirectoryMode=0700

[Install]
WantedBy=multi-user.target
EOF

run_root systemctl daemon-reload
if [[ "$ENABLE_PRIVATE_CONTROL" == "1" ]]; then
  run_root systemctl enable "$SERVICE_NAME"
fi
if [[ "$START_PRIVATE_CONTROL" == "1" ]]; then
  run_root systemctl start "$SERVICE_NAME"
fi
