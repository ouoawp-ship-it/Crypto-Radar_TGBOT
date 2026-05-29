#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="paopao-launch-radar"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
SERVICE_TEMPLATE="${SCRIPT_DIR}/paopao-launch-radar.service"
SERVICE_TARGET="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_TEMPLATE="${SCRIPT_DIR}/nginx-paoxx-launch-radar.conf"

if [ ! -d "${PROJECT_DIR}/paopao_radar" ] || [ ! -f "${PROJECT_DIR}/main.py" ]; then
  echo "ERROR: project directory check failed: ${PROJECT_DIR}" >&2
  exit 1
fi

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "ERROR: virtualenv python not found or not executable: ${PYTHON_BIN}" >&2
  echo "Run scripts/install_server.sh first, or create .venv manually." >&2
  exit 1
fi

if [ ! -f "${SERVICE_TEMPLATE}" ]; then
  echo "ERROR: missing systemd service template: ${SERVICE_TEMPLATE}" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: systemctl not found; this installer requires systemd." >&2
  exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

echo "Project directory: ${PROJECT_DIR}"
echo "Python: ${PYTHON_BIN}"
echo "Compile check: python -m compileall paopao_radar"
"${PYTHON_BIN}" -m compileall "${PROJECT_DIR}/paopao_radar"

tmp_service="$(mktemp)"
cleanup() {
  rm -f "${tmp_service}"
}
trap cleanup EXIT

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[#&]/\\&/g'
}

PROJECT_DIR_ESCAPED="$(escape_sed_replacement "${PROJECT_DIR}")"
PYTHON_BIN_ESCAPED="$(escape_sed_replacement "${PYTHON_BIN}")"

sed \
  -e "s#__PROJECT_DIR__#${PROJECT_DIR_ESCAPED}#g" \
  -e "s#__PYTHON_BIN__#${PYTHON_BIN_ESCAPED}#g" \
  "${SERVICE_TEMPLATE}" > "${tmp_service}"

echo "Installing systemd service: ${SERVICE_TARGET}"
${SUDO} install -m 0644 "${tmp_service}" "${SERVICE_TARGET}"
${SUDO} systemctl daemon-reload
${SUDO} systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "Service started. Useful commands:"
echo "  systemctl status ${SERVICE_NAME}.service --no-pager"
echo "  journalctl -u ${SERVICE_NAME}.service -f"
echo "  curl http://127.0.0.1:18090/api/health"
echo "  curl http://127.0.0.1:18090/api/launch-radar"
echo
echo "Nginx template: ${NGINX_TEMPLATE}"
echo "This script does not overwrite your existing Nginx site config."
echo "Include the template in the paoxx.com HTTPS server block, then run:"
echo "  nginx -t"
echo "  systemctl reload nginx"
