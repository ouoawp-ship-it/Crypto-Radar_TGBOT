#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
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

环境变量:
  BRANCH=main
  REMOTE=origin
  SERVICE_NAME=paopao-radar
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

run_post_update_cleanup() {
  if [ -f "${APP_DIR}/main.py" ]; then
    printf '\n[paopao-update] cleanup runtime artifacts\n'
    "$PYTHON_BIN" main.py cleanup --force-cleanup || true
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
    run_post_update_cleanup
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
"${APP_DIR}/.venv/bin/pip" install -r requirements.txt
"$PYTHON_BIN" -m compileall paopao_radar main.py
"$PYTHON_BIN" -m unittest discover -s tests -v
run_post_update_cleanup

if [ -f "${APP_DIR}/scripts/paopao_menu.sh" ]; then
  run_root tee /usr/local/bin/paopao >/dev/null <<EOF
#!/usr/bin/env bash
export PAOPAO_APP_DIR="${APP_DIR}"
export SERVICE_NAME="${SERVICE_NAME}"
exec bash "${APP_DIR}/scripts/paopao_menu.sh" "\$@"
EOF
  run_root chmod +x /usr/local/bin/paopao
  chmod +x "${APP_DIR}/scripts/paopao_menu.sh" || true
fi

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
  run_root systemctl restart "$SERVICE_NAME"
  run_root systemctl --no-pager --full status "$SERVICE_NAME" || true
else
  printf 'systemd service not found; update completed without restart.\n'
fi

printf '\n[paopao-update] 更新完成: %s (%s)  %s\n' "$(version_for_ref HEAD)" "$(short_commit HEAD)" "$(commit_title HEAD)"
