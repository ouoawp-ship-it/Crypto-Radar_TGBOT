#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
DELIVERY_MODE="${MAIN_BOT_DELIVERY_MODE:-dry_run}"
REAL_SEND="${MAIN_BOT_REAL_SEND:-false}"
REAL_SEND_ACK="${MAIN_BOT_REAL_SEND_ACK:-}"
EXPECTED_ACK="发送真实主BOT提醒"

args=("$PYTHON_BIN" "${APP_DIR}/main.py")

case "$DELIVERY_MODE" in
  dry_run)
    args+=("loop")
    ;;
  real)
    if [ "$REAL_SEND" != "true" ] ||
      [ "$REAL_SEND_ACK" != "$EXPECTED_ACK" ] ||
      [[ ! "${TG_BOT_TOKEN:-}" =~ ^[1-9][0-9]*:[A-Za-z0-9_-]+$ ]] ||
      [[ ! "${TG_CHAT_ID:-}" =~ ^[+-]?[0-9]+$ ]] ||
      [[ "${TG_CHAT_ID:-}" =~ ^[+-]?0+$ ]]; then
      printf 'main_bot_real_send_gate_blocked\n' >&2
      exit 2
    fi
    args+=("live" "--send" "--confirm-real-send")
    ;;
  *)
    printf 'main_bot_delivery_mode_invalid\n' >&2
    exit 2
    ;;
esac

exec "${args[@]}"
