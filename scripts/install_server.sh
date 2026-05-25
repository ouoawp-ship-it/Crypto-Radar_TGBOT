#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="${APP_NAME:-paopao-radar}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENV_FILE="${APP_DIR}/.env.oi"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
AUTO_START="${AUTO_START:-1}"
BOOTSTRAP_HISTORY="${BOOTSTRAP_HISTORY:-1}"
BOOTSTRAP_CYCLES="${BOOTSTRAP_CYCLES:-5}"
BOOTSTRAP_LAUNCH_SCAN_LIMIT="${BOOTSTRAP_LAUNCH_SCAN_LIMIT:-5}"
RUN_TELEGRAM_TEST="${RUN_TELEGRAM_TEST:-0}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

log() {
  printf '\n[%s] %s\n' "$APP_NAME" "$*"
}

die() {
  printf '\n[%s] ERROR: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

get_env_value() {
  local key="$1"
  local line value
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  [[ "$line" == *=* ]] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

is_placeholder_value() {
  local value lower
  value="${1:-}"
  lower="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  [[ -z "$value" || "$lower" == *your* || "$lower" == *token* || "$lower" == *chat_id* || "$lower" == *bot_token* || "$lower" == *example* || "$lower" == *xxx* || "$value" == *填写* || "$value" == *填入* || "$value" == *请输入* ]]
}

is_valid_bot_token() {
  local value="${1:-}"
  if is_placeholder_value "$value"; then
    return 1
  fi
  [[ "$value" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{25,}$ ]]
}

is_valid_chat_id() {
  local value="${1:-}"
  if is_placeholder_value "$value"; then
    return 1
  fi
  [[ "$value" =~ ^-?[0-9]{5,20}$ || "$value" =~ ^@[A-Za-z0-9_]{5,32}$ ]]
}

set_env_value() {
  local key="$1"
  local value="$2"
  "$PYTHON_BIN" - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
line = f"{key}={value}"
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
for idx, existing in enumerate(lines):
    if existing.startswith(f"{key}="):
        lines[idx] = line
        break
else:
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

prompt_telegram_config() {
  if [ ! -t 0 ]; then
    cat <<EOF

.env.oi is missing TG_BOT_TOKEN or TG_CHAT_ID, but this shell is not interactive.
Edit it manually, then rerun:

  nano ${ENV_FILE}
  bash ${APP_DIR}/scripts/install_server.sh

EOF
    exit 0
  fi

  local bot_token chat_id topic_id summary_topic_id launch_topic_id announcement_topic_id test_topic_id
  printf '\nTelegram configuration is required before starting real push.\n'
  printf 'Tip: the token input is visible so terminal paste can be verified.\n'
  while true; do
    read -r -p "TG_BOT_TOKEN paste here: " bot_token
    if is_valid_bot_token "$bot_token"; then
      break
    fi
    printf 'Invalid TG_BOT_TOKEN. It must look like 123456:ABC... Press Ctrl+C to stop.\n'
  done

  while true; do
    read -r -p "TG_CHAT_ID: " chat_id
    if is_valid_chat_id "$chat_id"; then
      break
    fi
    printf 'Invalid TG_CHAT_ID. Use a numeric id like -1001234567890 or @channel_username. Press Ctrl+C to stop.\n'
  done

  read -r -p "TG_TOPIC_ID default topic optional, press Enter to skip: " topic_id
  read -r -p "TG_RADAR_SUMMARY_TOPIC_ID scheduled summary optional, press Enter to skip: " summary_topic_id
  read -r -p "TG_LAUNCH_ALERT_TOPIC_ID instant launch alerts optional, press Enter to skip: " launch_topic_id
  read -r -p "TG_ANNOUNCEMENT_ALERT_TOPIC_ID announcements/risks optional, press Enter to skip: " announcement_topic_id
  read -r -p "TG_TEST_TOPIC_ID test messages optional, press Enter to skip: " test_topic_id

  set_env_value TG_BOT_TOKEN "$bot_token"
  set_env_value TG_CHAT_ID "$chat_id"
  if [ -n "$topic_id" ]; then
    set_env_value TG_TOPIC_ID "$topic_id"
    set_env_value TELEGRAM_USE_TOPIC "true"
  fi
  if [ -n "$summary_topic_id" ]; then
    set_env_value TG_RADAR_SUMMARY_TOPIC_ID "$summary_topic_id"
    set_env_value TELEGRAM_USE_TOPIC "true"
  fi
  if [ -n "$launch_topic_id" ]; then
    set_env_value TG_LAUNCH_ALERT_TOPIC_ID "$launch_topic_id"
    set_env_value TELEGRAM_USE_TOPIC "true"
  fi
  if [ -n "$announcement_topic_id" ]; then
    set_env_value TG_ANNOUNCEMENT_ALERT_TOPIC_ID "$announcement_topic_id"
    set_env_value TELEGRAM_USE_TOPIC "true"
  fi
  if [ -n "$test_topic_id" ]; then
    set_env_value TG_TEST_TOPIC_ID "$test_topic_id"
    set_env_value TELEGRAM_USE_TOPIC "true"
  fi
  set_env_value TG_AUTO_CREATE_TOPICS "true"
  chmod 600 "$ENV_FILE" || true
}

install_os_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing OS packages"
    run_root apt-get update
    run_root apt-get install -y git python3 python3-venv python3-pip
  else
    log "apt-get not found; skipping OS package installation"
  fi
}

ensure_env_file() {
  if [ ! -f "$ENV_FILE" ]; then
    log "Creating .env.oi from example"
    cp "${APP_DIR}/.env.oi.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE" || true
    prompt_telegram_config
  fi

  local existing_token existing_chat
  existing_token="$(get_env_value TG_BOT_TOKEN)"
  existing_chat="$(get_env_value TG_CHAT_ID)"
  if ! is_valid_bot_token "$existing_token" || ! is_valid_chat_id "$existing_chat"; then
    prompt_telegram_config
  fi
}

install_python_deps() {
  log "Creating virtual environment"
  "$PYTHON_BIN" -m venv "${APP_DIR}/.venv"

  log "Installing Python dependencies"
  "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
  "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
}

run_checks() {
  log "Running compile check"
  "${APP_DIR}/.venv/bin/python" -m py_compile \
    "${APP_DIR}/main.py" \
    "${APP_DIR}/paopao_radar/cli.py" \
    "${APP_DIR}/paopao_radar/config.py" \
    "${APP_DIR}/paopao_radar/storage.py" \
    "${APP_DIR}/paopao_radar/data_sources.py" \
    "${APP_DIR}/paopao_radar/telegram.py" \
    "${APP_DIR}/paopao_radar/radar.py" \
    "${APP_DIR}/paopao_radar/maintenance.py"

  log "Running unit tests"
  cd "$APP_DIR"
  "${APP_DIR}/.venv/bin/python" -m unittest discover -s tests -v
}

bootstrap_history_if_needed() {
  if [ "$BOOTSTRAP_HISTORY" != "1" ]; then
    return 0
  fi

  cd "$APP_DIR"
  if "${APP_DIR}/.venv/bin/python" main.py readiness >/tmp/paopao-readiness.log 2>&1; then
    log "Readiness already passes"
    return 0
  fi

  log "Bootstrapping dry-run launch history (${BOOTSTRAP_CYCLES} cycles, scan limit ${BOOTSTRAP_LAUNCH_SCAN_LIMIT})"
  local i
  for i in $(seq 1 "$BOOTSTRAP_CYCLES"); do
    "${APP_DIR}/.venv/bin/python" main.py observe \
      --duration-minutes 0 \
      --launch-interval 60 \
      --launch-scan-limit "$BOOTSTRAP_LAUNCH_SCAN_LIMIT" \
      --records 20 \
      --top 5
  done
}

run_readiness() {
  log "Running readiness"
  cd "$APP_DIR"
  "${APP_DIR}/.venv/bin/python" main.py readiness

  if [ "$RUN_TELEGRAM_TEST" = "1" ]; then
    log "Sending one Telegram test message"
    "${APP_DIR}/.venv/bin/python" main.py telegram-test --send --confirm-real-send
  else
    log "Skipping Telegram test message; set RUN_TELEGRAM_TEST=1 to send one during install"
  fi
}

install_systemd_service() {
  command -v systemctl >/dev/null 2>&1 || {
    log "systemctl not found; systemd service not installed"
    return 0
  }

  log "Installing systemd service: ${SERVICE_NAME}"
  local service_path="/etc/systemd/system/${SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Crypto Radar
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py live --send --confirm-real-send
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable "$SERVICE_NAME"

  if [ "$AUTO_START" = "1" ]; then
    log "Starting service"
    run_root systemctl restart "$SERVICE_NAME"
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    log "AUTO_START=0; service installed but not started"
  fi
}

main() {
  cd "$APP_DIR"
  log "App directory: ${APP_DIR}"
  install_os_packages
  ensure_env_file
  install_python_deps
  run_checks
  bootstrap_history_if_needed
  run_readiness
  install_systemd_service

  cat <<EOF

Done.

Useful commands:
  sudo systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
  cd ${APP_DIR} && . .venv/bin/activate && python main.py runtime-status

EOF
}

main "$@"
