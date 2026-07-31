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

main_bot_mode() {
  run_config status --json 2>/dev/null | "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin).get("MAIN_BOT_DELIVERY_MODE")
except Exception:
    value = None
print(value if value in {"dry_run", "real"} else "dry_run")
' 2>/dev/null || printf 'dry_run\n'
}

show_main_bot_delivery_status() {
  local config_json
  config_json="$(run_config status --json 2>/dev/null || printf '{}')"
  printf '%s' "$config_json" | "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    value = {}
mode = value.get("MAIN_BOT_DELIVERY_MODE")
print("主 BOT 模式:", mode if mode in {"dry_run", "real"} else "dry_run")
print("Real 发送:", bool(value.get("MAIN_BOT_REAL_SEND")))
print(
    "Real ACK:",
    "configured"
    if value.get("MAIN_BOT_REAL_SEND_ACK") == "configured"
    else "not_configured",
)
print(
    "Telegram Bot:",
    "configured"
    if value.get("TG_BOT_TOKEN") == "configured"
    else "not_configured",
)
print(
    "Telegram Chat:",
    "configured"
    if value.get("TG_CHAT_ID") == "configured"
    else "not_configured",
)
' 2>/dev/null || true
}

restart_services_from_menu() {
  local mode
  mode="$(main_bot_mode)"
  show_main_bot_delivery_status
  if [ "$mode" = "real" ] &&
    ! confirm_phrase "重启真实主BOT"; then
    return 0
  fi
  restart_services
}

restart_main_bot_from_menu() {
  local mode
  mode="$(main_bot_mode)"
  show_main_bot_delivery_status
  if [ "$mode" = "real" ]; then
    confirm_phrase "重启真实主BOT" || return 0
  else
    confirm_phrase "重启主BOT" || return 0
  fi
  if ! run_root systemctl restart "$SERVICE_NAME"; then
    printf '主 BOT 重启失败，请检查 systemctl status。\n' >&2
    return 0
  fi
  printf '主 BOT 状态：%s\n' "$(service_state "$SERVICE_NAME")"
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
      1)
        if run_config main-bot-delivery dry-run; then
          printf '主 BOT 已配置为安全 Dry-run；需要显式重启后生效。\n'
        fi
        pause_menu
        ;;
      2)
        if confirm_phrase "启用真实主BOT提醒"; then
          if run_config main-bot-delivery real; then
            printf '主 BOT Real 配置已保存；不会自动启动或重启服务。\n'
          fi
        fi
        pause_menu
        ;;
      3) show_main_bot_delivery_status; pause_menu ;;
      4) restart_main_bot_from_menu; pause_menu ;;
      5)
        if confirm_phrase "停止主BOT"; then
          run_root systemctl stop "$SERVICE_NAME" || \
            printf '主 BOT 停止失败，请检查 systemctl status。\n' >&2
        fi
        pause_menu
        ;;
      0) return ;;
    esac
  done
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

watch_ai_menu() {
  local choice
  cat <<'EOF'
自动 AI：
1. 启用自动 AI 分析
2. 禁用自动 AI 分析
0. 取消
EOF
  IFS= read -r choice
  case "$choice" in
    1)
      confirm_phrase "启用自动AI分析" || return 0
      run_config watch-delivery enable-ai || \
        printf '自动 AI 偏好保存失败；全局 AI 开关未改变。\n' >&2
      ;;
    2)
      run_config watch-delivery disable-ai || \
        printf '自动 AI 偏好保存失败。\n' >&2
      ;;
    0) return 0 ;;
    *) printf '无效选项。\n' ;;
  esac
}

watch_delivery_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
Watch 通知模式
1. Observe，只观察
2. Telegram Dry-run
3. Real，真实发送
4. 自动 AI 开关
5. 查看当前脱敏状态
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1)
        if run_config watch-delivery observe; then
          printf '已切换为 Observe；重启 OAR Watch 后生效。\n'
        else
          printf 'Observe 配置保存失败。\n' >&2
        fi
        pause_menu
        ;;
      2)
        if confirm_phrase "启用链上DryRun"; then
          if run_config watch-delivery dry-run; then
            printf '已切换为 Telegram Dry-run；重启 OAR Watch 后生效。\n'
          else
            printf 'Telegram Dry-run 配置保存失败。\n' >&2
          fi
        fi
        pause_menu
        ;;
      3)
        if confirm_phrase "启用真实链上提醒"; then
          if run_config watch-delivery real; then
            printf 'Real Gate 已通过；重启 OAR Watch 后生效。\n'
          else
            printf 'real_send_gate_blocked；未更改现有配置。\n' >&2
          fi
        fi
        pause_menu
        ;;
      4) watch_ai_menu; pause_menu ;;
      5) run_config status; pause_menu ;;
      0) return ;;
      *) printf '无效选项。\n' ;;
    esac
  done
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
9. 主 BOT 运行模式
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_status; run_root systemctl status "$OAR_SERVICE_NAME" --no-pager || true; pause_menu ;;
      2)
        if confirm_phrase "重启主服务"; then
          restart_services_from_menu || printf '主服务重启失败，请检查 systemctl status。\n' >&2
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
      9) main_bot_delivery_menu ;;
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
11. 设置 Arkham API Key
12. 设置 Arkham API Base URL（高级）
13. 设置 Dune API Key（可选）
14. 设置 Dune API Base URL（高级）
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
      11) config_set ARKHAM_API_KEY; pause_menu ;;
      12) config_set ARKHAM_API_BASE_URL; pause_menu ;;
      13) config_set DUNE_API_KEY; pause_menu ;;
      14) config_set DUNE_API_BASE_URL; pause_menu ;;
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
9. 设置 AI Timeout
10. 设置 AI Max Retries
11. Prompt 状态
12. 显示 Prompt
13. 编辑 Prompt
14. 校验 Prompt
15. 恢复默认 Prompt
16. Prompt 历史与回滚
17. Provider Check
18. AI Smoke
19. AI Cache 状态
20. 清理 AI 结果缓存
21. 启用 AI
22. 禁用 AI
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
      9) config_set OAR_AI_TIMEOUT_SEC; pause_menu ;;
      10) config_set OAR_AI_MAX_RETRIES; pause_menu ;;
      11) run_onchain ai-prompt-check; pause_menu ;;
      12) run_onchain ai-prompt show; pause_menu ;;
      13) prompt_edit; pause_menu ;;
      14) run_onchain ai-prompt validate; pause_menu ;;
      15) confirm_phrase "恢复提示词" && run_onchain ai-prompt restore-default; pause_menu ;;
      16) prompt_rollback; pause_menu ;;
      17) run_onchain ai-provider-check --allow-network; pause_menu ;;
      18) run_onchain ai-smoke --allow-network; pause_menu ;;
      19) run_onchain ai-cache status; pause_menu ;;
      20) clear_ai_cache; pause_menu ;;
      21) run_config enable OAR_AI_ENABLE; pause_menu ;;
      22) run_config disable OAR_AI_ENABLE; pause_menu ;;
      0) return ;;
    esac
  done
}

cex_label_candidate_menu() {
  local choice contract candidate_id max_addresses
  while true; do
    menu_header
    cat <<'EOF'
CEX 标签候选（Arkham 仅作候选来源）
1. Arkham 配置状态
2. Arkham Provider Check
3. 发现 Base CEX 候选
4. 查看 Pending 候选
5. 批准候选
6. 拒绝候选
7. Labels Check
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_config status; pause_menu ;;
      2)
        run_onchain label-candidates provider-check --allow-network || true
        pause_menu
        ;;
      3)
        printf '已审核 Base Token 合约：'
        IFS= read -r contract
        printf '最大候选地址数（1-100，建议 50）：'
        IFS= read -r max_addresses
        run_onchain label-candidates discover \
          --chain base \
          --contract "$contract" \
          --window 4h \
          --max-addresses "$max_addresses" \
          --allow-network || true
        pause_menu
        ;;
      4)
        run_onchain label-candidates list \
          --status pending --limit 100 || true
        pause_menu
        ;;
      5)
        printf 'Candidate ID：'
        IFS= read -r candidate_id
        if confirm_phrase "批准CEX标签"; then
          run_onchain label-candidates approve \
            --candidate-id "$candidate_id" || true
        fi
        pause_menu
        ;;
      6)
        printf 'Candidate ID：'
        IFS= read -r candidate_id
        run_onchain label-candidates reject \
          --candidate-id "$candidate_id" || true
        pause_menu
        ;;
      7) run_onchain labels-check || true; pause_menu ;;
      0) return ;;
      *) printf '无效选项。\n' ;;
    esac
  done
}

address_intelligence_menu() {
  local choice provider max_addresses candidate_id import_file import_table
  while true; do
    menu_header
    cat <<'EOF'
地址情报中心
1. Provider 状态
2. 未知地址队列
3. 运行显式候选发现
4. 查看 Pending
5. 查看冲突候选
6. 批准
7. 拒绝
8. 暂缓
9. 查看本地已批准标签
10. 导入 Dune CSV
11. 导入 OLI 数据
12. 导入 BaseScan 人工 CSV
13. Labels Check
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1)
        run_onchain address-intelligence providers || true
        pause_menu
        ;;
      2)
        run_onchain address-intelligence queue --limit 50 || true
        pause_menu
        ;;
      3)
        printf 'Provider（all/dune_cex/dune_cex_deposit/oli/basescan_manual/arkham_optional/behavior_inference）：'
        IFS= read -r provider
        printf '最大地址数（1-100，且受配置上限约束；默认 50）：'
        IFS= read -r max_addresses
        case "$provider" in
          dune_cex|dune_cex_deposit|arkham_optional|all)
            run_onchain address-intelligence discover \
              --provider "$provider" \
              --max-addresses "$max_addresses" \
              --allow-network || true
            ;;
          oli|basescan_manual|behavior_inference|local_approved)
            run_onchain address-intelligence discover \
              --provider "$provider" \
              --max-addresses "$max_addresses" || true
            ;;
          *) printf '未知 Provider。\n' ;;
        esac
        pause_menu
        ;;
      4)
        run_onchain address-intelligence candidates \
          --status pending --limit 100 || true
        pause_menu
        ;;
      5)
        run_onchain address-intelligence candidates \
          --status conflicted --limit 100 || true
        pause_menu
        ;;
      6)
        printf 'Candidate ID：'
        IFS= read -r candidate_id
        if confirm_phrase "批准地址标签"; then
          run_onchain address-intelligence approve \
            --candidate-id "$candidate_id" || true
        fi
        pause_menu
        ;;
      7)
        printf 'Candidate ID：'
        IFS= read -r candidate_id
        run_onchain address-intelligence reject \
          --candidate-id "$candidate_id" || true
        pause_menu
        ;;
      8)
        printf 'Candidate ID：'
        IFS= read -r candidate_id
        run_onchain address-intelligence defer \
          --candidate-id "$candidate_id" || true
        pause_menu
        ;;
      9)
        run_onchain address-intelligence approved || true
        pause_menu
        ;;
      10)
        printf 'Dune CSV 文件路径：'
        IFS= read -r import_file
        printf '表（cex.addresses / cex.deposit_addresses）：'
        IFS= read -r import_table
        run_onchain address-intelligence import-dune \
          --file "$import_file" \
          --table "$import_table" || true
        pause_menu
        ;;
      11)
        printf 'OLI Parquet 文件路径：'
        IFS= read -r import_file
        run_onchain address-intelligence import-oli \
          --file "$import_file" || true
        pause_menu
        ;;
      12)
        printf 'BaseScan 人工 CSV 文件路径：'
        IFS= read -r import_file
        run_onchain address-intelligence import-basescan \
          --file "$import_file" || true
        pause_menu
        ;;
      13) run_onchain labels-check || true; pause_menu ;;
      0) return ;;
      *) printf '无效选项。\n' ;;
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
14. Watch 通知模式
15. CEX 标签候选
16. 地址情报中心
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) run_onchain registry-list; pause_menu ;;
      2)
        printf 'Market Symbol：'
        IFS= read -r symbol
        printf 'Base Contract：'
        IFS= read -r contract
        run_onchain registry-add \
          --market-symbol "$symbol" \
          --chain base \
          --contract "$contract" \
          --source manual
        pause_menu
        ;;
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
      14) watch_delivery_menu ;;
      15) cex_label_candidate_menu ;;
      16) address_intelligence_menu ;;
      0) return ;;
    esac
  done
}

telegram_topic_bootstrap_error_cn() {
  case "$1" in
    telegram_shared_config_missing) printf '请先在 API 菜单配置主 BOT 的 Token 和群 ID。' ;;
    telegram_auth_failed) printf '主 BOT Token 无效，请重新配置。' ;;
    telegram_chat_not_found) printf '主 BOT 当前配置群不可访问，请检查群 ID 和机器人入群状态。' ;;
    telegram_bot_not_member) printf '机器人不在当前配置群中。' ;;
    telegram_send_permission_denied) printf '机器人没有在当前群发送消息的权限。' ;;
    telegram_forum_required) printf '当前共享群不是已启用话题的超级群。' ;;
    telegram_manage_topics_permission_required) printf '机器人缺少管理话题权限，无法创建链上活动雷达话题。' ;;
    telegram_pin_permission_required) printf '机器人缺少置顶消息权限。' ;;
    telegram_topic_configuration_failed) printf '话题已处理，但本地配置保存失败，请检查配置文件权限。' ;;
    telegram_topic_not_configured) printf '链上活动雷达话题尚未配置。' ;;
    telegram_topic_intro_failed) printf '话题说明发送或置顶失败。' ;;
    telegram_timeout) printf 'Telegram 连接超时。' ;;
    telegram_dns_failed) printf 'Telegram 域名解析失败。' ;;
    telegram_tls_failed) printf 'Telegram TLS 连接失败。' ;;
    telegram_connection_failed) printf 'Telegram 连接失败。' ;;
    telegram_rate_limited) printf 'Telegram 请求受限，请稍后重试。' ;;
    *) printf '链上活动雷达话题初始化失败。' ;;
  esac
}

publish_telegram_topic_intro() {
  local result="" reason="telegram_topic_intro_failed"
  printf '%s\n' \
    '本操作会向“链上活动雷达”话题发送一条永久说明消息并置顶。' \
    '不会发送链上报告，也不会切换主 BOT 或 OAR 的 Real 模式。'
  confirm_phrase "发送并置顶链上话题说明" || return 1
  if result="$(run_onchain telegram-topic intro \
    --allow-network --send --confirm-real-send 2>/dev/null)"; then
    printf '链上活动雷达话题说明：已发送并置顶\n'
    return 0
  fi
  reason="$(
    printf '%s' "$result" | "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin)
    print(value.get("reason") or value.get("error") or "telegram_topic_intro_failed")
except Exception:
    print("telegram_topic_intro_failed")
' 2>/dev/null
  )"
  printf '%s：' "$reason"
  telegram_topic_bootstrap_error_cn "$reason"
  printf '\n'
  return 1
}

bootstrap_telegram_topic() {
  local result="" error_code="telegram_http_error" action=""
  printf '%s\n' \
    '将直接复用主 BOT 的 Token 和群，不需要再次输入。' \
    '程序会检查群和机器人权限；已有有效链上话题会复用，否则创建“链上活动雷达”。' \
    '本操作不会发送消息，也不会切换 Real 模式。'
  if result="$(run_onchain telegram-topic bootstrap --allow-network 2>/dev/null)"; then
    action="$(
      printf '%s' "$result" | "$PYTHON_BIN" -c '
import json, sys
try:
    print(json.load(sys.stdin).get("topic_action") or "reused")
except Exception:
    print("reused")
' 2>/dev/null
    )"
    if [ "$action" = "created" ]; then
      printf '链上活动雷达话题：已自动创建并配置\n'
    else
      printf '链上活动雷达话题：已识别并复用\n'
    fi
    return 0
  fi
  error_code="$(
    printf '%s' "$result" | "$PYTHON_BIN" -c '
import json, sys
try:
    print(json.load(sys.stdin).get("error") or "telegram_http_error")
except Exception:
    print("telegram_http_error")
' 2>/dev/null
  )"
  printf '%s：' "$error_code"
  telegram_topic_bootstrap_error_cn "$error_code"
  printf '\n'
  return 1
}

show_shared_telegram_status() {
  run_config status --json 2>/dev/null | "$PYTHON_BIN" -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    value = {}
print("Telegram Bot:", value.get("TG_BOT_TOKEN") or "not_configured")
print("Telegram 群:", value.get("TG_CHAT_ID") or "not_configured")
print(
    "链上活动雷达话题:",
    value.get("TG_ONCHAIN_FLOW_TOPIC_ID") or "not_configured",
)
print("Bot/群配置来源: 主 BOT 共享 .env.oi")
' 2>/dev/null || true
}

telegram_menu() {
  local choice
  while true; do
    menu_header
    cat <<'EOF'
Telegram 设置与测试
1. 查看共享 Telegram 脱敏状态
2. 自动识别群并创建/修复链上话题
3. 链上报告 Dry-run
4. 主 BOT readiness
5. 发送并置顶链上话题说明
0. 返回
EOF
    IFS= read -r choice
    case "$choice" in
      1) show_shared_telegram_status; pause_menu ;;
      2) bootstrap_telegram_topic; pause_menu ;;
      3) telegram_dry_run; pause_menu ;;
      4) run_main readiness; pause_menu ;;
      5) publish_telegram_topic_intro; pause_menu ;;
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
