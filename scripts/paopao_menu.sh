#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-paopao-web}"
AI_SERVICE_NAME="${AI_SERVICE_NAME:-paopao-ai}"
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

service_unit_exists() {
  local name="$1"
  command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${name}.service" >/dev/null 2>&1
}

web_public_url() {
  local port
  port="$(get_env_value WEB_PORT)"
  [ -n "$port" ] || port="8080"
  if [ "$port" = "80" ]; then
    printf 'http://服务器IP/'
  else
    printf 'http://服务器IP:%s/' "$port"
  fi
}

show_web_token() {
  local host port token
  host="$(get_env_value WEB_HOST)"
  port="$(get_env_value WEB_PORT)"
  token="$(get_env_value WEB_ADMIN_TOKEN)"
  [ -n "$host" ] || host="0.0.0.0"
  [ -n "$port" ] || port="8080"
  printf 'Web 控制台地址: %s\n' "$(web_public_url)"
  printf '监听配置: %s:%s\n' "$host" "$port"
  if [ -n "$token" ]; then
    printf '访问令牌: %s\n' "$token"
  else
    printf '访问令牌未配置。请返回菜单选择 6 更新项目，或重新运行安装脚本。\n'
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

start_web_service() {
  if service_unit_exists "$WEB_SERVICE_NAME"; then
    run_root systemctl start "$WEB_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$WEB_SERVICE_NAME" || true
  else
    printf '未找到 Web 控制台 systemd 服务: %s\n' "$WEB_SERVICE_NAME"
  fi
}

update_project() {
  cd_app
  bash scripts/update_server.sh "$@"
}

show_version() {
  printf '当前版本: %s\n' "$(project_version)"
  printf 'Git提交 : %s\n' "$(project_commit)"
  cd_app
  git log -1 --format='提交说明: %s' 2>/dev/null || true
}

pause_menu() {
  printf '\n按回车返回菜单...'
  read -r _ || true
}

show_menu_header() {
  local token
  token="$(get_env_value WEB_ADMIN_TOKEN)"
  cat <<EOF
============================================================
泡泡雷达中文控制台
============================================================
项目目录: ${APP_DIR}
当前版本: $(project_version) ($(project_commit))

Web 地址: $(web_public_url)
访问令牌: ${token:-未配置，请先选择 6 更新项目，或执行安装/更新脚本}

说明:
  1. 服务器只需要记住一个入口命令：paopao。
  2. 打开上面的 Web 地址，输入访问令牌，就能进入网页控制台。
  3. 配置修改、日志查看、主服务/结构雷达启停、Telegram 测试、
     readiness、doctor、cleanup、结构复盘，都在 Web 页面里操作。
  4. 这个中文菜单只保留最常用的服务器维护动作，避免记长命令。

请选择:
  1. 查看 Web 地址和令牌
  2. 查看 Web 控制台服务状态
  3. 查看 Web 控制台实时日志
  4. 重启 Web 控制台服务
  5. 检查 GitHub 是否有更新
  6. 更新项目代码
  7. 查看当前版本
  0. 退出
============================================================
EOF
}

show_menu() {
  while true; do
    show_menu_header
    read -r -p "输入编号: " choice
    case "$choice" in
      1) show_web_token; pause_menu ;;
      2) show_web_status; pause_menu ;;
      3) show_web_logs ;;
      4) restart_web_service; pause_menu ;;
      5) update_project --check; pause_menu ;;
      6) update_project --yes; pause_menu ;;
      7) show_version; pause_menu ;;
      0) exit 0 ;;
      *) printf '无效选项，请输入 0-7。\n'; pause_menu ;;
    esac
  done
}

show_help() {
  cat <<EOF
泡泡雷达中文控制台

入口指令:
  paopao

Web 地址:
  $(web_public_url)

说明:
  输入 paopao 后，直接用数字选择功能。
  配置修改、服务启停、日志查看、测试消息、readiness、doctor、cleanup、结构复盘等日常控制功能，都在 Web 页面里完成。
  服务器菜单只保留 Web 地址/令牌、Web 服务排查、项目更新和版本查看。

项目目录: ${APP_DIR}
EOF
}

command="${1:-home}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$command" in
  home|menu|"")
    show_menu
    ;;
  web)
    run_main web "$@"
    ;;
  web-token|token|url)
    show_web_token
    ;;
  web-status|status)
    show_web_status
    ;;
  web-logs|web-log|logs)
    show_web_logs
    ;;
  web-restart|restart)
    restart_web_service
    ;;
  web-start|start)
    start_web_service
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
  help|-h|--help)
    show_help
    ;;
  *)
    printf '未知命令: %s\n\n' "$command" >&2
    show_help
    exit 2
    ;;
esac
