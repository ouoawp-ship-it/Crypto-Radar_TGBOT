#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
AUTO_CONFIRM="${AUTO_CONFIRM:-0}"
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

write_service() {
  local name="$1"
  local description="$2"
  local command="$3"
  local service_user="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
  run_root tee "/etc/systemd/system/${name}.service" >/dev/null <<EOF
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${service_user}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/.env.oi
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py ${command}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
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
  write_service "$SERVICE_NAME" "Paopao Telegram Signal Radar" "live --send --confirm-real-send"
  write_service "$MARKET_STREAM_SERVICE_NAME" "Paopao Realtime Market Stream" "market-stream"
  run_root systemctl daemon-reload
  run_root systemctl enable "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME"
  run_root systemctl restart "$MARKET_STREAM_SERVICE_NAME" "$SERVICE_NAME"
}

validate_runtime() {
  cd "$APP_DIR"
  .venv/bin/python scripts/sync_env.py --env .env.oi --example .env.oi.example
  .venv/bin/pip install -r requirements.lock
  .venv/bin/python -m compileall -q paopao_radar tests scripts main.py
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
}

run_stable_check() {
  set +e
  .venv/bin/python main.py stable-check --no-save
  local code=$?
  set -e
  if [ "$code" -eq 2 ]; then
    printf 'BOT 稳定性检查存在阻断项，更新终止。\n' >&2
    exit 2
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
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
validate_runtime
run_stable_check
retire_legacy_services
install_runtime_services
printf 'BOT-only 更新完成: %s\n' "$(git rev-parse --short HEAD)"
