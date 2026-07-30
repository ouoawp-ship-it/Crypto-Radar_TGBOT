#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
OAR_SERVICE_NAME="${OAR_SERVICE_NAME:-paopao-oar-watch}"
HEALTH_SERVICE_NAME="${HEALTH_SERVICE_NAME:-paopao-health}"
BACKUP_SERVICE_NAME="${BACKUP_SERVICE_NAME:-paopao-backup}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="${PAOPAO_PYTHON_BIN:-python3}"
UPDATE_SCRIPT="${PAOPAO_UPDATE_SCRIPT:-${APP_DIR}/scripts/update_server.sh}"

run_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

run_main() {
  (cd "$APP_DIR" && "$PYTHON_BIN" main.py "$@")
}

run_onchain() {
  (cd "$APP_DIR" && "$PYTHON_BIN" onchain_main.py "$@")
}

run_config() {
  (cd "$APP_DIR" && "$PYTHON_BIN" scripts/paopao_config.py "$@")
}

service_state() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl is-active "$1" 2>/dev/null || printf 'inactive\n'
  else
    printf 'unavailable\n'
  fi
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

restart_services() {
  run_root systemctl restart "$MARKET_STREAM_SERVICE_NAME" "$SERVICE_NAME"
  run_root systemctl --no-pager --full status \
    "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" || true
}

show_version() {
  (cd "$APP_DIR" && {
    printf '版本: %s\n' "$(head -n 1 VERSION 2>/dev/null || printf unknown)"
    printf '提交: %s\n' "$(git rev-parse --short HEAD 2>/dev/null || printf unknown)"
  })
}

show_help() {
  cat <<'EOF'
Paopao Telegram Radar BOT-only 控制命令

  paopao | paopao menu  在交互式 TTY 中打开中文菜单
  paopao status         查看 BOT、实时行情服务与配置状态
  paopao logs           跟踪 BOT 与实时行情日志
  paopao restart        重启 BOT 与实时行情服务
  paopao doctor         输出环境诊断
  paopao readiness      检查真实推送门禁
  paopao stable-check   执行 BOT 稳定性检查
  paopao providers      验收 CoinGlass/Coinalyze 数据源
  paopao backup         创建并恢复验证 SQLite 备份
  paopao telegram-test  执行 Telegram dry-run 测试
  paopao cleanup        清理运行期缓存
  paopao check-update   检查 GitHub 更新
  paopao update         拉取、测试并发布 GitHub 更新
  paopao version        查看版本与提交

非 TTY 环境不会打开全屏菜单，也不会自动发起网络、AI、RPC 或 Telegram 请求。
EOF
}

confirm_phrase() {
  local expected="$1"
  local entered=""
  printf '高风险操作。请输入完整确认短语“%s”：' "$expected"
  IFS= read -r entered
  if [ "$entered" != "$expected" ]; then
    printf '确认短语不匹配，操作已取消。\n'
    return 1
  fi
}

pause_menu() {
  printf '\n按回车键返回...'
  IFS= read -r _unused
}

menu_header() {
  if [ "${PAOPAO_MENU_NO_CLEAR:-0}" != "1" ] && command -v clear >/dev/null 2>&1; then
    clear
  fi
  printf '============================================================\n'
  printf ' 泡泡 Crypto Radar · FinalShell 中文运维菜单\n'
  printf '============================================================\n'
}

print_local_overview() {
  local oar_json config_json
  menu_header
  show_version
  printf '主 BOT:       %s\n' "$(service_state "$SERVICE_NAME")"
  printf 'Market Stream:%s\n' "$(service_state "$MARKET_STREAM_SERVICE_NAME")"
  printf 'OAR Watch:    %s\n' "$(service_state "$OAR_SERVICE_NAME")"
  config_json="$(run_config status --json 2>/dev/null || printf '{}')"
  printf '%s' "$config_json" | "$PYTHON_BIN" -c '
import json, sys
v=json.load(sys.stdin)
print("Base RPC configured:", v.get("ONCHAIN_BASE_HTTP_RPC_URL") == "configured")
print("DeepSeek configured:", v.get("OAR_AI_API_KEY") == "configured")
print("DeepSeek enabled:", bool(v.get("OAR_AI_ENABLE")))
print("On-chain Topic configured:", v.get("TG_ONCHAIN_FLOW_TOPIC_ID") == "configured")
' 2>/dev/null || true
  oar_json="$(run_onchain status 2>/dev/null || printf '{}')"
  printf '%s' "$oar_json" | "$PYTHON_BIN" -c '
import json, sys
v=json.load(sys.stdin); a=v.get("automation_status") or {}
print("Registry verified:", a.get("registry_verified", 0))
print("Active Watch:", a.get("active_watch_items", 0))
print("Open unresolved:", a.get("unresolved_signals", 0))
print("Last scan:", a.get("last_scan_status") or "none")
print("Automation DB:", a.get("status") or "not_initialized")
' 2>/dev/null || true
  printf '\n菜单打开过程只读取本地状态，不执行 fetch、RPC、AI、Telegram 或完整测试。\n'
}

system_resources() {
  command -v free >/dev/null 2>&1 && free -h || true
  df -h "$APP_DIR" || true
}

oar_systemd_main_pid() {
  local main_pid
  main_pid="$(systemctl show "$OAR_SERVICE_NAME" \
    --property MainPID --value 2>/dev/null || true)"
  if [[ "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s\n' "$main_pid"
  else
    printf '0\n'
  fi
}

oar_unit_installed() {
  command -v systemctl >/dev/null 2>&1 && \
    systemctl cat "$OAR_SERVICE_NAME" >/dev/null 2>&1
}

show_oar_workers() {
  local main_pid matches
  main_pid="$(oar_systemd_main_pid)"
  matches="$(pgrep -af '[o]nchain_main.py.*watch-live' 2>/dev/null || true)"
  printf 'systemd MainPID: %s\n' "$main_pid"
  if [ -z "$matches" ]; then
    printf '未发现 OAR Watch Worker。\n'
  else
    printf '%s\n' "$matches"
  fi
}

assert_no_conflicting_oar_worker() {
  local main_pid matches line pid conflicts=""
  main_pid="$(oar_systemd_main_pid)"
  matches="$(pgrep -af '[o]nchain_main.py.*watch-live' 2>/dev/null || true)"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pid="${line%% *}"
    if [ "$pid" != "$main_pid" ]; then
      conflicts="${conflicts}${line}"$'\n'
    fi
  done <<<"$matches"
  if [ -n "$conflicts" ]; then
    printf 'duplicate_writer_risk\n' >&2
    printf '%s' "$conflicts" >&2
    return 1
  fi
}

start_oar_watch() {
  local main_pid
  if ! oar_unit_installed; then
    printf '服务尚未安装：%s.service\n' "$OAR_SERVICE_NAME"
    return 0
  fi
  if ! assert_no_conflicting_oar_worker; then
    printf '检测到额外手工 Worker，拒绝启动第二个 Writer。\n'
    return 0
  fi
  main_pid="$(oar_systemd_main_pid)"
  if [ "$main_pid" != "0" ]; then
    printf 'OAR Watch 已由 systemd 运行，MainPID=%s。\n' "$main_pid"
    return 0
  fi
  if ! run_root systemctl start "$OAR_SERVICE_NAME"; then
    printf 'OAR Watch 启动失败，请检查 systemctl status。\n' >&2
    return 0
  fi
  printf 'OAR Watch 状态：%s\n' "$(service_state "$OAR_SERVICE_NAME")"
}

stop_oar_watch() {
  if ! oar_unit_installed; then
    printf '服务尚未安装：%s.service\n' "$OAR_SERVICE_NAME"
    return 0
  fi
  if ! run_root systemctl stop "$OAR_SERVICE_NAME"; then
    printf 'OAR Watch 停止失败，请检查 systemctl status。\n' >&2
  fi
  show_oar_workers
}

restart_oar_watch() {
  local main_pid
  if ! oar_unit_installed; then
    printf '服务尚未安装：%s.service\n' "$OAR_SERVICE_NAME"
    return 0
  fi
  if ! assert_no_conflicting_oar_worker; then
    printf '检测到额外手工 Worker，拒绝重启以避免双 Writer。\n'
    return 0
  fi
  main_pid="$(oar_systemd_main_pid)"
  if [ "$main_pid" = "0" ]; then
    printf 'OAR Watch 当前未由 systemd 运行，请使用启动操作。\n'
    return 0
  fi
  if ! run_root systemctl restart "$OAR_SERVICE_NAME"; then
    printf 'OAR Watch 重启失败，请检查 systemctl status。\n' >&2
    return 0
  fi
  printf 'OAR Watch 状态：%s\n' "$(service_state "$OAR_SERVICE_NAME")"
}

config_set() {
  local key="$1"
  run_config set "$key"
}

prompt_edit() {
  local temp_file
  mkdir -p "${APP_DIR}/data/onchain/config"
  temp_file="$(mktemp "${APP_DIR}/data/onchain/config/.operator-prompt.XXXXXX")"
  chmod 600 "$temp_file" 2>/dev/null || true
  run_onchain ai-prompt show >"$temp_file" 2>/dev/null || \
    cp "${APP_DIR}/config/onchain/oar_ai_operator_prompt.default.txt" "$temp_file"
  "${EDITOR:-vi}" "$temp_file"
  run_onchain ai-prompt save --stdin <"$temp_file"
  rm -f "$temp_file"
}

prompt_rollback() {
  local version
  run_onchain ai-prompt history
  printf '请输入要恢复的版本或 Hash 前缀：'
  IFS= read -r version
  confirm_phrase "恢复提示词" || return 0
  run_onchain ai-prompt rollback --version "$version"
}

clear_ai_cache() {
  confirm_phrase "清理AI缓存" || return 0
  run_onchain ai-cache clear-results
}

config_rollback() {
  local target version
  printf '选择配置（oi/onchain）：'
  IFS= read -r target
  run_config backups "$target"
  printf '请输入备份文件名：'
  IFS= read -r version
  confirm_phrase "回滚配置" || return 0
  run_config rollback "$target" --version "$version"
}

registry_verify_menu() {
  local token_key mode output code
  local -a verify_args
  printf 'Token Key：'
  IFS= read -r token_key
  cat <<'EOF'
Registry 验证：
1. 验证并设为 Primary
2. 仅验证为 Secondary
0. 取消
EOF
  IFS= read -r mode
  case "$mode" in
    1) verify_args=(registry-verify --token-key "$token_key" --allow-network --set-primary) ;;
    2) verify_args=(registry-verify --token-key "$token_key" --allow-network) ;;
    0) return 0 ;;
    *) printf '无效选项。\n'; return 0 ;;
  esac
  set +e
  output="$(run_onchain "${verify_args[@]}" 2>&1)"
  code=$?
  set -e
  printf '%s\n' "$output"
  if [ "$code" -eq 0 ]; then
    return 0
  fi
  if [[ "$output" != *"symbol_mismatch_requires_confirmation"* ]]; then
    return 0
  fi
  confirm_phrase "接受Symbol不一致" || return 0
  run_onchain "${verify_args[@]}" --accept-symbol-mismatch
}

diagnostic_export() {
  local directory path temporary
  directory="${APP_DIR}/reports/onchain"
  mkdir -p "$directory"
  chmod 700 "$directory" 2>/dev/null || true
  path="${directory}/diagnostic-$(date -u +%Y%m%dT%H%M%SZ)-$$.txt"
  temporary="${path}.tmp"
  {
    printf 'generated_at=%s\n' "$(date -u +%FT%TZ)"
    show_version
    run_main doctor || true
    run_onchain doctor || true
    run_config status || true
  } >"$temporary"
  chmod 600 "$temporary" 2>/dev/null || true
  mv "$temporary" "$path"
  printf '脱敏诊断已写入：%s\n' "$path"
}

token_query() {
  local command="$1" contract window
  printf 'Base Token 合约：'
  IFS= read -r contract
  printf '窗口（15m/1h/4h/24h）：'
  IFS= read -r window
  run_onchain "$command" --chain base --contract "$contract" \
    --window "$window" --allow-network --pretty
}

telegram_dry_run() {
  local contract window
  printf '已验证 Base Token 合约：'
  IFS= read -r contract
  printf '窗口（15m/1h/4h/24h）：'
  IFS= read -r window
  run_onchain token-notify --chain base --contract "$contract" \
    --window "$window" --allow-network
}

overview_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
总览与健康检查
1. 本地轻量总览
2. 主 BOT status
3. 主 BOT doctor
4. readiness
5. stable-check
6. Provider 检查
7. OAR status
8. OAR doctor
9. 磁盘与内存
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) print_local_overview; pause_menu ;;
      2) run_main status; pause_menu ;;
      3) run_main doctor; pause_menu ;;
      4) run_main readiness; pause_menu ;;
      5) run_main stable-check --json --no-save; pause_menu ;;
      6) run_main provider-check; pause_menu ;;
      7) run_onchain status; pause_menu ;;
      8) run_onchain doctor; pause_menu ;;
      9) system_resources; pause_menu ;;
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
4. 启动 OAR Watch
5. 停止 OAR Watch
6. 重启 OAR Watch
7. 重复 Worker 检查
8. 服务资源状态
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_status; run_root systemctl status "$OAR_SERVICE_NAME" --no-pager || true; pause_menu ;;
      2)
        if confirm_phrase "重启主服务"; then
          restart_services || printf '主服务重启失败，请检查 systemctl status。\n' >&2
        fi
        pause_menu
        ;;
      3)
        if confirm_phrase "停止主BOT"; then
          run_root systemctl stop "$SERVICE_NAME" || \
            printf '主 BOT 停止失败，请检查 systemctl status。\n' >&2
        fi
        pause_menu
        ;;
      4) start_oar_watch; pause_menu ;;
      5) stop_oar_watch; pause_menu ;;
      6) restart_oar_watch; pause_menu ;;
      7) show_oar_workers; pause_menu ;;
      8) run_root systemctl show "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" "$OAR_SERVICE_NAME" -p MainPID,NRestarts,MemoryCurrent,CPUUsageNSec || true; pause_menu ;;
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
4. 设置 CoinGlass API Key
5. 设置 Coinalyze API Key
6. 设置 Base RPC
7. 设置 DeepSeek API Key
8. 主 Provider 检查
9. Base RPC Smoke（手工输入合约）
10. 设置 Base RPC 最大区块范围（高级）
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config status; pause_menu ;;
      2) config_set TG_BOT_TOKEN; pause_menu ;;
      3) config_set TG_CHAT_ID; pause_menu ;;
      4) config_set COINGLASS_API_KEY; pause_menu ;;
      5) config_set COINALYZE_API_KEY; pause_menu ;;
      6) config_set ONCHAIN_BASE_HTTP_RPC_URL; pause_menu ;;
      7) config_set OAR_AI_API_KEY; pause_menu ;;
      8) run_main provider-check; pause_menu ;;
      9) token_query token-activity; pause_menu ;;
      10) config_set ONCHAIN_RPC_MAX_BLOCK_RANGE; pause_menu ;;
      0) return ;;
    esac
  done
}

ai_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
AI 模型与提示词
1. 查看 AI 配置状态
2. 应用 DeepSeek V4 Pro 推荐配置
3. 设置 DeepSeek API Key
4. 设置 AI Base URL
5. 设置模型
6. 设置 Thinking Mode
7. 设置 Reasoning Effort
8. 设置 Max Tokens
9. Prompt 状态
10. 显示 Prompt
11. 编辑 Prompt
12. 校验 Prompt
13. 恢复默认 Prompt
14. Prompt 历史与回滚
15. Provider Check
16. AI Smoke
17. AI Cache 状态
18. 清理 AI 结果缓存
19. 启用 AI
20. 禁用 AI
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config status; pause_menu ;;
      2) run_config profile deepseek-v4-pro; pause_menu ;;
      3) config_set OAR_AI_API_KEY; pause_menu ;;
      4) config_set OAR_AI_BASE_URL; pause_menu ;;
      5) config_set OAR_AI_MODEL; pause_menu ;;
      6) config_set OAR_AI_THINKING_MODE; pause_menu ;;
      7) config_set OAR_AI_REASONING_EFFORT; pause_menu ;;
      8) config_set OAR_AI_MAX_TOKENS; pause_menu ;;
      9) run_onchain ai-prompt-check; pause_menu ;;
      10) run_onchain ai-prompt show; pause_menu ;;
      11) prompt_edit; pause_menu ;;
      12) run_onchain ai-prompt validate; pause_menu ;;
      13) confirm_phrase "恢复提示词" && run_onchain ai-prompt restore-default; pause_menu ;;
      14) prompt_rollback; pause_menu ;;
      15) run_onchain ai-provider-check --allow-network; pause_menu ;;
      16) run_onchain ai-smoke --allow-network; pause_menu ;;
      17) run_onchain ai-cache status; pause_menu ;;
      18) clear_ai_cache; pause_menu ;;
      19) run_config enable OAR_AI_ENABLE; pause_menu ;;
      20) run_config disable OAR_AI_ENABLE; pause_menu ;;
      0) return ;;
    esac
  done
}

oar_menu() {
  local choice token_key symbol contract
  while true; do
    menu_header
    cat <<'EOF'
链上活动雷达
1. Registry 列表
2. Registry 添加
3. Registry 验证
4. Registry 禁用
5. Watch 列表
6. Watch 添加
7. Watch 移除
8. Signal Bridge Once
9. Watch Once
10. Unresolved 摘要
11. Token Activity
12. Token Report
13. Labels Check
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_onchain registry-list; pause_menu ;;
      2) printf 'Market Symbol：'; IFS= read -r symbol; printf 'Base Contract：'; IFS= read -r contract; run_onchain registry-add --market-symbol "$symbol" --chain base --contract "$contract" --source manual; pause_menu ;;
      3) registry_verify_menu; pause_menu ;;
      4) printf 'Token Key：'; IFS= read -r token_key; confirm_phrase "禁用Registry" && run_onchain registry-disable --token-key "$token_key"; pause_menu ;;
      5) run_onchain watch-list; pause_menu ;;
      6) printf 'Token Key：'; IFS= read -r token_key; run_onchain watch-add --token-key "$token_key"; pause_menu ;;
      7) printf 'Token Key：'; IFS= read -r token_key; run_onchain watch-remove --token-key "$token_key"; pause_menu ;;
      8) run_onchain bridge-once; pause_menu ;;
      9) run_onchain watch-once --allow-network; pause_menu ;;
      10) run_onchain unresolved-summary --limit 20; pause_menu ;;
      11) token_query token-activity; pause_menu ;;
      12) token_query token-report; pause_menu ;;
      13) run_onchain labels-check; pause_menu ;;
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
1. 查看脱敏配置
2. 设置链上 Topic ID
3. 链上报告 Dry-run
4. 主 BOT readiness
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config status; pause_menu ;;
      2) config_set TG_ONCHAIN_FLOW_TOPIC_ID; pause_menu ;;
      3) telegram_dry_run; pause_menu ;;
      4) run_main readiness; pause_menu ;;
      0) return ;;
    esac
  done
}

data_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
数据库、备份与清理
1. 数据库 quick_check
2. 创建并验证备份
3. 备份列表
4. 清理运行期缓存
5. 磁盘用量
6. 数据库真实恢复（高级保护）
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_onchain db-check; run_main doctor; pause_menu ;;
      2) run_main database-backup; pause_menu ;;
      3) find "${APP_DIR}/data/backups" "${APP_DIR}/backups" -maxdepth 2 -type f 2>/dev/null | head -100; pause_menu ;;
      4) run_main cleanup --force-cleanup; pause_menu ;;
      5) du -sh "${APP_DIR}/data" 2>/dev/null || true; df -h "$APP_DIR"; pause_menu ;;
      6) confirm_phrase "恢复数据库" && printf '安全门禁已通过；请按备份清单先执行恢复验证，菜单不会猜测目标数据库。\n'; pause_menu ;;
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
4. OAR 最近日志
5. OAR 跟随日志
6. 仅错误日志
7. 脱敏诊断
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_root journalctl -u "$SERVICE_NAME" -n 200 --no-pager; pause_menu ;;
      2) run_root journalctl -u "$SERVICE_NAME" -f ;;
      3) run_root journalctl -u "$MARKET_STREAM_SERVICE_NAME" -n 200 --no-pager; pause_menu ;;
      4) run_root journalctl -u "$OAR_SERVICE_NAME" -n 200 --no-pager; pause_menu ;;
      5) run_root journalctl -u "$OAR_SERVICE_NAME" -f ;;
      6) run_root journalctl -p err -u "$SERVICE_NAME" -u "$MARKET_STREAM_SERVICE_NAME" -u "$OAR_SERVICE_NAME" -n 200 --no-pager; pause_menu ;;
      7) diagnostic_export; pause_menu ;;
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
1. 重复 OAR Writer 检查
2. 配置校验
3. 配置回滚
4. 真实 Telegram 测试
5. systemd Unit 内容
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_oar_workers; pause_menu ;;
      2) run_config validate; pause_menu ;;
      3) config_rollback; pause_menu ;;
      4) confirm_phrase "发送真实测试" && run_main telegram-test --send --confirm-real-send; pause_menu ;;
      5) run_root systemctl cat "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" "$OAR_SERVICE_NAME" || true; pause_menu ;;
      0) return ;;
    esac
  done
}

interactive_menu() {
  local choice
  while true; do
    print_local_overview
    cat <<'EOF'

1. 总览与健康检查
2. 服务管理
3. 检查更新与版本
4. API、Token 与密钥
5. AI 模型与提示词
6. 链上活动雷达
7. Telegram 设置与测试
8. 数据库、备份与清理
9. 日志与故障诊断
10. 高级运维
0. 退出
EOF
    printf '请选择：'
    IFS= read -r choice
    case "$choice" in
      1) overview_menu ;;
      2) service_menu ;;
      3) update_menu ;;
      4) api_menu ;;
      5) ai_menu ;;
      6) oar_menu ;;
      7) telegram_menu ;;
      8) data_menu ;;
      9) log_menu ;;
      10) advanced_menu ;;
      0) return ;;
      *) printf '无效选项。\n'; sleep 1 ;;
    esac
  done
}

command="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi

if [ -z "$command" ] || [ "$command" = "menu" ]; then
  if [ -t 0 ] && [ -t 1 ]; then
    interactive_menu
  else
    show_help
  fi
  exit 0
fi

case "$command" in
  status) show_status ;;
  logs) show_logs ;;
  restart) restart_services ;;
  doctor) run_main doctor "$@" ;;
  readiness) run_main readiness "$@" ;;
  stable-check) run_main stable-check "$@" ;;
  providers|provider-check) run_main provider-check "$@" ;;
  backup|database-backup) run_main database-backup "$@" ;;
  telegram-test) run_main telegram-test "$@" ;;
  cleanup) run_main cleanup --force-cleanup "$@" ;;
  check-update|check) (cd "$APP_DIR" && bash "$UPDATE_SCRIPT" --check) ;;
  update) (cd "$APP_DIR" && bash "$UPDATE_SCRIPT" --yes) ;;
  version) show_version ;;
  config-status) run_config status "$@" ;;
  onchain-status) run_onchain status "$@" ;;
  ai-prompt-check) run_onchain ai-prompt-check "$@" ;;
  help|-h|--help) show_help ;;
  *) printf '未知命令: %s\n\n' "$command" >&2; show_help; exit 2 ;;
esac
