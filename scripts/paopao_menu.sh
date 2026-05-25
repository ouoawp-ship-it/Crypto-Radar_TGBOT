#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PAOPAO_PYTHON_BIN:-python3}"
fi

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

cd_app() {
  cd "$APP_DIR"
}

run_main() {
  cd_app
  "$PYTHON_BIN" main.py "$@"
}

service_exists() {
  command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1
}

show_help() {
  cat <<EOF
泡泡抓币快捷命令:

  paopao                 打开中文操作菜单
  paopao menu            打开中文操作菜单
  paopao config          修改 token / 群 ID / CoinGlass key / 话题配置
  paopao status          查看 systemd 服务和运行状态
  paopao logs            查看实时日志
  paopao restart         重启服务
  paopao start           启动服务
  paopao stop            停止服务
  paopao update          检查 GitHub 版本，有更新时确认后更新
  paopao update --yes    有更新时自动确认更新
  paopao check-update    只检查当前版本和 GitHub 版本
  paopao test            发送 Telegram 测试消息
  paopao coinglass       测试 CoinGlass API
  paopao runtime         查看 runtime-status
  paopao readiness       检查真实推送准备度
  paopao doctor          查看环境诊断
  paopao help            查看这份帮助

当前项目目录: ${APP_DIR}
EOF
}

show_status() {
  if service_exists; then
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    printf '未找到 systemd 服务: %s\n' "$SERVICE_NAME"
  fi
  printf '\n运行状态:\n'
  run_main runtime-status || true
}

show_logs() {
  if service_exists; then
    run_root journalctl -u "$SERVICE_NAME" -f
  else
    printf '未找到 systemd 服务，改为查看 data/runtime.log:\n'
    cd_app
    tail -f data/runtime.log
  fi
}

restart_service() {
  if service_exists; then
    run_root systemctl restart "$SERVICE_NAME"
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    printf '未找到 systemd 服务: %s\n' "$SERVICE_NAME"
  fi
}

start_service() {
  if service_exists; then
    run_root systemctl start "$SERVICE_NAME"
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    printf '未找到 systemd 服务: %s\n' "$SERVICE_NAME"
  fi
}

stop_service() {
  if service_exists; then
    run_root systemctl stop "$SERVICE_NAME"
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    printf '未找到 systemd 服务: %s\n' "$SERVICE_NAME"
  fi
}

update_project() {
  cd_app
  bash scripts/update_server.sh "$@"
}

edit_config() {
  cd_app
  bash scripts/install_server.sh config
}

pause_menu() {
  printf '\n按回车返回菜单...'
  read -r _ || true
}

show_menu() {
  while true; do
    cat <<EOF

============================================================
泡泡抓币 - 快捷操作菜单
============================================================
项目目录: ${APP_DIR}

  1. 查看服务状态
  2. 查看实时日志
  3. 修改配置 token / 群 ID / CoinGlass key
  4. 发送 Telegram 测试消息
  5. 测试 CoinGlass API
  6. 查看运行状态 runtime-status
  7. 检查 readiness
  8. 重启服务
  9. 启动服务
 10. 停止服务
 11. 检查更新 / 更新项目代码
 12. 环境诊断 doctor
  0. 退出
============================================================

EOF
    read -r -p "请选择操作: " choice
    case "$choice" in
      1) show_status; pause_menu ;;
      2) show_logs ;;
      3) edit_config; pause_menu ;;
      4) run_main telegram-test --send --confirm-real-send; pause_menu ;;
      5) run_main coinglass-test; pause_menu ;;
      6) run_main runtime-status; pause_menu ;;
      7) run_main readiness; pause_menu ;;
      8) restart_service; pause_menu ;;
      9) start_service; pause_menu ;;
      10) stop_service; pause_menu ;;
      11) update_project; pause_menu ;;
      12) run_main doctor; pause_menu ;;
      0) exit 0 ;;
      *) printf '无效选项，请输入 0-12。\n'; pause_menu ;;
    esac
  done
}

command="${1:-menu}"
if [ "$#" -gt 0 ]; then
  shift
fi
case "$command" in
  menu|"")
    show_menu
    ;;
  config|configure)
    edit_config
    ;;
  status)
    show_status
    ;;
  logs|log)
    show_logs
    ;;
  restart)
    restart_service
    ;;
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  update)
    update_project "$@"
    ;;
  check-update|check|version)
    update_project --check
    ;;
  test|telegram-test)
    run_main telegram-test --send --confirm-real-send
    ;;
  coinglass|coinglass-test)
    run_main coinglass-test
    ;;
  runtime|runtime-status)
    run_main runtime-status
    ;;
  readiness)
    run_main readiness
    ;;
  doctor)
    run_main doctor
    ;;
  help|-h|--help)
    show_help
    ;;
  *)
    printf '未知命令: %s\n\n' "$command" >&2
    show_help
    exit 2
    ;;
esac
