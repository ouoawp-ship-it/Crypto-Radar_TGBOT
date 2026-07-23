#!/usr/bin/env bash
set -u

APP_DIR="${1:-${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

set +e
"${APP_DIR}/.venv/bin/python" "${APP_DIR}/main.py" stable-check --json --no-save
code=$?
set -e

case "$code" in
  0|1)
    # Exit 1 means the runtime needs attention, but has no blocking failure.
    exit 0
    ;;
  2)
    exit 2
    ;;
  *)
    exit "$code"
    ;;
esac
