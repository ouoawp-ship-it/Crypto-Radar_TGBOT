#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="${PAOPAO_PYTHON_BIN:-python3}"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

run_main() {
  cd "$APP_DIR"
  "$PYTHON_BIN" main.py "$@"
}

show_status() {
  run_main status
  if command -v systemctl >/dev/null 2>&1; then
    run_root systemctl --no-pager --full status "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" || true
  fi
}

show_logs() {
  command -v journalctl >/dev/null 2>&1 || return 0
  run_root journalctl -u "$SERVICE_NAME" -u "$MARKET_STREAM_SERVICE_NAME" -f
}

restart_services() {
  run_root systemctl restart "$MARKET_STREAM_SERVICE_NAME" "$SERVICE_NAME"
  run_root systemctl --no-pager --full status "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" || true
}

show_version() {
  cd "$APP_DIR"
  printf '版本: %s\n' "$(head -n 1 VERSION 2>/dev/null || printf unknown)"
  printf '提交: %s\n' "$(git rev-parse --short HEAD 2>/dev/null || printf unknown)"
}

show_help() {
  cat <<'EOF'
Paopao Telegram Radar BOT-only 控制命令

  paopao status          查看 BOT、实时行情服务与配置状态
  paopao logs            跟踪 BOT 与实时行情日志
  paopao restart         重启 BOT 与实时行情服务
  paopao doctor          输出环境诊断
  paopao readiness       检查真实推送门禁
  paopao stable-check    执行 BOT 稳定性检查
  paopao telegram-test   执行 Telegram dry-run 测试
  paopao cleanup         清理运行期缓存
  paopao check-update    检查 GitHub 更新
  paopao update          拉取、测试并发布 GitHub 更新
  paopao version         查看版本与提交
EOF
}

command="${1:-help}"
[ "$#" -gt 0 ] && shift
case "$command" in
  status) show_status ;;
  logs) show_logs ;;
  restart) restart_services ;;
  doctor) run_main doctor "$@" ;;
  readiness) run_main readiness "$@" ;;
  stable-check) run_main stable-check "$@" ;;
  telegram-test) run_main telegram-test "$@" ;;
  cleanup) run_main cleanup --force-cleanup "$@" ;;
  check-update|check) cd "$APP_DIR"; bash scripts/update_server.sh --check ;;
  update) cd "$APP_DIR"; bash scripts/update_server.sh --yes ;;
  version) show_version ;;
  help|-h|--help) show_help ;;
  *) printf '未知命令: %s\n\n' "$command" >&2; show_help; exit 2 ;;
esac
