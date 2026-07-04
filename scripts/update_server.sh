#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
STRUCTURE_SERVICE_NAME="${STRUCTURE_SERVICE_NAME:-paopao-structure}"
CLEANUP_SERVICE_NAME="${CLEANUP_SERVICE_NAME:-paopao-cleanup}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-paopao-web}"
AI_SERVICE_NAME="${AI_SERVICE_NAME:-paopao-ai}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
AUTO_CONFIRM="${AUTO_CONFIRM:-0}"
CHECK_ONLY="${CHECK_ONLY:-0}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
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

usage() {
  cat <<EOF
用法:
  bash scripts/update_server.sh          # 检查 GitHub 最新版本，有更新时询问是否更新
  bash scripts/update_server.sh --yes    # 有更新时自动确认更新
  bash scripts/update_server.sh --check  # 只检查版本，不更新
  更新或确认已是最新版后，会自动执行 stable-check 输出稳定版自检摘要

环境变量:
  BRANCH=main
  REMOTE=origin
  SERVICE_NAME=paopao-radar
  STRUCTURE_SERVICE_NAME=paopao-structure
  CLEANUP_SERVICE_NAME=paopao-cleanup
  WEB_SERVICE_NAME=paopao-web
  AI_SERVICE_NAME=paopao-ai
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -y|--yes)
      AUTO_CONFIRM=1
      ;;
    --check|check)
      CHECK_ONLY=1
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n\n' "$1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

short_commit() {
  git rev-parse --short "$1"
}

commit_title() {
  git log -1 --format=%s "$1"
}

version_for_ref() {
  local ref="$1"
  local version
  version="$(git show "${ref}:VERSION" 2>/dev/null | head -n 1 | tr -d '\r' || true)"
  if [ -z "$version" ]; then
    version="unknown"
  fi
  printf '%s' "$version"
}

sync_env_file() {
  if [ -f "${APP_DIR}/scripts/sync_env.py" ]; then
    "$PYTHON_BIN" scripts/sync_env.py --env .env.oi --example .env.oi.example
  fi
}

get_env_value() {
  local key="$1"
  local line value
  line="$(grep -E "^${key}=" "${APP_DIR}/.env.oi" 2>/dev/null | tail -n 1 || true)"
  [[ "$line" == *=* ]] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

set_env_value() {
  local key="$1"
  local value="$2"
  "$PYTHON_BIN" - "${APP_DIR}/.env.oi" "$key" "$value" <<'PY'
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

generate_web_admin_token() {
  "$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
}

ensure_web_public_config() {
  local host port token
  host="$(get_env_value WEB_HOST)"
  port="$(get_env_value WEB_PORT)"
  token="$(get_env_value WEB_ADMIN_TOKEN)"

  if [ -z "$host" ] || [ "$host" = "127.0.0.1" ] || [ "$host" = "localhost" ]; then
    set_env_value WEB_HOST "0.0.0.0"
  fi
  if [ -z "$port" ] || [ "$port" = "80" ]; then
    set_env_value WEB_PORT "8080"
  fi
  if [ -z "$token" ]; then
    token="$(generate_web_admin_token)"
    set_env_value WEB_ADMIN_TOKEN "$token"
    chmod 600 "${APP_DIR}/.env.oi" || true
    printf '[paopao-update] 已生成 Web 控制台访问令牌。查看令牌: 输入 paopao 后选择 1\n'
  fi
}

run_post_update_cleanup() {
  if [ -f "${APP_DIR}/main.py" ]; then
    printf '\n[paopao-update] cleanup runtime artifacts\n'
    "$PYTHON_BIN" main.py cleanup --force-cleanup || true
  fi
}

run_post_update_stable_check() {
  if [ ! -f "${APP_DIR}/main.py" ]; then
    return 0
  fi
  printf '\n[paopao-update] stable check\n'
  set +e
  "$PYTHON_BIN" main.py stable-check
  local check_status=$?
  set -e
  case "$check_status" in
    0)
      printf '[paopao-update] 稳定版自检通过，长期运行就绪度请以上方摘要为准。\n'
      ;;
    1)
      printf '[paopao-update] 稳定版自检有警告，长期运行就绪度可能是准稳定候选。可打开 Web 控制台 -> 诊断报告查看详情。\n'
      ;;
    2)
      printf '[paopao-update] 稳定版自检未达标，长期运行就绪度需要处理。请打开 Web 控制台 -> 诊断报告按建议处理。\n'
      ;;
    *)
      printf '[paopao-update] 稳定版自检执行异常，退出码: %s\n' "$check_status"
      ;;
  esac
  return 0
}

install_or_update_structure_service() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local service_path="/etc/systemd/system/${STRUCTURE_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Structure Radar
After=network-online.target ${SERVICE_NAME}.service
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER:-$(id -un)}
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
  run_root systemctl enable "$STRUCTURE_SERVICE_NAME" >/dev/null 2>&1 || true
}

install_or_update_cleanup_timer() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local service_path="/etc/systemd/system/${CLEANUP_SERVICE_NAME}.service"
  local timer_path="/etc/systemd/system/${CLEANUP_SERVICE_NAME}.timer"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao runtime cleanup

[Service]
Type=oneshot
User=${SUDO_USER:-$(id -un)}
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
  run_root systemctl enable --now "${CLEANUP_SERVICE_NAME}.timer" >/dev/null 2>&1 || true
}

install_or_update_web_service() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local service_path="/etc/systemd/system/${WEB_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Web Console
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER:-$(id -un)}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py web
Restart=always
RestartSec=10
EnvironmentFile=-${APP_DIR}/.env.oi
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF
  run_root systemctl daemon-reload
  run_root systemctl enable "$WEB_SERVICE_NAME" >/dev/null 2>&1 || true
}

install_or_update_ai_service() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local service_path="/etc/systemd/system/${AI_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao AI Assistant Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER:-$(id -un)}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py ai-assistant
Restart=always
RestartSec=10
EnvironmentFile=-${APP_DIR}/.env.oi
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF
  run_root systemctl daemon-reload
  run_root systemctl enable "$AI_SERVICE_NAME" >/dev/null 2>&1 || true
}

install_shortcut_command() {
  if [ -f "${APP_DIR}/scripts/paopao_menu.sh" ]; then
    run_root tee /usr/local/bin/paopao >/dev/null <<EOF
#!/usr/bin/env bash
export PAOPAO_APP_DIR="${APP_DIR}"
export SERVICE_NAME="${SERVICE_NAME}"
export STRUCTURE_SERVICE_NAME="${STRUCTURE_SERVICE_NAME}"
export CLEANUP_SERVICE_NAME="${CLEANUP_SERVICE_NAME}"
export WEB_SERVICE_NAME="${WEB_SERVICE_NAME}"
export AI_SERVICE_NAME="${AI_SERVICE_NAME}"
exec bash "${APP_DIR}/scripts/paopao_menu.sh" "\$@"
EOF
    run_root chmod +x /usr/local/bin/paopao
    chmod +x "${APP_DIR}/scripts/paopao_menu.sh" || true
  fi
}

stop_legacy_structure_loops() {
  pkill -f "${APP_DIR}/main.py structure-loop" >/dev/null 2>&1 || true
  pkill -f "main.py structure-loop" >/dev/null 2>&1 || true
}

restart_services_if_present() {
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    run_root systemctl restart "$SERVICE_NAME"
    run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  else
    printf 'systemd service not found; update completed without main service restart.\n'
  fi

  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${STRUCTURE_SERVICE_NAME}.service" >/dev/null 2>&1; then
    stop_legacy_structure_loops
    run_root systemctl restart "$STRUCTURE_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$STRUCTURE_SERVICE_NAME" || true
  fi

  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${WEB_SERVICE_NAME}.service" >/dev/null 2>&1; then
    run_root systemctl restart "$WEB_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$WEB_SERVICE_NAME" || true
  fi

  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${AI_SERVICE_NAME}.service" >/dev/null 2>&1; then
    run_root systemctl restart "$AI_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$AI_SERVICE_NAME" || true
  fi
}

confirm_update() {
  if [ "$AUTO_CONFIRM" = "1" ]; then
    return 0
  fi
  if [ ! -t 0 ]; then
    printf '非交互式终端未带 --yes，已取消更新。\n'
    return 1
  fi
  local answer
  read -r -p "发现 GitHub 新版本，是否立即更新? [y/N]: " answer
  case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

cd "$APP_DIR"

printf '\n[paopao-update] 检查 GitHub 最新版本\n'
git fetch "$REMOTE" "$BRANCH"

LOCAL_REF="HEAD"
REMOTE_REF="FETCH_HEAD"
LOCAL_SHA="$(git rev-parse "$LOCAL_REF")"
REMOTE_SHA="$(git rev-parse "$REMOTE_REF")"
BASE_SHA="$(git merge-base "$LOCAL_REF" "$REMOTE_REF")"
LOCAL_VERSION="$(version_for_ref "$LOCAL_REF")"
REMOTE_VERSION="$(version_for_ref "$REMOTE_REF")"

printf '当前版本 : %s (%s)  %s\n' "$LOCAL_VERSION" "$(short_commit "$LOCAL_REF")" "$(commit_title "$LOCAL_REF")"
printf 'GitHub版本: %s (%s)  %s\n' "$REMOTE_VERSION" "$(short_commit "$REMOTE_REF")" "$(commit_title "$REMOTE_REF")"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
  if [ "$CHECK_ONLY" != "1" ]; then
    sync_env_file
    ensure_web_public_config
    run_post_update_cleanup
    install_shortcut_command
    install_or_update_structure_service
    install_or_update_cleanup_timer
    install_or_update_web_service
    install_or_update_ai_service
    restart_services_if_present
    run_post_update_stable_check
  fi
  printf '\n当前已经是最新版本，不需要更新。\n'
  exit 0
fi

if [ "$REMOTE_SHA" = "$BASE_SHA" ]; then
  printf '\n本地版本比 GitHub 更新，已取消自动更新，避免覆盖本地提交。\n'
  exit 1
fi

if [ "$LOCAL_SHA" != "$BASE_SHA" ]; then
  printf '\n本地版本和 GitHub 版本已分叉，不能安全 fast-forward。\n'
  printf '请先人工检查: git status && git log --oneline --graph --decorate --all -n 20\n'
  exit 1
fi

printf '\n发现可更新版本: %s (%s) -> %s (%s)\n' \
  "$LOCAL_VERSION" "$(short_commit "$LOCAL_REF")" \
  "$REMOTE_VERSION" "$(short_commit "$REMOTE_REF")"

if [ "$CHECK_ONLY" = "1" ]; then
  printf '当前是只检查模式，未执行更新。\n'
  exit 0
fi

if ! confirm_update; then
  printf '已取消更新。\n'
  exit 0
fi

git pull --ff-only "$REMOTE" "$BRANCH"

sync_env_file
ensure_web_public_config
"${APP_DIR}/.venv/bin/pip" install -r requirements.txt
"$PYTHON_BIN" -m compileall paopao_radar main.py
"$PYTHON_BIN" -m unittest discover -s tests -v
run_post_update_cleanup

install_shortcut_command
install_or_update_structure_service
install_or_update_cleanup_timer
install_or_update_web_service
install_or_update_ai_service
restart_services_if_present
run_post_update_stable_check

printf '\n[paopao-update] 更新完成: %s (%s)  %s\n' "$(version_for_ref HEAD)" "$(short_commit HEAD)" "$(commit_title HEAD)"
printf '[paopao-update] Web 控制台: http://服务器IP:8080/，访问令牌: 输入 paopao 后选择 1\n'
