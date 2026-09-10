#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
HEALTH_SERVICE_NAME="${HEALTH_SERVICE_NAME:-paopao-health}"
BACKUP_SERVICE_NAME="${BACKUP_SERVICE_NAME:-paopao-backup}"
PRIVATE_CONTROL_SERVICE_NAME="${PRIVATE_CONTROL_SERVICE_NAME:-paopao-private-control}"
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

service_state_cn() {
  case "$(service_state "$1")" in
    active) printf '运行中\n' ;;
    inactive) printf '已停止\n' ;;
    failed) printf '运行失败\n' ;;
    activating) printf '正在启动\n' ;;
    deactivating) printf '正在停止\n' ;;
    *) printf '状态未知\n' ;;
  esac
}

main_bot_mode_cn() {
  case "$1" in
    real) printf '真实发送\n' ;;
    dry_run) printf '安全演练（不发送）\n' ;;
    *) printf '模式未知\n' ;;
  esac
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

show_config_status_cn() {
  run_config status --json 2>/dev/null | "$PYTHON_BIN" -c '
import json, sys

labels = {
    "TG_BOT_TOKEN": "Telegram 机器人密钥",
    "TG_CHAT_ID": "Telegram 目标群",
    "TG_PRIVATE_CONTROL_ENABLE": "管理员私聊菜单",
    "TG_PRIVATE_CONTROL_ADMIN_USER_ID": "私聊管理员",
    "TG_PRIVATE_CONTROL_ALERT_ENABLE": "私聊主动故障提醒",
    "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC": "故障提醒冷却秒数",
    "MAIN_BOT_DELIVERY_MODE": "主 BOT 发送模式",
    "MAIN_BOT_REAL_SEND": "主 BOT 真实发送",
    "MAIN_BOT_REAL_SEND_ACK": "主 BOT 真实发送确认",
    "PULSE_RADAR_ENABLE": "脉冲雷达自动运行",
    "RADAR_SUMMARY_ENABLE": "资金摘要自动运行",
    "FUNDING_ALERT_ENABLE": "资金费率警报自动运行",
    "FLOW_RADAR_ENABLE": "五因子资金流自动运行",
    "ANNOUNCEMENT_RISK_ENABLE": "公告风险自动运行",
}
values = json.load(sys.stdin)
translations = {
    "configured": "已配置",
    "not_configured": "未配置",
    "dry_run": "安全演练（不发送）",
    "real": "真实发送",
}
for key, value in values.items():
    if isinstance(value, bool):
        shown = "已开启" if value else "已关闭"
    else:
        shown = translations.get(str(value), str(value))
    print(f"{labels.get(key, key)}：{shown}")
'
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
  printf '版本：'
  if [[ -f "${APP_DIR}/VERSION" ]]; then cat "${APP_DIR}/VERSION"; else printf '未知\n'; fi
  (cd "$APP_DIR" && git rev-parse --short HEAD)
}

config_set() {
  run_config set "$1"
}

set_private_control_admin() {
  local was_active=0
  local was_enabled=0
  local unit_present=0
  if systemctl cat "$PRIVATE_CONTROL_SERVICE_NAME" >/dev/null 2>&1; then
    unit_present=1
    if [[ "$(service_state "$PRIVATE_CONTROL_SERVICE_NAME")" == "active" ]]; then
      was_active=1
    fi
    if systemctl is-enabled --quiet "$PRIVATE_CONTROL_SERVICE_NAME" 2>/dev/null; then
      was_enabled=1
    fi
    if ! run_root systemctl disable --now "$PRIVATE_CONTROL_SERVICE_NAME"; then
      printf '无法安全停止并取消私聊菜单开机自启，管理员没有修改。\n' >&2
      return 1
    fi
  fi
  if ! config_set TG_PRIVATE_CONTROL_ADMIN_USER_ID; then
    printf '管理员修改失败；为防止旧管理员继续操作，私聊菜单保持停止且不随开机启动。\n' >&2
    return 1
  fi
  if [[ "$unit_present" == "1" && "$was_enabled" == "1" ]]; then
    if ! run_root systemctl enable "$PRIVATE_CONTROL_SERVICE_NAME"; then
      printf '管理员已修改，但私聊菜单无法恢复开机自启，请查看 FinalShell 日志。\n' >&2
      return 1
    fi
  fi
  if [[ "$was_active" == "1" ]]; then
    if ! run_root systemctl start "$PRIVATE_CONTROL_SERVICE_NAME"; then
      printf '管理员已修改，但私聊菜单启动失败，请查看 FinalShell 日志。\n' >&2
      return 1
    fi
  fi
}

restart_main_services() {
  run_root systemctl restart "$MARKET_STREAM_SERVICE_NAME" "$SERVICE_NAME"
  run_root systemctl --no-pager --full status \
    "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" || true
}

restart_main_bot_from_menu() {
  local mode
  mode="$(main_bot_mode)"
  printf '当前主 BOT 模式：%s\n' "$(main_bot_mode_cn "$mode")"
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
      3) show_config_status_cn; pause_menu ;;
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
3. 主 BOT 自动诊断
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
      5) run_main stable-check --no-save; pause_menu ;;
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
2. 重启主 BOT 与市场数据服务
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
Telegram 配置
1. 查看脱敏配置状态
2. 设置 Telegram Bot Token
3. 设置 Telegram Chat ID
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_config_status_cn; pause_menu ;;
      2) config_set TG_BOT_TOKEN; pause_menu ;;
      3) config_set TG_CHAT_ID; pause_menu ;;
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
3. 管理员私聊菜单
4. 主 BOT readiness
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_config_status_cn; pause_menu ;;
      2) telegram_topic_setup_menu ;;
      3) private_control_menu ;;
      4) run_main readiness; pause_menu ;;
      0) return ;;
    esac
  done
}

private_control_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
管理员私聊菜单
1. 查看脱敏配置与服务状态
2. 设置管理员 Telegram User ID
3. 开启并启动私聊菜单
4. 停止并关闭私聊菜单
5. 重启私聊菜单服务
6. 查看私聊菜单日志
0. 返回

说明：这里只绑定管理员和管理独立服务。
真实发送、部署回滚和完整诊断仍保留在 FinalShell。
EOF
    IFS= read -r choice
    case "$choice" in
      1)
        show_config_status_cn
        printf '私聊菜单服务：%s\n' "$(service_state_cn "$PRIVATE_CONTROL_SERVICE_NAME")"
        pause_menu
        ;;
      2) set_private_control_admin || true; pause_menu ;;
      3)
        if confirm_phrase "启用管理员私聊菜单"; then
          if run_config enable TG_PRIVATE_CONTROL_ENABLE; then
            run_root systemctl enable --now "$PRIVATE_CONTROL_SERVICE_NAME"
            run_root systemctl --no-pager --full status "$PRIVATE_CONTROL_SERVICE_NAME" || true
          fi
        fi
        pause_menu
        ;;
      4)
        if confirm_phrase "关闭管理员私聊菜单"; then
          run_root systemctl disable --now "$PRIVATE_CONTROL_SERVICE_NAME" || true
          run_config disable TG_PRIVATE_CONTROL_ENABLE
        fi
        pause_menu
        ;;
      5)
        confirm_phrase "重启管理员私聊菜单" && \
          run_root systemctl restart "$PRIVATE_CONTROL_SERVICE_NAME"
        pause_menu
        ;;
      6)
        run_root journalctl -u "$PRIVATE_CONTROL_SERVICE_NAME" -n 200 --no-pager
        pause_menu
        ;;
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
2. 脉冲雷达
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
3. 市场数据服务最近日志
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
4. 服务启动配置内容
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
    printf '主 BOT：%s | 市场数据服务：%s\n\n' \
      "$(service_state_cn "$SERVICE_NAME")" \
      "$(service_state_cn "$MARKET_STREAM_SERVICE_NAME")"
    cat <<'EOF'
1. 总览与健康检查
2. 服务管理
3. 检查更新与版本
4. Telegram 配置
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
  config-status)
    if [[ "${1:-}" == "--json" ]]; then
      run_config status "$@"
    else
      show_config_status_cn
    fi
    ;;
  help|-h|--help) show_help ;;
  *) printf '未知命令: %s\n\n' "$command" >&2; show_help; exit 2 ;;
esac
