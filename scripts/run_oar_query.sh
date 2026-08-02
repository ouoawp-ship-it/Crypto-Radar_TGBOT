#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-/home/ubuntu/paopao-crypto-radar}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
QUERY_ENABLE="${OAR_TELEGRAM_QUERY_ENABLE:-false}"
QUERY_ACK="${OAR_TELEGRAM_QUERY_ACK:-}"
EXPECTED_ACK="启用群内链上查询"

if [ "$QUERY_ENABLE" != "true" ] || [ "$QUERY_ACK" != "$EXPECTED_ACK" ]; then
  printf 'telegram_query_configuration_blocked\n' >&2
  exit 2
fi
if [[ ! "${TG_BOT_TOKEN:-}" =~ ^[1-9][0-9]*:[A-Za-z0-9_-]+$ ]] ||
  [[ ! "${TG_CHAT_ID:-}" =~ ^-?[1-9][0-9]*$ ]] ||
  [[ ! "${TG_ONCHAIN_FLOW_TOPIC_ID:-}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'telegram_query_configuration_blocked\n' >&2
  exit 2
fi

args=(
  "$PYTHON_BIN"
  "${APP_DIR}/onchain_main.py"
  "telegram-query-live"
  "--allow-network"
  "--send"
  "--confirm-real-send"
)

exec "${args[@]}"
