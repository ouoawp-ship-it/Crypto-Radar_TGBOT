#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
HEALTH_SERVICE_NAME="${HEALTH_SERVICE_NAME:-paopao-health}"
BACKUP_SERVICE_NAME="${BACKUP_SERVICE_NAME:-paopao-backup}"
PRIVATE_CONTROL_SERVICE_NAME="${PRIVATE_CONTROL_SERVICE_NAME:-paopao-private-control}"
RADAR_MEMORY_HIGH="${RADAR_MEMORY_HIGH:-450M}"
RADAR_MEMORY_MAX="${RADAR_MEMORY_MAX:-650M}"
MARKET_STREAM_MEMORY_HIGH="${MARKET_STREAM_MEMORY_HIGH:-128M}"
MARKET_STREAM_MEMORY_MAX="${MARKET_STREAM_MEMORY_MAX:-256M}"
AUTO_CONFIRM="${AUTO_CONFIRM:-0}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
CHECK_ONLY=0

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

usage() {
  cat <<'EOF'
用法: bash scripts/update_server.sh [--check] [--yes]
  --check  只检查 GitHub 是否有更新
  --yes    无交互执行安全 fast-forward 更新
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check|check) CHECK_ONLY=1 ;;
    -y|--yes) AUTO_CONFIRM=1 ;;
    -h|--help|help) usage; exit 0 ;;
    *) printf '未知参数: %s\n' "$1" >&2; usage; exit 2 ;;
  esac
  shift
done

confirm_update() {
  [ "$AUTO_CONFIRM" = "1" ] && return 0
  [ -t 0 ] || return 1
  local answer
  read -r -p "发现新版本，是否更新? [y/N]: " answer
  [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]]
}

retire_legacy_services() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local legacy
  for legacy in paopao-frontend paopao-web paopao-ai; do
    run_root systemctl disable --now "$legacy" >/dev/null 2>&1 || true
    run_root rm -f "/etc/systemd/system/${legacy}.service"
  done
  # Remove only the route previously owned by this project; leave unrelated Nginx config untouched.
  run_root rm -f /etc/nginx/conf.d/00-paoxx-frontend.conf
  if command -v nginx >/dev/null 2>&1 && run_root nginx -t >/dev/null 2>&1; then
    run_root systemctl reload nginx >/dev/null 2>&1 || true
  fi
}

install_runtime_services() {
  command -v systemctl >/dev/null 2>&1 || return 0
  local service_user="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
  local private_control_was_active=0
  if systemctl is-active --quiet "$PRIVATE_CONTROL_SERVICE_NAME" 2>/dev/null; then
    private_control_was_active=1
  fi
  PAOPAO_APP_DIR="$APP_DIR" \
    MAIN_BOT_SERVICE_NAME="$SERVICE_NAME" \
    SERVICE_USER="$service_user" \
    RADAR_MEMORY_HIGH="$RADAR_MEMORY_HIGH" \
    RADAR_MEMORY_MAX="$RADAR_MEMORY_MAX" \
    ALLOW_ENV_MIGRATION_TRANSITION=1 \
    START_MAIN_BOT=0 \
    bash "${APP_DIR}/scripts/install_main_bot_service.sh"
  PAOPAO_APP_DIR="$APP_DIR" \
    SERVICE_USER="$service_user" \
    START_PRIVATE_CONTROL=0 \
    ENABLE_PRIVATE_CONTROL=0 \
    bash "${APP_DIR}/scripts/install_private_control_service.sh"
  PAOPAO_APP_DIR="$APP_DIR" \
    MARKET_STREAM_SERVICE_NAME="$MARKET_STREAM_SERVICE_NAME" \
    SERVICE_USER="$service_user" \
    MARKET_STREAM_MEMORY_HIGH="$MARKET_STREAM_MEMORY_HIGH" \
    MARKET_STREAM_MEMORY_MAX="$MARKET_STREAM_MEMORY_MAX" \
    START_MARKET_STREAM=0 \
    bash "${APP_DIR}/scripts/install_market_stream_service.sh"
  run_root tee "/etc/systemd/system/${HEALTH_SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Paopao Runtime Health Check
After=${SERVICE_NAME}.service ${MARKET_STREAM_SERVICE_NAME}.service

[Service]
Type=oneshot
User=${service_user}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/config/.env.oi
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/bin/bash ${APP_DIR}/scripts/systemd_health_check.sh ${APP_DIR}
Nice=10
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
EOF
  run_root tee "/etc/systemd/system/${HEALTH_SERVICE_NAME}.timer" >/dev/null <<EOF
[Unit]
Description=Check Paopao Runtime Health Every Five Minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  run_root tee "/etc/systemd/system/${BACKUP_SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Paopao SQLite Database Backup
After=${SERVICE_NAME}.service ${MARKET_STREAM_SERVICE_NAME}.service

[Service]
Type=oneshot
User=${service_user}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/config/.env.oi
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py database-backup
Nice=10
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
EOF
  run_root tee "/etc/systemd/system/${BACKUP_SERVICE_NAME}.timer" >/dev/null <<EOF
[Unit]
Description=Back Up Paopao SQLite Databases Daily

[Timer]
OnBootSec=15min
OnCalendar=*-*-* 03:30:00 UTC
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  run_root systemctl daemon-reload
  run_root systemctl enable "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" "${HEALTH_SERVICE_NAME}.timer" "${BACKUP_SERVICE_NAME}.timer"
  run_root systemctl restart "$MARKET_STREAM_SERVICE_NAME" "$SERVICE_NAME" "${HEALTH_SERVICE_NAME}.timer" "${BACKUP_SERVICE_NAME}.timer"
  if [ "$private_control_was_active" = "1" ]; then
    run_root systemctl restart "$PRIVATE_CONTROL_SERVICE_NAME"
  fi
}

validate_runtime() {
  cd "$APP_DIR"
  .venv/bin/python scripts/sync_env.py --env config/.env.oi --example config/.env.oi.example
  .venv/bin/pip install -r requirements.lock
  .venv/bin/python -m compileall -q radars shared runtime config tests scripts main.py
  .venv/bin/python -m unittest discover -s tests -t . -p 'test_*.py'
}

run_stable_check() {
  set +e
  .venv/bin/python main.py stable-check --no-save
  local code=$?
  set -e
  if [ "$code" -ge 2 ]; then
    printf 'bot_stable_check_failed\n' >&2
    exit 2
  fi
  if [ "$code" -eq 1 ]; then
    printf 'bot_stable_check_attention_non_blocking\n' >&2
  fi
}

cd "$APP_DIR"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  printf '检测到未提交的已跟踪文件修改，拒绝自动更新。\n' >&2
  exit 1
fi

git fetch "$REMOTE" "$BRANCH"
LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse FETCH_HEAD)"
BASE_SHA="$(git merge-base HEAD FETCH_HEAD)"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
  printf '当前已经是最新版本。\n'
  [ "$CHECK_ONLY" = "1" ] && exit 0
elif [ "$LOCAL_SHA" != "$BASE_SHA" ]; then
  printf '本地与远程已分叉，拒绝自动更新。\n' >&2
  exit 1
elif [ "$CHECK_ONLY" = "1" ]; then
  printf '发现可更新版本: %s -> %s\n' "${LOCAL_SHA:0:7}" "${REMOTE_SHA:0:7}"
  exit 0
else
  confirm_update || { printf '已取消更新。\n'; exit 0; }
  git pull --ff-only "$REMOTE" "$BRANCH"
  if [ "${PAOPAO_UPDATE_REEXEC:-0}" != "1" ]; then
    export PAOPAO_UPDATE_REEXEC=1
    exec bash "${APP_DIR}/scripts/update_server.sh" --yes
  fi
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python scripts/migrate_config_layout.py \
  --base-dir "$APP_DIR" --owner-user "$SERVICE_USER"
validate_runtime
run_stable_check
retire_legacy_services
install_runtime_services
.venv/bin/python scripts/migrate_config_layout.py \
  --base-dir "$APP_DIR" --finalize --owner-user "$SERVICE_USER"
printf 'BOT-only 更新完成: %s\n' "$(git rev-parse --short HEAD)"
