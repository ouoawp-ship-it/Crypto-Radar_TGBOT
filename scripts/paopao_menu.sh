#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
HEALTH_SERVICE_NAME="${HEALTH_SERVICE_NAME:-paopao-health}"
BACKUP_SERVICE_NAME="${BACKUP_SERVICE_NAME:-paopao-backup}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="${PAOPAO_PYTHON_BIN:-python3}"
UPDATE_SCRIPT="${PAOPAO_UPDATE_SCRIPT:-${APP_DIR}/scripts/update_server.sh}"

run_root() {
  if [[ "$(id -u)" -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

run_main() {
  (cd "$APP_DIR" && "$PYTHON_BIN" main.py "$@")
}

run_config() {
  (cd "$APP_DIR" && "$PYTHON_BIN" scripts/paopao_config.py "$@")
}

service_state() {
  systemctl is-active "$1" 2>/dev/null || true
}

confirm_phrase() {
  local expected="$1"
  local actual=""
  printf '请输入完整确认短语“%s”：' "$expected"
  IFS= read -r actual
  [[ "$actual" == "$expected" ]]
}

pause_menu() {
  IFS= read -r -p "按回车键返回..." _unused
}

menu_header() {
  printf '\n============================================================\n'
  printf ' 泡泡 Crypto Radar · FinalShell 中文运维菜单\n'
  printf '============================================================\n'
}

main_bot_mode() {
  run_config status --json 2>/dev/null | "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    value = {}
print(value.get("MAIN_BOT_DELIVERY_MODE") or "dry_run")
' || printf 'dry_run\n'
}

show_status() {
  run_main status
  if command -v systemctl >/dev/null 2>&1; then
    run_root systemctl --no-pager --full status \
      "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" \
      "${HEALTH_SERVICE_NAME}.timer" "${BACKUP_SERVICE_NAME}.timer" || true
  fi
}

show_logs() {
  command -v journalctl >/dev/null 2>&1 || return 0
  run_root journalctl -u "$SERVICE_NAME" -u "$MARKET_STREAM_SERVICE_NAME" -f
}

show_version() {
  printf 'Version: '
  if [[ -f "${APP_DIR}/VERSION" ]]; then cat "${APP_DIR}/VERSION"; else printf 'unknown\n'; fi
  (cd "$APP_DIR" && git rev-parse --short HEAD)
}

config_set() {
  run_config set "$1"
}

restart_main_services() {
  run_root systemctl restart "$MARKET_STREAM_SERVICE_NAME" "$SERVICE_NAME"
  run_root systemctl --no-pager --full status \
    "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" || true
}

restart_main_bot_from_menu() {
  local mode
  mode="$(main_bot_mode)"
  printf '当前主 BOT 模式：%s\n' "$mode"
  if [[ "$mode" == "real" ]]; then
    confirm_phrase "重启真实主BOT" || return 1
  else
    confirm_phrase "重启主BOT" || return 1
  fi
  run_root systemctl restart "$SERVICE_NAME"
}

main_bot_delivery_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
主 BOT 运行模式
1. 安全 Dry-run
2. Real 真实发送
3. 查看当前脱敏状态
4. 重启主 BOT
5. 停止主 BOT
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config main-bot-delivery dry-run; pause_menu ;;
      2)
        if confirm_phrase "启用真实主BOT提醒"; then
          run_config main-bot-delivery real
        fi
        pause_menu
        ;;
      3) run_config status; pause_menu ;;
      4) restart_main_bot_from_menu; pause_menu ;;
      5) confirm_phrase "停止主BOT" && run_root systemctl stop "$SERVICE_NAME"; pause_menu ;;
      0) return ;;
    esac
  done
}

overview_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
总览与健康检查
1. 服务与五雷达状态
2. 主 BOT 状态
3. 主 BOT Doctor
4. 真实推送准备度
5. 稳定性检查
6. 磁盘与内存
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_status; pause_menu ;;
      2) run_main status; pause_menu ;;
      3) run_main doctor; pause_menu ;;
      4) run_main readiness; pause_menu ;;
      5) run_main stable-check --json --no-save; pause_menu ;;
      6) df -h "$APP_DIR"; free -h 2>/dev/null || true; pause_menu ;;
      0) return ;;
    esac
  done
}

service_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
服务管理
1. 服务状态
2. 重启主 BOT 与 Market Stream
3. 停止主 BOT
4. 主 BOT 运行模式
5. 服务资源状态
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_status; pause_menu ;;
      2) confirm_phrase "重启主服务" && restart_main_services; pause_menu ;;
      3) confirm_phrase "停止主BOT" && run_root systemctl stop "$SERVICE_NAME"; pause_menu ;;
      4) main_bot_delivery_menu ;;
      5) run_root systemctl show "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" -p MainPID,NRestarts,MemoryCurrent,CPUUsageNSec; pause_menu ;;
      0) return ;;
    esac
  done
}

update_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
检查更新与版本
1. 当前版本与最近 Commit
2. 检查更新
3. 安全更新
4. 恢复点列表
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_version; (cd "$APP_DIR" && git log -5 --oneline); pause_menu ;;
      2) (cd "$APP_DIR" && bash "$UPDATE_SCRIPT" --check); pause_menu ;;
      3) confirm_phrase "执行安全更新" && (cd "$APP_DIR" && bash "$UPDATE_SCRIPT" --yes); pause_menu ;;
      4) find "${APP_DIR}/backups" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null | sort -r | head -50; pause_menu ;;
      0) return ;;
    esac
  done
}

api_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
API、Token 与密钥
1. 查看脱敏配置状态
2. 设置 Telegram Bot Token
3. 设置 Telegram Chat ID
4. 设置 AI API Key
5. 设置 AI 接口地址
6. 设置 AI 模型
7. 设置 AI 超时秒数
8. 设置多周期方向雷达开关
9. 设置 AI 白话解读开关
10. 设置每轮深度候选数量
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config status; pause_menu ;;
      2) config_set TG_BOT_TOKEN; pause_menu ;;
      3) config_set TG_CHAT_ID; pause_menu ;;
      4) config_set AI_API_KEY; pause_menu ;;
      5) config_set AI_BASE_URL; pause_menu ;;
      6) config_set AI_MODEL; pause_menu ;;
      7) config_set AI_TIMEOUT_SEC; pause_menu ;;
      8) config_set LAUNCH_DIRECTIONAL_ENABLE; pause_menu ;;
      9) config_set LAUNCH_AI_INTERPRETER_ENABLE; pause_menu ;;
      10) config_set LAUNCH_DIRECTIONAL_MAX_CANDIDATES; pause_menu ;;
      0) return ;;
    esac
  done
}

telegram_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
Telegram 设置与测试
1. 查看共享 Telegram 脱敏状态
2. 手工创建/修复话题并置顶说明
3. 主 BOT readiness
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config status; pause_menu ;;
      2) telegram_topic_setup_menu ;;
      3) run_main readiness; pause_menu ;;
      0) return ;;
    esac
  done
}

telegram_topic_setup_menu() {
  local choice template
  while true; do
    menu_header
    cat <<'EOF'
手工话题与置顶说明
1. 资金摘要
2. 启动预警
3. 公告风险
4. 测试消息
5. 资金流雷达
6. 资金费率警报
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) template="TG_RADAR_SUMMARY" ;;
      2) template="TG_LAUNCH_ALERT" ;;
      3) template="TG_ANNOUNCEMENT_ALERT" ;;
      4) template="TG_TEST_MESSAGE" ;;
      5) template="TG_FLOW_RADAR" ;;
      6) template="TG_FUNDING_ALERT" ;;
      0) return ;;
      *) printf '无效选项。\n'; continue ;;
    esac
    if confirm_phrase "创建并置顶话题说明"; then
      run_main telegram-topic-setup \
        --topic-template "$template" \
        --send --confirm-real-send
    fi
    pause_menu
  done
}

data_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
数据库、备份与清理
1. 创建数据库备份
2. 清理过期缓存
3. 查看数据目录大小
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_main database-backup; pause_menu ;;
      2) confirm_phrase "清理过期缓存" && run_main cleanup --force-cleanup; pause_menu ;;
      3) du -sh "${APP_DIR}/data" 2>/dev/null || true; pause_menu ;;
      0) return ;;
    esac
  done
}

log_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
日志与故障诊断
1. 主 BOT 最近日志
2. 主 BOT 跟随日志
3. Market Stream 最近日志
4. 两个服务错误日志
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_root journalctl -u "$SERVICE_NAME" -n 200 --no-pager; pause_menu ;;
      2) run_root journalctl -u "$SERVICE_NAME" -f ;;
      3) run_root journalctl -u "$MARKET_STREAM_SERVICE_NAME" -n 200 --no-pager; pause_menu ;;
      4) run_root journalctl -p err -u "$SERVICE_NAME" -u "$MARKET_STREAM_SERVICE_NAME" -n 200 --no-pager; pause_menu ;;
      0) return ;;
    esac
  done
}

advanced_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
高级运维
1. 配置校验
2. 配置回滚列表
3. 真实 Telegram 测试
4. systemd Unit 内容
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config validate; pause_menu ;;
      2) run_config backups oi; pause_menu ;;
      3) confirm_phrase "发送真实测试" && run_main telegram-test --send --confirm-real-send; pause_menu ;;
      4) run_root systemctl cat "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME"; pause_menu ;;
      0) return ;;
    esac
  done
}

interactive_menu() {
  local choice
  while true; do
    menu_header
    printf '主 BOT: %s | Market Stream: %s\n\n' \
      "$(service_state "$SERVICE_NAME")" \
      "$(service_state "$MARKET_STREAM_SERVICE_NAME")"
    cat <<'EOF'
1. 总览与健康检查
2. 服务管理
3. 检查更新与版本
4. API、Token 与密钥
5. Telegram 设置与测试
6. 数据库、备份与清理
7. 日志与故障诊断
8. 高级运维
0. 退出
EOF
    IFS= read -r choice
    case "$choice" in
      1) overview_menu ;;
      2) service_menu ;;
      3) update_menu ;;
      4) api_menu ;;
      5) telegram_menu ;;
      6) data_menu ;;
      7) log_menu ;;
      8) advanced_menu ;;
      0) return ;;
      *) printf '无效选项。\n'; sleep 1 ;;
    esac
  done
}

show_help() {
  cat <<'EOF'
用法: paopao [命令]

  menu                打开中文菜单
  status              服务状态
  radar-status        五个雷达运行状态
  doctor              主 BOT 诊断
  readiness           真实推送准备度
  config-status       脱敏配置状态
  check-update        检查更新
  update              安全更新
EOF
}

command="${1:-}"
if [[ "$#" -gt 0 ]]; then shift; fi

if [[ -z "$command" || "$command" == "menu" ]]; then
  if [[ -t 0 && -t 1 ]]; then interactive_menu; else show_help; fi
  exit 0
fi

case "$command" in
  status) show_status ;;
  logs) show_logs ;;
  restart) restart_main_services ;;
  doctor) run_main doctor "$@" ;;
  readiness) run_main readiness "$@" ;;
  radar-status) run_main radar-status "$@" ;;
  stable-check) run_main stable-check "$@" ;;
  backup|database-backup) run_main database-backup "$@" ;;
  telegram-test) run_main telegram-test "$@" ;;
  cleanup) run_main cleanup --force-cleanup "$@" ;;
  check-update|check) (cd "$APP_DIR" && bash "$UPDATE_SCRIPT" --check) ;;
  update) (cd "$APP_DIR" && bash "$UPDATE_SCRIPT" --yes) ;;
  version) show_version ;;
  config-status) run_config status "$@" ;;
  help|-h|--help) show_help ;;
  *) printf '未知命令: %s\n\n' "$command" >&2; show_help; exit 2 ;;
esac
