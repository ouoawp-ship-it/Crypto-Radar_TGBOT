#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
PRODUCTION_ENABLE="${ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE:-false}"
SEND_ENABLE="${ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE:-false}"
SEND_CONFIRM="${ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_CONFIRM:-}"
EXPECTED_CONFIRM="ENABLE_ALTCOIN_ANOMALY_REAL_SEND"

case "$PRODUCTION_ENABLE" in
  false)
    [ "$SEND_ENABLE" = "false" ] || {
      printf 'altcoin_production_send_requires_production_mode\n' >&2
      exit 2
    }
    exec "$PYTHON_BIN" "${APP_DIR}/main.py" market-stream
    ;;
  true)
    args=("$PYTHON_BIN" "${APP_DIR}/main.py" market-stream "--altcoin-production")
    case "$SEND_ENABLE" in
      false)
        ;;
      true)
        if [ "$SEND_CONFIRM" != "$EXPECTED_CONFIRM" ] ||
          [[ ! "${TG_BOT_TOKEN:-}" =~ ^[1-9][0-9]*:[A-Za-z0-9_-]+$ ]] ||
          [[ ! "${TG_CHAT_ID:-}" =~ ^-?[1-9][0-9]+$ ]] ||
          [[ ! "${TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID:-}" =~ ^[1-9][0-9]*$ ]]; then
          printf 'altcoin_production_real_send_gate_blocked\n' >&2
          exit 2
        fi
        args+=("--send" "--confirm-real-send")
        ;;
      *)
        printf 'altcoin_production_send_mode_invalid\n' >&2
        exit 2
        ;;
    esac
    exec "${args[@]}"
    ;;
  *)
    printf 'altcoin_production_mode_invalid\n' >&2
    exit 2
    ;;
esac
