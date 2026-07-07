#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-paopao-web}"
AI_SERVICE_NAME="${AI_SERVICE_NAME:-paopao-ai}"
PUBLIC_FRONTEND_URL="${PUBLIC_FRONTEND_URL:-https://paoxx.com/}"
ADMIN_CONSOLE_URL="${ADMIN_CONSOLE_URL:-https://paoxx.com/admin}"
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

admin_login_status() {
  local username password_hash
  username="$(get_env_value WEB_ADMIN_USERNAME)"
  password_hash="$(get_env_value WEB_ADMIN_PASSWORD_HASH)"
  [ -n "$username" ] || username="admin"
  if [ -n "$password_hash" ]; then
    printf '使用自定义账号 + 密码（用户：%s）' "$username"
  else
    printf '尚未设置，请选择 2 设置后台账号密码'
  fi
}

show_web_entry() {
  local host port
  host="$(get_env_value WEB_HOST)"
  port="$(get_env_value WEB_PORT)"
  [ -n "$host" ] || host="0.0.0.0"
  [ -n "$port" ] || port="8080"
  printf '公开前台: %s\n' "$PUBLIC_FRONTEND_URL"
  printf '后台控制台: %s\n' "$ADMIN_CONSOLE_URL"
  printf '本机监听配置: %s:%s\n' "$host" "$port"
  printf '后台登录: %s\n' "$(admin_login_status)"
  printf '\n说明:\n'
  printf '  1. 日常访问请使用 %s。\n' "$ADMIN_CONSOLE_URL"
  printf '  2. 8080 仅作为 Nginx 反代后端入口，不作为公网入口。\n'
  printf '  3. 如需设置或重置后台登录密码，请使用菜单项 2。\n'
}

set_admin_password() {
  run_main admin-password set
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
  cat <<EOF
============================================================
泡泡雷达中文控制台
============================================================
项目目录: ${APP_DIR}
当前版本: $(project_version) ($(project_commit))

公开前台: ${PUBLIC_FRONTEND_URL}
后台控制台: ${ADMIN_CONSOLE_URL}
后台登录: $(admin_login_status)

说明:
  1. 日常访问请使用 ${ADMIN_CONSOLE_URL}。
  2. 8080 仅作为 Nginx 反代后端入口，不作为公网入口。
  3. 如需设置或重置后台登录密码，请使用菜单项 2。
  4. 配置修改、日志查看、主服务/结构雷达启停、Telegram 测试、
     readiness、doctor、cleanup、结构复盘，都在 Web 页面里操作。
  5. 这个中文菜单只保留最常用的服务器维护动作，避免记长命令。

请选择:
  1. 查看正式访问入口
  2. 设置后台账号密码
  3. 查看 Web 控制台服务状态
  4. 查看 Web 控制台实时日志
  5. 重启 Web 控制台服务
  6. 检查 GitHub 是否有更新
  7. 更新项目代码
  8. 查看当前版本
  0. 退出
============================================================
EOF
}

show_menu() {
  while true; do
    show_menu_header
    read -r -p "输入编号: " choice
    case "$choice" in
      1) show_web_entry; pause_menu ;;
      2) set_admin_password; pause_menu ;;
      3) show_web_status; pause_menu ;;
      4) show_web_logs ;;
      5) restart_web_service; pause_menu ;;
      6) update_project --check; pause_menu ;;
      7) update_project --yes; pause_menu ;;
      8) show_version; pause_menu ;;
      0) exit 0 ;;
      *) printf '无效选项，请输入 0-8。\n'; pause_menu ;;
    esac
  done
}

show_help() {
  cat <<EOF
泡泡雷达中文控制台

入口指令:
  paopao

正式访问入口:
  公开前台: ${PUBLIC_FRONTEND_URL}
  后台控制台: ${ADMIN_CONSOLE_URL}

说明:
  输入 paopao 后，直接用数字选择功能。
  配置修改、服务启停、日志查看、测试消息、readiness、doctor、cleanup、结构复盘等日常控制功能，都在 Web 页面里完成。
  服务器菜单只保留正式入口、令牌查看、Web 服务排查、项目更新和版本查看。

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
  web-token|token)
    printf '旧访问令牌查看已不作为默认后台登录方式。请使用: paopao admin-password\n'
    ;;
  admin-password|password)
    set_admin_password
    ;;
  url)
    show_web_entry
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
