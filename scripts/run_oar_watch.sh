#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-/home/ubuntu/paopao-crypto-radar}"
PYTHON_BIN="${PAOPAO_PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"
DELIVERY_MODE="${OAR_WATCH_DELIVERY_MODE:-observe}"
REAL_SEND_ACK_REQUIRED="发送真实链上提醒"

is_true() {
  [ "${1:-false}" = "true" ]
}

telegram_config_complete() {
  [[ "${TG_BOT_TOKEN:-}" =~ ^[1-9][0-9]*:[A-Za-z0-9_-]+$ ]] &&
    [[ "${TG_CHAT_ID:-}" =~ ^[+-]?[1-9][0-9]*$ ]] &&
    [[ "${TG_ONCHAIN_FLOW_TOPIC_ID:-}" =~ ^[1-9][0-9]*$ ]]
}

ai_config_complete() {
  [ -n "${OAR_AI_BASE_URL:-}" ] &&
    [ -n "${OAR_AI_API_KEY:-}" ] &&
    [ -n "${OAR_AI_MODEL:-}" ]
}

args=(
  "${APP_DIR}/onchain_main.py"
  "watch-live"
  "--allow-network"
)

case "$DELIVERY_MODE" in
  observe)
    ;;
  dry_run)
    args+=("--notify-dry-run")
    ;;
  real)
    if ! is_true "${ONCHAIN_REAL_SEND:-false}" ||
      [ "${OAR_WATCH_REAL_SEND_ACK:-}" != "$REAL_SEND_ACK_REQUIRED" ] ||
      ! telegram_config_complete; then
      printf 'real_send_gate_blocked\n' >&2
      exit 2
    fi
    args+=("--send" "--confirm-real-send")
    ;;
  *)
    printf 'watch_delivery_mode_invalid\n' >&2
    exit 2
    ;;
esac

if [ "$DELIVERY_MODE" != "observe" ] &&
  is_true "${OAR_WATCH_WITH_AI:-false}" &&
  is_true "${OAR_AI_ENABLE:-false}" &&
  ai_config_complete; then
  args+=("--with-ai")
fi

exec "$PYTHON_BIN" "${args[@]}"
