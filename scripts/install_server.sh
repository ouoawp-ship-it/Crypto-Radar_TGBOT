#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="${APP_NAME:-paopao-radar}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
STRUCTURE_SERVICE_NAME="${STRUCTURE_SERVICE_NAME:-paopao-structure}"
CLEANUP_SERVICE_NAME="${CLEANUP_SERVICE_NAME:-paopao-cleanup}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-paopao-web}"
FRONTEND_SERVICE_NAME="${FRONTEND_SERVICE_NAME:-paopao-frontend}"
AI_SERVICE_NAME="${AI_SERVICE_NAME:-paopao-ai}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENV_FILE="${APP_DIR}/.env.oi"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
AUTO_START="${AUTO_START:-1}"
BOOTSTRAP_HISTORY="${BOOTSTRAP_HISTORY:-1}"
BOOTSTRAP_CYCLES="${BOOTSTRAP_CYCLES:-5}"
BOOTSTRAP_LAUNCH_SCAN_LIMIT="${BOOTSTRAP_LAUNCH_SCAN_LIMIT:-5}"
RUN_TELEGRAM_TEST="${RUN_TELEGRAM_TEST:-0}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

log() {
  printf '\n[%s] %s\n' "$APP_NAME" "$*"
}

die() {
  printf '\n[%s] 错误: %s\n' "$APP_NAME" "$*" >&2
  exit 1
}

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

print_banner() {
  cat <<EOF

============================================================
泡泡抓币 - 中文服务器安装向导
============================================================
安装目录: ${APP_DIR}
配置文件: ${ENV_FILE}

安装流程:
  1. 安装 Linux 基础依赖
  2. 创建/检查 .env.oi 配置文件
  3. 输入 Telegram bot token 和群 ID
  4. 选择是否启用 Telegram 话题自动分类
  5. 可选输入 Coinalyze API key
  6. 创建 Python 虚拟环境并安装依赖
  7. 运行代码检查、单元测试和 readiness
  8. 安装并启动 systemd 服务

注意:
  - TG_BOT_TOKEN 只填 Telegram bot token，例如 123456:ABC...
  - TG_CHAT_ID 只填群 ID，例如 -1001234567890
  - Coinalyze key 只在 COINALYZE_API_KEY 那一步填写
  - 话题 ID 默认不需要填写，机器人有权限时会自动创建并记录
============================================================

EOF
}

get_env_value() {
  local key="$1"
  local line value
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  [[ "$line" == *=* ]] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

is_placeholder_value() {
  local value lower
  value="${1:-}"
  lower="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
  [[ -z "$value" \
    || "$lower" == *your* \
    || "$lower" == *token* \
    || "$lower" == *chat_id* \
    || "$lower" == *bot_token* \
    || "$lower" == *example* \
    || "$lower" == *xxx* \
    || "$value" == *填写* \
    || "$value" == *填入* \
    || "$value" == *请输入* ]]
}

is_valid_bot_token() {
  local value="${1:-}"
  if is_placeholder_value "$value"; then
    return 1
  fi
  [[ "$value" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{25,}$ ]]
}

is_valid_chat_id() {
  local value="${1:-}"
  if is_placeholder_value "$value"; then
    return 1
  fi
  [[ "$value" =~ ^-?[0-9]{5,20}$ || "$value" =~ ^@[A-Za-z0-9_]{5,32}$ ]]
}

is_valid_topic_id() {
  local value="${1:-}"
  [[ "$value" =~ ^[0-9]{1,20}$ ]]
}

is_valid_coinalyze_key() {
  local value="${1:-}"
  if is_placeholder_value "$value"; then
    return 1
  fi
  [[ "$value" =~ ^[A-Za-z0-9_-]{16,128}$ ]]
}

set_env_value() {
  local key="$1"
  local value="$2"
  "$PYTHON_BIN" - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
line = f"{key}={value}"
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
for idx, existing in enumerate(lines):
    if existing.startswith(f"{key}="):
        lines[idx] = line
        break
else:
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

ensure_web_public_config() {
  local host port password_hash session_secret
  host="$(get_env_value WEB_HOST)"
  port="$(get_env_value WEB_PORT)"
  password_hash="$(get_env_value WEB_ADMIN_PASSWORD_HASH)"
  session_secret="$(get_env_value WEB_SESSION_SECRET)"

  if [ -z "$host" ] || [ "$host" = "127.0.0.1" ] || [ "$host" = "localhost" ]; then
    set_env_value WEB_HOST "0.0.0.0"
  fi
  if [ -z "$port" ] || [ "$port" = "80" ]; then
    set_env_value WEB_PORT "8080"
  fi
  set_env_value WEB_AUTH_MODE "password"
  if [ -z "$password_hash" ] || [ -z "$session_secret" ]; then
    log "后台账号密码未完整配置。安装完成后执行: .venv/bin/python main.py admin-password set"
  fi
}

clear_env_value() {
  set_env_value "$1" ""
}

yes_no_default_no() {
  local answer
  read -r -p "$1 [y/N]: " answer
  case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

yes_no_default_yes() {
  local answer
  read -r -p "$1 [Y/n]: " answer
  case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
    n|no) return 1 ;;
    *) return 0 ;;
  esac
}

prompt_topic_id() {
  local key="$1"
  local label="$2"
  local value
  while true; do
    read -r -p "${label}，只填数字 message_thread_id，回车跳过: " value
    if [ -z "$value" ]; then
      clear_env_value "$key"
      return 0
    fi
    if is_valid_topic_id "$value"; then
      set_env_value "$key" "$value"
      return 0
    fi
    printf '格式不对：话题 ID 只能是数字。不要在这里填 bot token、群 ID 或 API key。\n'
  done
}

sanitize_topic_config() {
  local key value
  for key in \
    TG_TOPIC_ID \
    TG_RADAR_SUMMARY_TOPIC_ID \
    TG_LAUNCH_ALERT_TOPIC_ID \
    TG_ANNOUNCEMENT_ALERT_TOPIC_ID \
    TG_FLOW_RADAR_TOPIC_ID \
    STRUCTURE_TOPIC_ID \
    STRUCTURE_REVIEW_TOPIC_ID \
    TG_TEST_TOPIC_ID
  do
    value="$(get_env_value "$key")"
    if [ -n "$value" ] && ! is_valid_topic_id "$value"; then
      log "检测到 ${key} 不是数字话题 ID，已自动清空，避免误用其他 key"
      clear_env_value "$key"
    fi
  done
}

configure_topics() {
  set_env_value TELEGRAM_USE_TOPIC "true"
  set_env_value TG_AUTO_CREATE_TOPICS "true"
  set_env_value TG_TOPIC_INTRO_ENABLE "true"
  set_env_value TG_TOPIC_INTRO_PIN "true"
  sanitize_topic_config

  if [ ! -t 0 ]; then
    log "非交互式终端：保留现有话题配置，并启用自动话题模式"
    return 0
  fi

  local key has_manual_topic
  has_manual_topic=0
  for key in \
    TG_TOPIC_ID \
    TG_RADAR_SUMMARY_TOPIC_ID \
    TG_LAUNCH_ALERT_TOPIC_ID \
    TG_ANNOUNCEMENT_ALERT_TOPIC_ID \
    TG_FLOW_RADAR_TOPIC_ID \
    STRUCTURE_TOPIC_ID \
    STRUCTURE_REVIEW_TOPIC_ID \
    TG_TEST_TOPIC_ID
  do
    if is_valid_topic_id "$(get_env_value "$key")"; then
      has_manual_topic=1
      break
    fi
  done

  cat <<EOF

Telegram 话题分类:
  默认启用自动话题模式。
  如果 bot 是群管理员，并且有管理话题/置顶消息权限，项目会自动创建:
    - 资金摘要
    - 启动预警
    - 公告风险
    - 资金流雷达
    - 测试消息

通常这里不需要手动填写任何话题 ID。
只有你已经知道 Telegram 的 message_thread_id，才选择手动填写。

EOF

  if [ "$has_manual_topic" = "1" ]; then
    printf '检测到已有手动话题 ID，默认保留。\n'
    if ! yes_no_default_no "是否重新填写 Telegram 话题 ID"; then
      return 0
    fi
  elif ! yes_no_default_no "是否手动填写 Telegram 话题 ID"; then
    clear_env_value TG_TOPIC_ID
    clear_env_value TG_RADAR_SUMMARY_TOPIC_ID
    clear_env_value TG_LAUNCH_ALERT_TOPIC_ID
    clear_env_value TG_ANNOUNCEMENT_ALERT_TOPIC_ID
  clear_env_value TG_FLOW_RADAR_TOPIC_ID
  clear_env_value STRUCTURE_TOPIC_ID
  clear_env_value STRUCTURE_REVIEW_TOPIC_ID
  clear_env_value TG_TEST_TOPIC_ID
    printf '已选择自动话题模式：不手动写话题 ID。\n'
    return 0
  fi

  prompt_topic_id TG_TOPIC_ID "默认兜底话题 TG_TOPIC_ID"
  prompt_topic_id TG_RADAR_SUMMARY_TOPIC_ID "资金摘要话题 TG_RADAR_SUMMARY_TOPIC_ID"
  prompt_topic_id TG_LAUNCH_ALERT_TOPIC_ID "启动预警话题 TG_LAUNCH_ALERT_TOPIC_ID"
  prompt_topic_id TG_ANNOUNCEMENT_ALERT_TOPIC_ID "公告风险话题 TG_ANNOUNCEMENT_ALERT_TOPIC_ID"
  prompt_topic_id TG_FLOW_RADAR_TOPIC_ID "资金流雷达话题 TG_FLOW_RADAR_TOPIC_ID"
  prompt_topic_id STRUCTURE_TOPIC_ID "结构突破话题 STRUCTURE_TOPIC_ID"
  prompt_topic_id STRUCTURE_REVIEW_TOPIC_ID "结构复盘话题 STRUCTURE_REVIEW_TOPIC_ID"
  prompt_topic_id TG_TEST_TOPIC_ID "测试消息话题 TG_TEST_TOPIC_ID"
}

prompt_telegram_config() {
  if [ ! -t 0 ]; then
    cat <<EOF

.env.oi 缺少 TG_BOT_TOKEN 或 TG_CHAT_ID，但当前不是交互式终端。
请手动编辑配置后重新运行:

  nano ${ENV_FILE}
  bash ${APP_DIR}/scripts/install_server.sh

EOF
    exit 0
  fi

  local bot_token chat_id
  cat <<EOF

Telegram 必填配置:
  TG_BOT_TOKEN = BotFather 给你的机器人 token
  TG_CHAT_ID   = 机器人要推送到的群 ID，通常是 -100... 或 @channel_username

提示: token 输入会显示出来，方便确认粘贴成功。

EOF

  while true; do
    read -r -p "TG_BOT_TOKEN 粘贴到这里: " bot_token
    if is_valid_bot_token "$bot_token"; then
      break
    fi
    printf 'TG_BOT_TOKEN 格式不对，必须类似 123456:ABC...。按 Ctrl+C 可退出安装。\n'
  done

  while true; do
    read -r -p "TG_CHAT_ID 填群 ID: " chat_id
    if is_valid_chat_id "$chat_id"; then
      break
    fi
    printf 'TG_CHAT_ID 格式不对，通常是 -1001234567890 或 @channel_username。按 Ctrl+C 可退出安装。\n'
  done

  set_env_value TG_BOT_TOKEN "$bot_token"
  set_env_value TG_CHAT_ID "$chat_id"
  configure_topics
  chmod 600 "$ENV_FILE" || true
}

prompt_coinalyze_config_if_needed() {
  if [ ! -t 0 ]; then
    return 0
  fi
  local enabled existing_key coinalyze_key
  enabled="$(get_env_value COINALYZE_ENABLE)"
  existing_key="$(get_env_value COINALYZE_API_KEY)"
  if [ "$enabled" = "true" ] && is_valid_coinalyze_key "$existing_key"; then
    log "Coinalyze 已配置，跳过 key 输入"
    return 0
  fi

  cat <<EOF

Coinalyze 可选配置:
  - 直接回车: 不启用 Coinalyze 历史清算辅助
  - 粘贴 COINALYZE_API_KEY: 启用 Coinalyze 免费清算历史辅助

说明: 这是结构雷达可选清算历史方向辅助，不等同于预测清算池。

EOF

  while true; do
    read -r -p "COINALYZE_API_KEY 可选，回车跳过: " coinalyze_key
    if [ -z "$coinalyze_key" ]; then
      set_env_value COINALYZE_ENABLE "false"
      set_env_value COINALYZE_API_KEY ""
      printf '已跳过 Coinalyze。后续仍会使用 Binance 免费盘口降级。\n'
      return 0
    fi
    if is_valid_coinalyze_key "$coinalyze_key"; then
      set_env_value COINALYZE_ENABLE "true"
      set_env_value COINALYZE_API_KEY "$coinalyze_key"
      set_env_value LIQUIDITY_FALLBACK_ENABLE "true"
      set_env_value BINANCE_ORDERBOOK_LIQUIDITY_ENABLE "true"
      printf 'COINALYZE_API_KEY 已保存，已启用免费清算历史辅助。\n'
      return 0
    fi
    printf 'COINALYZE_API_KEY 格式不对。回车可跳过，或重新粘贴有效 key。\n'
  done
}

install_os_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    log "安装 Linux 基础依赖"
    run_root apt-get update
    run_root apt-get install -y git python3 python3-venv python3-pip curl ca-certificates gnupg
  else
    log "未找到 apt-get，跳过系统依赖安装"
  fi
}

node_major_version() {
  command -v node >/dev/null 2>&1 || return 1
  node -p "Number(process.versions.node.split('.')[0])" 2>/dev/null
}

npm_bin_path() {
  command -v npm 2>/dev/null || true
}

ensure_node_runtime() {
  local major=""
  major="$(node_major_version || true)"
  if [ -n "$major" ] && [ "$major" -ge 20 ]; then
    log "Node.js 已满足 Next.js 前台构建要求: $(node --version)"
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    die "未检测到 Node.js 20+。请先安装 Node.js LTS 后再构建 frontend。"
  fi

  log "安装 Node.js 22 LTS，用于构建 Next.js 公开前台"
  local key_tmp="/tmp/nodesource.gpg.$$"
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key -o "$key_tmp"
  run_root install -d -m 0755 /etc/apt/keyrings
  run_root gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg "$key_tmp"
  rm -f "$key_tmp"
  printf 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main\n' \
    | run_root tee /etc/apt/sources.list.d/nodesource.list >/dev/null
  run_root apt-get update
  run_root apt-get install -y nodejs
}

frontend_install_deps() {
  if [ -f package-lock.json ]; then
    npm ci
  else
    npm install
  fi
}

ensure_env_file_exists() {
  if [ ! -f "$ENV_FILE" ]; then
    log "创建 .env.oi 配置文件"
    cp "${APP_DIR}/.env.oi.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE" || true
  fi
}

clear_topic_routes_file() {
  rm -f "${APP_DIR}/data/tg_topic_routes.json"
  log "已清理旧话题路由 data/tg_topic_routes.json，后续会按新群重新自动创建"
}

restart_service_if_requested() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local has_main has_structure
  has_main=0
  has_structure=0
  if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    has_main=1
  fi
  if systemctl list-unit-files "${STRUCTURE_SERVICE_NAME}.service" >/dev/null 2>&1; then
    has_structure=1
  fi
  if [ "$has_main" = "0" ] && [ "$has_structure" = "0" ]; then
    return 0
  fi
  if yes_no_default_yes "配置已修改，是否立即重启已安装的泡泡抓币服务"; then
    if [ "$has_main" = "1" ]; then
      run_root systemctl restart "$SERVICE_NAME"
      run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
    fi
    if [ "$has_structure" = "1" ]; then
      run_root systemctl restart "$STRUCTURE_SERVICE_NAME"
      run_root systemctl --no-pager --full status "$STRUCTURE_SERVICE_NAME" || true
    fi
  else
    printf '未重启服务。稍后可手动执行: sudo systemctl restart %s %s\n' "$SERVICE_NAME" "$STRUCTURE_SERVICE_NAME"
  fi
}

prompt_bot_token_only() {
  local bot_token
  while true; do
    read -r -p "新的 TG_BOT_TOKEN: " bot_token
    if is_valid_bot_token "$bot_token"; then
      set_env_value TG_BOT_TOKEN "$bot_token"
      chmod 600 "$ENV_FILE" || true
      printf 'TG_BOT_TOKEN 已更新。\n'
      return 0
    fi
    printf 'TG_BOT_TOKEN 格式不对，必须类似 123456:ABC...。按 Ctrl+C 可退出。\n'
  done
}

prompt_chat_id_only() {
  local old_chat_id chat_id
  old_chat_id="$(get_env_value TG_CHAT_ID)"
  while true; do
    read -r -p "新的 TG_CHAT_ID: " chat_id
    if is_valid_chat_id "$chat_id"; then
      set_env_value TG_CHAT_ID "$chat_id"
      chmod 600 "$ENV_FILE" || true
      printf 'TG_CHAT_ID 已更新。\n'
      if [ "$old_chat_id" != "$chat_id" ]; then
        clear_topic_routes_file
      fi
      return 0
    fi
    printf 'TG_CHAT_ID 格式不对，通常是 -1001234567890 或 @channel_username。按 Ctrl+C 可退出。\n'
  done
}

prompt_coinalyze_config_force() {
  local coinalyze_key
  cat <<EOF

修改 Coinalyze API key:
  - 直接回车: 关闭 Coinalyze 清算历史辅助
  - 粘贴新 key: 启用 Coinalyze 免费清算历史辅助

EOF
  while true; do
    read -r -p "新的 COINALYZE_API_KEY，回车关闭: " coinalyze_key
    if [ -z "$coinalyze_key" ]; then
      set_env_value COINALYZE_ENABLE "false"
      set_env_value COINALYZE_API_KEY ""
      printf 'Coinalyze 已关闭。\n'
      return 0
    fi
    if is_valid_coinalyze_key "$coinalyze_key"; then
      set_env_value COINALYZE_ENABLE "true"
      set_env_value COINALYZE_API_KEY "$coinalyze_key"
      set_env_value LIQUIDITY_FALLBACK_ENABLE "true"
      set_env_value BINANCE_ORDERBOOK_LIQUIDITY_ENABLE "true"
      printf 'COINALYZE_API_KEY 已更新。\n'
      return 0
    fi
    printf 'COINALYZE_API_KEY 格式不对。回车可关闭，或重新粘贴有效 key。\n'
  done
}

print_config_menu() {
  cat <<EOF

============================================================
泡泡抓币 - 配置修改向导
============================================================
当前配置文件: ${ENV_FILE}

  1. 修改 TG_BOT_TOKEN
  2. 修改 TG_CHAT_ID / 群 ID
  3. 修改 COINALYZE_API_KEY
  4. 修改 Telegram 话题配置
  5. Telegram / Coinalyze 全部重新填写
  6. 清理旧 Telegram 话题路由
  0. 保存并退出

说明:
  - 修改群 ID 后会自动清理旧话题路由。
  - 清理旧话题路由后，机器人会在新群重新自动创建话题。
  - 修改 token / 群 ID / key 后建议重启服务。
============================================================

EOF
}

run_config_wizard() {
  cd "$APP_DIR"
  if [ ! -t 0 ]; then
    die "配置修改向导需要交互式终端"
  fi
  ensure_env_file_exists

  local choice changed
  changed=0
  while true; do
    print_config_menu
    read -r -p "请选择: " choice
    case "$choice" in
      1)
        prompt_bot_token_only
        changed=1
        ;;
      2)
        prompt_chat_id_only
        changed=1
        ;;
      3)
        prompt_coinalyze_config_force
        changed=1
        ;;
      4)
        configure_topics
        changed=1
        ;;
      5)
        prompt_telegram_config
        prompt_coinalyze_config_force
        changed=1
        ;;
      6)
        clear_topic_routes_file
        changed=1
        ;;
      0)
        break
        ;;
      *)
        printf '无效选项，请输入 0-7。\n'
        ;;
    esac
  done

  if [ "$changed" = "1" ]; then
    restart_service_if_requested
  else
    printf '配置未修改。\n'
  fi
}

ensure_env_file() {
  ensure_env_file_exists

  local existing_token existing_chat
  existing_token="$(get_env_value TG_BOT_TOKEN)"
  existing_chat="$(get_env_value TG_CHAT_ID)"
  if ! is_valid_bot_token "$existing_token" || ! is_valid_chat_id "$existing_chat"; then
    prompt_telegram_config
  else
    configure_topics
  fi
  prompt_coinalyze_config_if_needed
}

install_python_deps() {
  log "创建 Python 虚拟环境"
  "$PYTHON_BIN" -m venv "${APP_DIR}/.venv"

  log "安装 Python 依赖"
  "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
  "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
}

run_checks() {
  log "运行 Python 编译检查"
  cd "$APP_DIR"
  "${APP_DIR}/.venv/bin/python" -m compileall paopao_radar main.py

  log "运行单元测试"
  "${APP_DIR}/.venv/bin/python" -m unittest discover -s tests -v
}

build_frontend_dashboard() {
  if [ ! -f "${APP_DIR}/frontend/package.json" ]; then
    log "未发现 frontend/package.json，跳过 Next.js 公开前台构建"
    return 0
  fi

  ensure_node_runtime
  log "安装并构建 Next.js 公开前台"
  (
    cd "${APP_DIR}/frontend"
    frontend_install_deps
    npm run build
  )
}

bootstrap_history_if_needed() {
  if [ "$BOOTSTRAP_HISTORY" != "1" ]; then
    return 0
  fi

  cd "$APP_DIR"
  if "${APP_DIR}/.venv/bin/python" main.py readiness >/tmp/paopao-readiness.log 2>&1; then
    log "readiness 已通过，无需预热观察历史"
    return 0
  fi

  log "生成 dry-run 启动观察历史 (${BOOTSTRAP_CYCLES} 轮，每轮扫描 ${BOOTSTRAP_LAUNCH_SCAN_LIMIT} 个)"
  local i
  for i in $(seq 1 "$BOOTSTRAP_CYCLES"); do
    "${APP_DIR}/.venv/bin/python" main.py observe \
      --duration-minutes 0 \
      --launch-interval 60 \
      --launch-scan-limit "$BOOTSTRAP_LAUNCH_SCAN_LIMIT" \
      --records 20 \
      --top 5
  done
}

run_readiness() {
  log "运行真实推送 readiness 检查"
  cd "$APP_DIR"
  "${APP_DIR}/.venv/bin/python" main.py readiness

  if [ "$RUN_TELEGRAM_TEST" = "1" ]; then
    log "发送一条 Telegram 测试消息"
    "${APP_DIR}/.venv/bin/python" main.py telegram-test --send --confirm-real-send
  else
    log "跳过安装阶段 Telegram 测试消息；如需发送，使用 RUN_TELEGRAM_TEST=1"
  fi

  log "结构外部确认使用 Binance 免费盘口，可选 Coinalyze 历史清算辅助"
}

install_systemd_service() {
  command -v systemctl >/dev/null 2>&1 || {
    log "未找到 systemctl，不安装 systemd 服务"
    return 0
  }

  log "安装 systemd 服务: ${SERVICE_NAME}"
  local service_path="/etc/systemd/system/${SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Crypto Radar
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py live --send --confirm-real-send
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable "$SERVICE_NAME"

  if [ "$AUTO_START" = "1" ]; then
    log "启动服务"
    run_root systemctl restart "$SERVICE_NAME"
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    log "AUTO_START=0，服务已安装但未启动"
  fi
}

install_structure_systemd_service() {
  command -v systemctl >/dev/null 2>&1 || {
    log "未找到 systemctl，不安装结构雷达 systemd 服务"
    return 0
  }

  log "安装 systemd 服务: ${STRUCTURE_SERVICE_NAME}"
  local service_path="/etc/systemd/system/${STRUCTURE_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Structure Radar
After=network-online.target ${SERVICE_NAME}.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py structure-loop --send --confirm-real-send
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable "$STRUCTURE_SERVICE_NAME"

  if [ "$AUTO_START" = "1" ]; then
    log "启动结构雷达服务"
    run_root systemctl restart "$STRUCTURE_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$STRUCTURE_SERVICE_NAME" || true
  else
    log "AUTO_START=0，结构雷达服务已安装但未启动"
  fi
}

install_cleanup_systemd_timer() {
  command -v systemctl >/dev/null 2>&1 || {
    log "未找到 systemctl，不安装自动清理 timer"
    return 0
  }

  log "安装 systemd 自动清理: ${CLEANUP_SERVICE_NAME}.timer"
  local service_path="/etc/systemd/system/${CLEANUP_SERVICE_NAME}.service"
  local timer_path="/etc/systemd/system/${CLEANUP_SERVICE_NAME}.timer"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao runtime cleanup

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py cleanup --force-cleanup
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
EOF

  run_root tee "$timer_path" >/dev/null <<EOF
[Unit]
Description=Run Paopao cleanup hourly

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
Persistent=true
Unit=${CLEANUP_SERVICE_NAME}.service

[Install]
WantedBy=timers.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable --now "${CLEANUP_SERVICE_NAME}.timer"
}

install_web_systemd_service() {
  command -v systemctl >/dev/null 2>&1 || {
    log "未找到 systemctl，不安装 Web 控制台 systemd 服务"
    return 0
  }

  log "安装 systemd 服务: ${WEB_SERVICE_NAME}"
  local service_path="/etc/systemd/system/${WEB_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Web Console
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py web
Restart=always
RestartSec=10
EnvironmentFile=-${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable "$WEB_SERVICE_NAME"

  if [ "$AUTO_START" = "1" ]; then
    log "启动 Web 控制台服务"
    run_root systemctl restart "$WEB_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$WEB_SERVICE_NAME" || true
  else
    log "AUTO_START=0，Web 控制台服务已安装但未启动"
  fi
}

install_frontend_systemd_service() {
  command -v systemctl >/dev/null 2>&1 || {
    log "未找到 systemctl，不安装 Next.js 公开前台 systemd 服务"
    return 0
  }
  if [ ! -f "${APP_DIR}/frontend/package.json" ]; then
    log "未发现 frontend/package.json，不安装 Next.js 公开前台 systemd 服务"
    return 0
  fi
  ensure_node_runtime
  local npm_bin
  npm_bin="$(npm_bin_path)"
  if [ -z "$npm_bin" ]; then
    die "未检测到 npm，无法安装 Next.js 公开前台 systemd 服务"
  fi

  log "安装 systemd 服务: ${FRONTEND_SERVICE_NAME}"
  local service_path="/etc/systemd/system/${FRONTEND_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Next.js Public Frontend
After=network.target ${WEB_SERVICE_NAME}.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}/frontend
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=HOSTNAME=127.0.0.1
ExecStart=${npm_bin} run start -- --hostname 127.0.0.1 --port 3000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  run_root systemctl daemon-reload

  if [ "$AUTO_START" = "1" ]; then
    log "启动 Next.js 公开前台服务"
    run_root systemctl enable --now "$FRONTEND_SERVICE_NAME"
    run_root systemctl restart "$FRONTEND_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$FRONTEND_SERVICE_NAME" || true
  else
    run_root systemctl enable "$FRONTEND_SERVICE_NAME"
    log "AUTO_START=0，Next.js 公开前台服务已安装但未启动"
  fi
}

install_or_update_nginx_frontend_routes() {
  command -v nginx >/dev/null 2>&1 || {
    log "未找到 nginx，跳过公开前台反代配置"
    return 0
  }
  local domain="${PUBLIC_DOMAIN:-paoxx.com}"
  local site_path="${NGINX_SITE_PATH:-/etc/nginx/sites-available/${domain}}"
  local enabled_path="/etc/nginx/sites-enabled/${domain}"
  local fullchain="/etc/letsencrypt/live/${domain}/fullchain.pem"
  local privkey="/etc/letsencrypt/live/${domain}/privkey.pem"
  if [ ! -f "$fullchain" ] || [ ! -f "$privkey" ]; then
    log "未找到 ${domain} 证书文件，跳过 Nginx 反代配置写入"
    return 0
  fi

  log "写入 Nginx 公开前台反代配置: ${site_path}"
  run_root tee "$site_path" >/dev/null <<EOF
server {
    listen 80;
    server_name ${domain};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${domain};

    ssl_certificate ${fullchain};
    ssl_certificate_key ${privkey};

    location ^~ /admin {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /public-api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
EOF
  run_root ln -sfn "$site_path" "$enabled_path"
  run_root nginx -t
  if command -v systemctl >/dev/null 2>&1; then
    run_root systemctl reload nginx
  else
    run_root nginx -s reload
  fi
}

install_ai_systemd_service() {
  command -v systemctl >/dev/null 2>&1 || {
    log "未找到 systemctl，不安装 AI 助手 systemd 服务"
    return 0
  }

  log "安装 systemd 服务: ${AI_SERVICE_NAME}"
  local service_path="/etc/systemd/system/${AI_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao AI Assistant Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py ai-assistant
Restart=always
RestartSec=10
EnvironmentFile=-${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable "$AI_SERVICE_NAME"

  if [ "$AUTO_START" = "1" ]; then
    log "启动 AI 助手服务"
    run_root systemctl restart "$AI_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$AI_SERVICE_NAME" || true
  else
    log "AUTO_START=0，AI 助手服务已安装但未启动"
  fi
}

install_shortcut_command() {
  local shortcut="/usr/local/bin/paopao"
  log "安装快捷命令: paopao"
  run_root tee "$shortcut" >/dev/null <<EOF
#!/usr/bin/env bash
export PAOPAO_APP_DIR="${APP_DIR}"
export SERVICE_NAME="${SERVICE_NAME}"
export STRUCTURE_SERVICE_NAME="${STRUCTURE_SERVICE_NAME}"
export CLEANUP_SERVICE_NAME="${CLEANUP_SERVICE_NAME}"
export WEB_SERVICE_NAME="${WEB_SERVICE_NAME}"
export FRONTEND_SERVICE_NAME="${FRONTEND_SERVICE_NAME}"
export AI_SERVICE_NAME="${AI_SERVICE_NAME}"
exec bash "${APP_DIR}/scripts/paopao_menu.sh" "\$@"
EOF
  run_root chmod +x "$shortcut"
  chmod +x "${APP_DIR}/scripts/paopao_menu.sh" || true
}

main() {
  cd "$APP_DIR"
  case "${1:-install}" in
    install|"")
      ;;
    config|configure|--config)
      run_config_wizard
      return 0
      ;;
    shortcut|--shortcut)
      install_shortcut_command
      return 0
      ;;
    help|-h|--help)
      cat <<EOF
用法:
  bash scripts/install_server.sh          # 中文安装向导
  bash scripts/install_server.sh config   # 修改 token / 群 ID / Coinalyze key / 话题配置
  bash scripts/install_server.sh shortcut # 只安装 paopao 快捷命令
EOF
      return 0
      ;;
    *)
      die "未知参数: $1。可用: install, config, --help"
      ;;
  esac

  print_banner
  install_os_packages
  ensure_env_file
  ensure_web_public_config
  install_python_deps
  run_checks
  build_frontend_dashboard
  bootstrap_history_if_needed
  run_readiness
  install_systemd_service
  install_structure_systemd_service
  install_cleanup_systemd_timer
  install_web_systemd_service
  install_frontend_systemd_service
  install_or_update_nginx_frontend_routes
  install_ai_systemd_service
  install_shortcut_command

  cat <<EOF

安装完成。

入口命令:
  paopao

正式访问入口:
  公开前台: https://paoxx.com/（Next.js Dashboard）
  后台控制台: https://paoxx.com/admin
  后台登录: 使用自定义账号 + 密码；输入 paopao 后选择“设置后台账号密码”

说明:
  服务器只需要记住 paopao 这一个入口命令。
  进入中文菜单后，用数字查看正式入口、设置后台账号密码、Web 服务状态、实时日志、重启 Web 服务、检查更新、更新项目和查看版本。
  paopao-frontend 监听 127.0.0.1:3000，只供 Nginx 反代公开前台。
  8080 仅作为 Nginx 反代后端入口，不作为公网访问入口。
  配置修改、主服务/结构雷达启停、测试消息、readiness、doctor、cleanup、结构复盘等控制功能在 Web 控制台完成。

中文安装说明:
  ${APP_DIR}/docs/INSTALL_CN.md

EOF
}

main "$@"
