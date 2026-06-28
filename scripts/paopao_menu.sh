#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
STRUCTURE_SERVICE_NAME="${STRUCTURE_SERVICE_NAME:-paopao-structure}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-paopao-web}"
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

get_env_value() {
  local key="$1"
  local env_file="${APP_DIR}/.env.oi"
  local line value
  line="$(grep -E "^${key}=" "$env_file" 2>/dev/null | tail -n 1 || true)"
  [[ "$line" == *=* ]] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

service_exists() {
  command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1
}

service_unit_exists() {
  local name="$1"
  command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${name}.service" >/dev/null 2>&1
}

project_version() {
  cd_app
  if [ -f VERSION ]; then
    head -n 1 VERSION | tr -d '\r'
  else
    printf 'unknown'
  fi
}

project_commit() {
  cd_app
  git rev-parse --short HEAD 2>/dev/null || printf 'unknown'
}

show_version() {
  printf '当前版本: %s\n' "$(project_version)"
  printf 'Git提交 : %s\n' "$(project_commit)"
  cd_app
  git log -1 --format='提交说明: %s' 2>/dev/null || true
}

show_help() {
  cat <<EOF
泡泡抓币快捷命令:

  paopao                 打开中文操作菜单
  paopao menu            打开中文操作菜单
  paopao config          修改 token / 群 ID / Coinalyze key / 话题配置
  paopao status          查看 systemd 服务和运行状态
  paopao logs            查看实时日志
  paopao restart         重启服务
  paopao start           启动服务
  paopao stop            停止服务
  paopao update          检查 GitHub 版本，有更新时确认后更新
  paopao update --yes    有更新时自动确认更新
  paopao check-update    只检查当前版本和 GitHub 版本
  paopao version         查看当前项目版本
  paopao test            发送 Telegram 测试消息
  paopao announcements   测试 Binance 公告抓取和分类
  paopao cleanup         立即清理运行垃圾
  paopao structure       dry-run 运行结构突破雷达
  paopao structure-review dry-run 生成结构信号复盘报告
  paopao structure-status 查看结构雷达独立服务状态
  paopao structure-logs   查看结构雷达独立服务日志
  paopao structure-restart 重启结构雷达独立服务
  paopao runtime         查看 runtime-status
  paopao readiness       检查真实推送准备度
  paopao doctor          查看环境诊断
  paopao web             前台调试启动 Web 控制台
  paopao web-status      查看 Web 控制台服务状态
  paopao web-logs        查看 Web 控制台服务日志
  paopao web-restart     重启 Web 控制台服务
  paopao web-token       查看 Web 控制台访问令牌
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

show_structure_status() {
  if service_unit_exists "$STRUCTURE_SERVICE_NAME"; then
    run_root systemctl --no-pager --full status "$STRUCTURE_SERVICE_NAME" || true
  else
    printf '未找到结构雷达 systemd 服务: %s\n' "$STRUCTURE_SERVICE_NAME"
  fi
  printf '\n结构雷达运行状态:\n'
  run_main runtime-status || true
}

show_structure_logs() {
  if service_unit_exists "$STRUCTURE_SERVICE_NAME"; then
    run_root journalctl -u "$STRUCTURE_SERVICE_NAME" -f
  else
    printf '未找到结构雷达 systemd 服务: %s\n' "$STRUCTURE_SERVICE_NAME"
  fi
}

restart_structure_service() {
  if service_unit_exists "$STRUCTURE_SERVICE_NAME"; then
    run_root systemctl restart "$STRUCTURE_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$STRUCTURE_SERVICE_NAME" || true
  else
    printf '未找到结构雷达 systemd 服务: %s\n' "$STRUCTURE_SERVICE_NAME"
  fi
}

show_web_status() {
  if service_unit_exists "$WEB_SERVICE_NAME"; then
    run_root systemctl --no-pager --full status "$WEB_SERVICE_NAME" || true
  else
    printf '未找到 Web 控制台 systemd 服务: %s\n' "$WEB_SERVICE_NAME"
  fi
}

show_web_logs() {
  if service_unit_exists "$WEB_SERVICE_NAME"; then
    run_root journalctl -u "$WEB_SERVICE_NAME" -f
  else
    printf '未找到 Web 控制台 systemd 服务: %s\n' "$WEB_SERVICE_NAME"
  fi
}

restart_web_service() {
  if service_unit_exists "$WEB_SERVICE_NAME"; then
    run_root systemctl restart "$WEB_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$WEB_SERVICE_NAME" || true
  else
    printf '未找到 Web 控制台 systemd 服务: %s\n' "$WEB_SERVICE_NAME"
  fi
}

show_web_token() {
  local host port token
  host="$(get_env_value WEB_HOST)"
  port="$(get_env_value WEB_PORT)"
  token="$(get_env_value WEB_ADMIN_TOKEN)"
  [ -n "$host" ] || host="0.0.0.0"
  [ -n "$port" ] || port="80"
  printf 'Web 控制台地址: http://服务器IP/admin/\n'
  printf '监听配置: %s:%s\n' "$host" "$port"
  if [ -n "$token" ]; then
    printf '访问令牌: %s\n' "$token"
  else
    printf '访问令牌未配置。请执行: bash scripts/update_server.sh --yes\n'
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

cleanup_project() {
  run_main cleanup --force-cleanup
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
当前版本: $(project_version) ($(project_commit))

 1. 查看服务状态
  2. 查看实时日志
  3. 修改配置 token / 群 ID / Coinalyze key
  4. 发送 Telegram 测试消息
  5. 查看运行状态 runtime-status
  6. 检查 readiness
  7. 重启服务
  8. 启动服务
  9. 停止服务
 10. 检查更新 / 更新项目代码
 11. 环境诊断 doctor
 12. 查看当前版本
 13. 结构信号复盘 structure-review
 14. 结构雷达服务状态
 15. 结构雷达实时日志
  16. 重启结构雷达服务
  17. 测试 Binance 公告抓取
  18. 立即清理运行垃圾 cleanup
 19. Web 控制台服务状态
 20. Web 控制台实时日志
 21. 重启 Web 控制台服务
 22. 查看 Web 控制台访问令牌
  0. 退出
============================================================

EOF
    read -r -p "请选择操作: " choice
    case "$choice" in
      1) show_status; pause_menu ;;
      2) show_logs ;;
      3) edit_config; pause_menu ;;
      4) run_main telegram-test --send --confirm-real-send; pause_menu ;;
      5) run_main runtime-status; pause_menu ;;
      6) run_main readiness; pause_menu ;;
      7) restart_service; pause_menu ;;
      8) start_service; pause_menu ;;
      9) stop_service; pause_menu ;;
      10) update_project; pause_menu ;;
      11) run_main doctor; pause_menu ;;
      12) show_version; pause_menu ;;
      13) run_main structure-review; pause_menu ;;
      14) show_structure_status; pause_menu ;;
      15) show_structure_logs ;;
      16) restart_structure_service; pause_menu ;;
      17) run_main announcements-test; pause_menu ;;
      18) cleanup_project; pause_menu ;;
      19) show_web_status; pause_menu ;;
      20) show_web_logs ;;
      21) restart_web_service; pause_menu ;;
      22) show_web_token; pause_menu ;;
      0) exit 0 ;;
      *) printf '无效选项，请输入 0-22。\n'; pause_menu ;;
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
  check-update|check)
    update_project --check
    ;;
  version|local-version|current-version)
    show_version
    ;;
  test|telegram-test)
    run_main telegram-test --send --confirm-real-send
    ;;
  announcements|announcements-test)
    run_main announcements-test
    ;;
  cleanup|clean)
    cleanup_project
    ;;
  structure|structure-radar)
    run_main structure-radar --save-charts "$@"
    ;;
  structure-loop)
    run_main structure-loop "$@"
    ;;
  structure-review)
    run_main structure-review "$@"
    ;;
  structure-status)
    show_structure_status
    ;;
  structure-logs|structure-log)
    show_structure_logs
    ;;
  structure-restart)
    restart_structure_service
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
  web)
    run_main web "$@"
    ;;
  web-status)
    show_web_status
    ;;
  web-logs|web-log)
    show_web_logs
    ;;
  web-restart)
    restart_web_service
    ;;
  web-token)
    show_web_token
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
