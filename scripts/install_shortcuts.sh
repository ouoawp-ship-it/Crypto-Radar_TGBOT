#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTALL_PP_SHORTCUT="${INSTALL_PP_SHORTCUT:-1}"
TARGET_DIR="${PAOPAO_SHORTCUT_DIR:-/usr/local/bin}"
TEMPORARY=""

run_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

cleanup() {
  [ -z "$TEMPORARY" ] || rm -f -- "$TEMPORARY"
}
trap cleanup EXIT

main() {
  test -x "${APP_DIR}/scripts/paopao_menu.sh"
  TEMPORARY="$(mktemp)"
  cat >"$TEMPORARY" <<EOF
#!/usr/bin/env bash
export PAOPAO_APP_DIR="${APP_DIR}"
exec bash "${APP_DIR}/scripts/paopao_menu.sh" "\$@"
EOF
  run_root mkdir -p "$TARGET_DIR"
  run_root install -m 0755 "$TEMPORARY" "${TARGET_DIR}/paopao"
  if [ "$INSTALL_PP_SHORTCUT" = "1" ]; then
    run_root ln -sfn "${TARGET_DIR}/paopao" "${TARGET_DIR}/pp"
  fi
}

main "$@"
