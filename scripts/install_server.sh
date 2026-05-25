#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="${APP_NAME:-paopao-radar}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
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
  5. 可选输入 CoinGlass API key
  6. 创建 Python 虚拟环境并安装依赖
  7. 运行代码检查、单元测试和 readiness
  8. 安装并启动 systemd 服务

注意:
  - TG_BOT_TOKEN 只填 Telegram bot token，例如 123456:ABC...
  - TG_CHAT_ID 只填群 ID，例如 -1001234567890
  - CoinGlass key 只在 COINGLASS_API_KEY 那一步填写
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

is_valid_coinglass_key() {
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
    printf '格式不对：话题 ID 只能是数字。不要在这里填 bot token、群 ID 或 CoinGlass key。\n'
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
    clear_env_value TG_TEST_TOPIC_ID
    printf '已选择自动话题模式：不手动写话题 ID。\n'
    return 0
  fi

  prompt_topic_id TG_TOPIC_ID "默认兜底话题 TG_TOPIC_ID"
  prompt_topic_id TG_RADAR_SUMMARY_TOPIC_ID "资金摘要话题 TG_RADAR_SUMMARY_TOPIC_ID"
  prompt_topic_id TG_LAUNCH_ALERT_TOPIC_ID "启动预警话题 TG_LAUNCH_ALERT_TOPIC_ID"
  prompt_topic_id TG_ANNOUNCEMENT_ALERT_TOPIC_ID "公告风险话题 TG_ANNOUNCEMENT_ALERT_TOPIC_ID"
  prompt_topic_id TG_FLOW_RADAR_TOPIC_ID "资金流雷达话题 TG_FLOW_RADAR_TOPIC_ID"
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

prompt_coinglass_config_if_needed() {
  if [ ! -t 0 ]; then
    return 0
  fi
  local enabled existing_key coinglass_key
  enabled="$(get_env_value COINGLASS_ENABLE)"
  existing_key="$(get_env_value COINGLASS_API_KEY)"
  if [ "$enabled" = "true" ] && is_valid_coinglass_key "$existing_key"; then
    log "CoinGlass 已配置，跳过 key 输入"
    return 0
  fi

  cat <<EOF

CoinGlass 可选配置:
  - 直接回车: 使用纯 Binance 数据版本
  - 粘贴 COINGLASS_API_KEY: 启用 Binance + CoinGlass 双源版本

注意: CoinGlass key 只在这里填写，不要填到 Telegram 话题 ID。

EOF

  while true; do
    read -r -p "COINGLASS_API_KEY 可选，回车跳过: " coinglass_key
    if [ -z "$coinglass_key" ]; then
      set_env_value COINGLASS_ENABLE "false"
      set_env_value COINGLASS_API_KEY ""
      printf '已选择纯 Binance 数据版本。\n'
      return 0
    fi
    if is_valid_coinglass_key "$coinglass_key"; then
      set_env_value COINGLASS_ENABLE "true"
      set_env_value COINGLASS_API_KEY "$coinglass_key"
      set_env_value COINGLASS_BASE_URL "https://open-api-v4.coinglass.com"
      set_env_value COINGLASS_REQUEST_BUDGET "60"
      printf '已启用 Binance + CoinGlass 双源版本。\n'
      return 0
    fi
    printf 'COINGLASS_API_KEY 格式不对。回车可跳过，或重新粘贴有效 key。\n'
  done
}

install_os_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    log "安装 Linux 基础依赖"
    run_root apt-get update
    run_root apt-get install -y git python3 python3-venv python3-pip
  else
    log "未找到 apt-get，跳过系统依赖安装"
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
  if ! systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    return 0
  fi
  if yes_no_default_yes "配置已修改，是否立即重启 ${SERVICE_NAME} 服务"; then
    run_root systemctl restart "$SERVICE_NAME"
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    printf '未重启服务。稍后可手动执行: sudo systemctl restart %s\n' "$SERVICE_NAME"
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

prompt_coinglass_config_force() {
  local coinglass_key
  cat <<EOF

修改 CoinGlass API key:
  - 直接回车: 关闭 CoinGlass，使用纯 Binance 数据版本
  - 粘贴新 key: 启用 Binance + CoinGlass 双源版本

EOF
  while true; do
    read -r -p "新的 COINGLASS_API_KEY，回车关闭: " coinglass_key
    if [ -z "$coinglass_key" ]; then
      set_env_value COINGLASS_ENABLE "false"
      set_env_value COINGLASS_API_KEY ""
      printf 'CoinGlass 已关闭，后续使用纯 Binance 数据版本。\n'
      return 0
    fi
    if is_valid_coinglass_key "$coinglass_key"; then
      set_env_value COINGLASS_ENABLE "true"
      set_env_value COINGLASS_API_KEY "$coinglass_key"
      set_env_value COINGLASS_BASE_URL "https://open-api-v4.coinglass.com"
      set_env_value COINGLASS_REQUEST_BUDGET "60"
      printf 'COINGLASS_API_KEY 已更新。\n'
      return 0
    fi
    printf 'COINGLASS_API_KEY 格式不对。回车可关闭，或重新粘贴有效 key。\n'
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
  3. 修改 COINGLASS_API_KEY
  4. 修改 Telegram 话题配置
  5. Telegram 和 CoinGlass 全部重新填写
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
        prompt_coinglass_config_force
        changed=1
        ;;
      4)
        configure_topics
        changed=1
        ;;
      5)
        prompt_telegram_config
        prompt_coinglass_config_force
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
        printf '无效选项，请输入 0-6。\n'
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
  prompt_coinglass_config_if_needed
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

  if [ "$(get_env_value COINGLASS_ENABLE)" = "true" ]; then
    log "测试 CoinGlass API"
    if ! "${APP_DIR}/.venv/bin/python" main.py coinglass-test; then
      log "CoinGlass API 测试失败，自动切回纯 Binance 模式"
      set_env_value COINGLASS_ENABLE "false"
    fi
  else
    log "CoinGlass 未启用，使用纯 Binance 模式"
  fi
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

install_shortcut_command() {
  local shortcut="/usr/local/bin/paopao"
  log "安装快捷命令: paopao"
  run_root tee "$shortcut" >/dev/null <<EOF
#!/usr/bin/env bash
export PAOPAO_APP_DIR="${APP_DIR}"
export SERVICE_NAME="${SERVICE_NAME}"
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
  bash scripts/install_server.sh config   # 修改 token / 群 ID / CoinGlass key / 话题配置
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
  install_python_deps
  run_checks
  bootstrap_history_if_needed
  run_readiness
  install_systemd_service
  install_shortcut_command

  cat <<EOF

安装完成。

常用命令:
  paopao
  paopao config
  paopao logs
  paopao status
  paopao version
  paopao check-update
  paopao update
  sudo systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f
  cd ${APP_DIR} && . .venv/bin/activate && python main.py runtime-status
  cd ${APP_DIR} && . .venv/bin/activate && python main.py telegram-test --send --confirm-real-send

中文安装说明:
  ${APP_DIR}/docs/INSTALL_CN.md

EOF
}

main "$@"
