#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy or roll back an immutable, annotated release tag.  This entry point
# deliberately stays separate from update_server.sh, whose contract remains a
# fast-forward update of the main branch.

APP_DIR="${PAOPAO_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REMOTE="${REMOTE:-origin}"
BACKUP_ROOT="${PAOPAO_RELEASE_BACKUP_DIR:-${APP_DIR}/backups/releases}"
SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
MARKET_STREAM_SERVICE_NAME="${MARKET_STREAM_SERVICE_NAME:-paopao-market-stream}"
PRIVATE_CONTROL_SERVICE_NAME="${PRIVATE_CONTROL_SERVICE_NAME:-paopao-private-control}"
HEALTH_SERVICE_NAME="${HEALTH_SERVICE_NAME:-paopao-health}"
BACKUP_SERVICE_NAME="${BACKUP_SERVICE_NAME:-paopao-backup}"
CLEANUP_SERVICE_NAME="${CLEANUP_SERVICE_NAME:-paopao-cleanup}"
READINESS_TIMEOUT_SEC="${PAOPAO_RELEASE_READINESS_TIMEOUT_SEC:-1200}"
READINESS_INTERVAL_SEC="${PAOPAO_RELEASE_READINESS_INTERVAL_SEC:-15}"
READINESS_REQUIRED_SUCCESSES="${PAOPAO_RELEASE_READINESS_SUCCESSES:-2}"
AUTO_CONFIRM=0
MODE=""
TARGET_TAG=""
ROLLBACK_SET=""
ROLLBACK_READY=0
SESSION_DIR=""
CREATED_BACKUP=""
EXIT_HANDLER_ACTIVE=0
REFRESH_PULSE_TOPIC_INTRO=0

umask 077

run_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法：
  bash scripts/deploy_tag.sh --check-tag vX.Y.Z
  bash scripts/deploy_tag.sh --tag vX.Y.Z [--yes] [--refresh-pulse-topic-intro]
  bash scripts/deploy_tag.sh --rollback /absolute/backup/set [--yes]

--check-tag  只验证远端正式 Tag，不切换代码、不停止服务
--tag        备份当前生产状态后部署指定正式 Tag
--rollback   从指定发布备份恢复代码、配置、状态、数据库和 systemd 定义
--yes        跳过交互确认
--refresh-pulse-topic-intro  部署后按版本刷新并置顶现有脉冲雷达说明
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check-tag)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -z "$MODE" ] || { usage; exit 2; }
      MODE="check"
      TARGET_TAG="$2"
      shift 2
      ;;
    --tag)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -z "$MODE" ] || { usage; exit 2; }
      MODE="deploy"
      TARGET_TAG="$2"
      shift 2
      ;;
    --rollback)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      [ -z "$MODE" ] || { usage; exit 2; }
      MODE="rollback"
      ROLLBACK_SET="$2"
      shift 2
      ;;
    -y|--yes)
      AUTO_CONFIRM=1
      shift
      ;;
    --refresh-pulse-topic-intro)
      REFRESH_PULSE_TOPIC_INTRO=1
      shift
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n' "$1" >&2
      usage
      exit 2
      ;;
  esac
done

[ -n "$MODE" ] || { usage; exit 2; }
if [ "$REFRESH_PULSE_TOPIC_INTRO" = "1" ] && [ "$MODE" != "deploy" ]; then
  fail "pulse_topic_intro_refresh_requires_deploy_mode"
fi

validate_unit_name() {
  [[ "$1" =~ ^[A-Za-z0-9_.@-]+$ ]] || fail "systemd_unit_name_invalid"
}

for unit_name in \
  "$SERVICE_NAME" "$MARKET_STREAM_SERVICE_NAME" \
  "$PRIVATE_CONTROL_SERVICE_NAME" "$HEALTH_SERVICE_NAME" \
  "$BACKUP_SERVICE_NAME" "$CLEANUP_SERVICE_NAME"; do
  validate_unit_name "$unit_name"
done

SERVICE_UNITS=(
  "${SERVICE_NAME}.service"
  "${MARKET_STREAM_SERVICE_NAME}.service"
  "${PRIVATE_CONTROL_SERVICE_NAME}.service"
  "${HEALTH_SERVICE_NAME}.service"
  "${HEALTH_SERVICE_NAME}.timer"
  "${BACKUP_SERVICE_NAME}.service"
  "${BACKUP_SERVICE_NAME}.timer"
  "${CLEANUP_SERVICE_NAME}.service"
  "${CLEANUP_SERVICE_NAME}.timer"
)

require_clean_checkout() {
  cd "$APP_DIR"
  [ -z "$(git status --porcelain)" ] || \
    fail "tracked_worktree_not_clean"
}

require_safe_host_paths() {
  [ -d "$APP_DIR/.git" ] || git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1 || \
    fail "git_checkout_missing"
  [ -f "$APP_DIR/config/.env.oi" ] || fail "runtime_config_missing"
  [ ! -L "$APP_DIR/config/.env.oi" ] || fail "runtime_config_symlink_rejected"
  [ "$(stat -c '%a' "$APP_DIR/config/.env.oi")" = "600" ] || \
    fail "runtime_config_permissions_must_be_600"
  [ -d "$APP_DIR/data" ] || fail "runtime_data_directory_missing"
  [ ! -L "$APP_DIR/data" ] || fail "runtime_data_symlink_rejected"
  [ -x "$APP_DIR/.venv/bin/python" ] || fail "project_venv_missing"
  [ -f "$APP_DIR/scripts/release_runtime_data.py" ] || \
    fail "release_runtime_data_helper_missing"
  [ ! -L "$APP_DIR/scripts/release_runtime_data.py" ] || \
    fail "release_runtime_data_helper_symlink_rejected"
  if [ -e "$BACKUP_ROOT" ]; then
    [ -d "$BACKUP_ROOT" ] || fail "release_backup_path_invalid"
    [ ! -L "$BACKUP_ROOT" ] || fail "release_backup_symlink_rejected"
  fi
  command -v systemctl >/dev/null 2>&1 || fail "systemd_required"
  command -v python3 >/dev/null 2>&1 || fail "python3_required"
  command -v sha256sum >/dev/null 2>&1 || fail "sha256sum_required"
  command -v flock >/dev/null 2>&1 || fail "flock_required"
}

validate_readiness_policy() {
  [[ "$READINESS_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || \
    fail "release_readiness_timeout_invalid"
  [[ "$READINESS_INTERVAL_SEC" =~ ^[1-9][0-9]*$ ]] || \
    fail "release_readiness_interval_invalid"
  [[ "$READINESS_REQUIRED_SUCCESSES" =~ ^[1-9][0-9]*$ ]] || \
    fail "release_readiness_success_count_invalid"
  [ "$READINESS_TIMEOUT_SEC" -ge "$READINESS_INTERVAL_SEC" ] || \
    fail "release_readiness_window_too_short"
}

acquire_deploy_lock() {
  mkdir -p "$BACKUP_ROOT"
  chmod 0700 "$BACKUP_ROOT"
  exec 9>"$BACKUP_ROOT/.deploy.lock"
  flock -n 9 || fail "release_deployment_already_running"
}

fetch_and_verify_tag() {
  local tag="$1"
  [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "release_tag_format_invalid"
  cd "$APP_DIR"
  local remote_main
  git fetch --no-tags "$REMOTE" main
  remote_main="$(git rev-parse FETCH_HEAD)"
  git fetch --no-tags "$REMOTE" "refs/tags/${tag}:refs/tags/${tag}"
  [ "$(git cat-file -t "refs/tags/${tag}")" = "tag" ] || \
    fail "annotated_release_tag_required"
  local commit version
  commit="$(git rev-parse --verify "refs/tags/${tag}^{commit}")"
  git merge-base --is-ancestor "$commit" "$remote_main" || \
    fail "release_tag_not_on_remote_main"
  version="$(git show "${commit}:VERSION" | tr -d '\r\n')"
  [ "$version" = "$tag" ] || fail "release_tag_version_mismatch"
  git show --check --format= "$commit"
  printf '%s\n' "$commit"
}

confirm_action() {
  local prompt="$1"
  [ "$AUTO_CONFIRM" = "1" ] && return 0
  [ -t 0 ] || return 1
  local answer
  read -r -p "$prompt [y/N]: " answer
  [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]]
}

record_and_stop_units() {
  local state_file="$1" unit active enabled running
  local -a running_units=()
  : >"$state_file"
  for unit in "${SERVICE_UNITS[@]}"; do
    active=0
    enabled=0
    running=0
    systemctl is-active --quiet "$unit" 2>/dev/null && running=1
    active="$running"
    case "$unit" in
      "${HEALTH_SERVICE_NAME}.service"|"${BACKUP_SERVICE_NAME}.service"|"${CLEANUP_SERVICE_NAME}.service")
        # One-shot units are restored through their timers, not replayed as an
        # in-progress job after a deployment or rollback.
        active=0
        ;;
    esac
    systemctl is-enabled --quiet "$unit" 2>/dev/null && enabled=1
    printf '%s\t%s\t%s\n' "$unit" "$active" "$enabled" >>"$state_file"
    if [ "$running" = "1" ]; then
      running_units+=("$unit")
    fi
  done
  for unit in "${running_units[@]}"; do
    run_root systemctl stop "$unit"
  done
}

restore_unit_activity() {
  local state_file="$1" unit active enabled failed=0
  [ -f "$state_file" ] || return 0
  while IFS=$'\t' read -r unit active enabled; do
    if [ "$enabled" = "1" ]; then
      run_root systemctl enable "$unit" >/dev/null || failed=1
    else
      run_root systemctl disable "$unit" >/dev/null 2>&1 || failed=1
    fi
    if [ "$active" = "1" ]; then
      run_root systemctl restart "$unit" || failed=1
    else
      run_root systemctl stop "$unit" >/dev/null 2>&1 || failed=1
    fi
  done <"$state_file"
  return "$failed"
}

verify_unit_activity() {
  local state_file="$1" unit active enabled
  while IFS=$'\t' read -r unit active enabled; do
    if [ "$enabled" = "1" ]; then
      run_root systemctl is-enabled --quiet "$unit" || \
        fail "unit_enabled_state_mismatch"
    elif run_root systemctl is-enabled --quiet "$unit"; then
      fail "unit_enabled_state_mismatch"
    fi
    if [ "$active" = "1" ]; then
      run_root systemctl is-active --quiet "$unit"
    elif run_root systemctl is-active --quiet "$unit"; then
      fail "rollback_unit_activity_mismatch"
    fi
  done <"$state_file"
}

full_runtime_was_active() {
  local state_file="$1" unit active _enabled main_active=0 market_active=0
  while IFS=$'\t' read -r unit active _enabled; do
    case "$unit" in
      "${SERVICE_NAME}.service") main_active="$active" ;;
      "${MARKET_STREAM_SERVICE_NAME}.service") market_active="$active" ;;
    esac
  done <"$state_file"
  [ "$main_active" = "1" ] && [ "$market_active" = "1" ]
}

backup_runtime() {
  local target_tag="$1" previous_commit stamp backup_dir unit unit_path counter
  previous_commit="$(git -C "$APP_DIR" rev-parse HEAD)"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_dir="${BACKUP_ROOT}/${stamp}-${previous_commit:0:12}"
  counter=1
  while [ -e "$backup_dir" ]; do
    backup_dir="${BACKUP_ROOT}/${stamp}-${previous_commit:0:12}-${counter}"
    counter=$((counter + 1))
  done
  mkdir -p "$BACKUP_ROOT"
  chmod 0700 "$BACKUP_ROOT"
  mkdir -m 0700 "$backup_dir" "$backup_dir/config" "$backup_dir/data" "$backup_dir/systemd"

  install -m 0600 "$APP_DIR/config/.env.oi" "$backup_dir/config/.env.oi"
  "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" database-backup \
    >"$backup_dir/database-backup-report.json"
  python3 "$APP_DIR/scripts/release_runtime_data.py" backup \
    --source "$APP_DIR/data" \
    --destination "$backup_dir/data" \
    --exclude-root "$BACKUP_ROOT" \
    >"$backup_dir/runtime-data-report.json"
  install -m 0600 "$APP_DIR/scripts/release_runtime_data.py" \
    "$backup_dir/release-runtime-data.py"

  for unit in "${SERVICE_UNITS[@]}"; do
    unit_path="/etc/systemd/system/${unit}"
    if run_root test -f "$unit_path"; then
      run_root cat "$unit_path" >"$backup_dir/systemd/${unit}"
      chmod 0600 "$backup_dir/systemd/${unit}"
      printf '%s\tpresent\n' "$unit" >>"$backup_dir/systemd-inventory.tsv"
    else
      printf '%s\tmissing\n' "$unit" >>"$backup_dir/systemd-inventory.tsv"
    fi
  done
  cp "$SESSION_DIR/unit-state.tsv" "$backup_dir/unit-state.tsv"
  cat >"$backup_dir/release-manifest.env" <<EOF
schema_version=1
previous_commit=${previous_commit}
target_tag=${target_tag}
created_at=${stamp}
EOF
  (
    cd "$backup_dir"
    find config data systemd -type f -print0 | sort -z | xargs -0 sha256sum
    sha256sum database-backup-report.json runtime-data-report.json \
      release-runtime-data.py systemd-inventory.tsv unit-state.tsv \
      release-manifest.env
  ) >"$backup_dir/SHA256SUMS"
  chmod 0600 "$backup_dir/SHA256SUMS" "$backup_dir"/*.tsv "$backup_dir"/*.env
  CREATED_BACKUP="$backup_dir"
  ROLLBACK_READY=1
  printf 'release_backup=%s\n' "$backup_dir"
}

manifest_value() {
  local backup_dir="$1" key="$2" value
  value="$(sed -n "s/^${key}=//p" "$backup_dir/release-manifest.env")"
  [ -n "$value" ] || fail "release_backup_manifest_invalid"
  printf '%s\n' "$value"
}

verify_backup_set() {
  local requested="$1" root resolved
  root="$(realpath -m "$BACKUP_ROOT")"
  resolved="$(realpath -e "$requested")"
  [[ "$resolved" == "$root"/* ]] || fail "rollback_backup_outside_release_root"
  [ -d "$resolved" ] && [ ! -L "$resolved" ] || fail "rollback_backup_invalid"
  [ -f "$resolved/release-manifest.env" ] || fail "rollback_manifest_missing"
  [ -f "$resolved/SHA256SUMS" ] || fail "rollback_checksums_missing"
  (
    cd "$resolved"
    sha256sum --check --quiet SHA256SUMS
  ) || fail "rollback_checksum_failed"
  printf '%s\n' "$resolved"
}

restore_backup_set() {
  local backup_dir previous_commit unit status
  backup_dir="$(verify_backup_set "$1")"
  previous_commit="$(manifest_value "$backup_dir" previous_commit)"
  [[ "$previous_commit" =~ ^[0-9a-f]{40}$ ]] || fail "rollback_commit_invalid"
  git -C "$APP_DIR" cat-file -e "${previous_commit}^{commit}" || \
    fail "rollback_commit_unavailable"
  git -C "$APP_DIR" checkout --detach "$previous_commit"

  install -m 0600 "$backup_dir/config/.env.oi" "$APP_DIR/config/.env.oi"
  python3 "$backup_dir/release-runtime-data.py" restore \
    --source "$backup_dir/data" \
    --destination "$APP_DIR/data"
  while IFS=$'\t' read -r unit status; do
    if [ "$status" = "present" ]; then
      run_root install -m 0644 "$backup_dir/systemd/$unit" "/etc/systemd/system/$unit"
    elif [ "$status" = "missing" ]; then
      run_root rm -f "/etc/systemd/system/$unit"
    else
      fail "rollback_systemd_inventory_invalid"
    fi
  done <"$backup_dir/systemd-inventory.tsv"

  if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/.venv"
  fi
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.lock"
  "$APP_DIR/.venv/bin/python" -m compileall -q \
    "$APP_DIR/radars" "$APP_DIR/shared" "$APP_DIR/runtime" \
    "$APP_DIR/config" "$APP_DIR/tests" "$APP_DIR/scripts" "$APP_DIR/main.py"
  if [ -f "$APP_DIR/scripts/validate_runtime_config.py" ]; then
    "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/validate_runtime_config.py"
  else
    "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" stable-check --no-save
  fi
  run_root systemctl daemon-reload
  restore_unit_activity "$backup_dir/unit-state.tsv"
  verify_unit_activity "$backup_dir/unit-state.tsv"
  printf 'rollback_commit=%s\n' "$previous_commit"
}

validate_no_conflicting_stream() {
  local manual_count market_count
  manual_count="$(pgrep -fc '[m]ain.py altcoin-anomaly.*--realtime-duration-sec' || true)"
  market_count="$(pgrep -fc '[m]ain.py market-stream' || true)"
  [ "$manual_count" -eq 0 ] || fail "bounded_p2_process_conflict"
  [ "$market_count" -le 1 ] || fail "multiple_market_stream_processes_detected"
}

validate_deployed_runtime_once() {
  local manual_count market_count
  run_root systemctl is-active --quiet "$SERVICE_NAME" || return 1
  run_root systemctl is-active --quiet "$MARKET_STREAM_SERVICE_NAME" || return 1
  manual_count="$(pgrep -fc '[m]ain.py altcoin-anomaly.*--realtime-duration-sec' || true)"
  market_count="$(pgrep -fc '[m]ain.py market-stream' || true)"
  if [ "$manual_count" -ne 0 ] || [ "$market_count" -gt 1 ]; then
    printf 'runtime_process_conflict manual=%s market_stream=%s\n' \
      "$manual_count" "$market_count" >&2
    return 2
  fi
  "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" readiness || return 1
  "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" stable-check --no-save || \
    return 1
}

wait_for_deployed_runtime() {
  local started deadline now attempts=0 consecutive=0 code=0
  local log_file="${SESSION_DIR:-$APP_DIR}/release-readiness-last.log"
  started="$(date +%s)"
  deadline=$((started + READINESS_TIMEOUT_SEC))
  while true; do
    attempts=$((attempts + 1))
    code=0
    validate_deployed_runtime_once >"$log_file" 2>&1 || code=$?
    if [ "$code" -eq 0 ]; then
      consecutive=$((consecutive + 1))
      if [ "$consecutive" -ge "$READINESS_REQUIRED_SUCCESSES" ]; then
        printf 'release_readiness_passed=true attempts=%s consecutive=%s\n' \
          "$attempts" "$consecutive"
        rm -f -- "$log_file"
        return 0
      fi
    elif [ "$code" -eq 2 ]; then
      cat "$log_file" >&2
      return 1
    else
      consecutive=0
    fi
    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
      printf 'release_readiness_timeout attempts=%s timeout_sec=%s\n' \
        "$attempts" "$READINESS_TIMEOUT_SEC" >&2
      cat "$log_file" >&2
      return 1
    fi
    sleep "$READINESS_INTERVAL_SEC"
  done
}

refresh_pulse_topic_intro() {
  [ "$REFRESH_PULSE_TOPIC_INTRO" = "1" ] || return 0
  "$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" telegram-topic-refresh \
    --topic-template TG_LAUNCH_ALERT \
    --send --confirm-real-send
  printf 'release_pulse_topic_intro_refresh=complete\n'
}

deploy_tag() {
  local tag="$1" commit="$2"
  SESSION_DIR="$(mktemp -d)"
  record_and_stop_units "$SESSION_DIR/unit-state.tsv"
  backup_runtime "$tag"
  git -C "$APP_DIR" checkout --detach "$commit"
  [ "$(tr -d '\r\n' <"$APP_DIR/VERSION")" = "$tag" ] || \
    fail "checked_out_version_mismatch"
  PYTHON_BIN=python3 AUTO_START=0 INSTALL_PP_SHORTCUT=0 \
    bash "$APP_DIR/scripts/install_server.sh"
  restore_unit_activity "$CREATED_BACKUP/unit-state.tsv"
  verify_unit_activity "$CREATED_BACKUP/unit-state.tsv"
  if full_runtime_was_active "$CREATED_BACKUP/unit-state.tsv"; then
    wait_for_deployed_runtime
  else
    printf 'release_readiness_skipped=services_previously_inactive\n'
  fi
  ROLLBACK_READY=0
  refresh_pulse_topic_intro
  printf 'deployed_tag=%s\n' "$tag"
  printf 'deployed_commit=%s\n' "$commit"
}

cleanup_session() {
  if [ -n "$SESSION_DIR" ] && [ -d "$SESSION_DIR" ]; then
    rm -f -- "$SESSION_DIR/unit-state.tsv" \
      "$SESSION_DIR/release-readiness-last.log"
    rmdir -- "$SESSION_DIR" 2>/dev/null || true
  fi
}

handle_exit() {
  local code=$? rollback_code=0
  [ "$EXIT_HANDLER_ACTIVE" = "0" ] || return
  EXIT_HANDLER_ACTIVE=1
  trap - EXIT ERR
  trap '' INT TERM HUP
  set +e
  if [ "$code" -ne 0 ]; then
    if [ "$ROLLBACK_READY" = "1" ] && [ -n "$CREATED_BACKUP" ]; then
      printf 'release_deploy_failed; rollback_started=true\n' >&2
      ROLLBACK_READY=0
      (
        set -Eeuo pipefail
        restore_backup_set "$CREATED_BACKUP"
      )
      rollback_code=$?
      if [ "$rollback_code" -ne 0 ]; then
        printf 'release_rollback_failed=true backup=%s\n' \
          "$CREATED_BACKUP" >&2
        code=1
      else
        printf 'release_rollback_completed=true backup=%s\n' \
          "$CREATED_BACKUP" >&2
      fi
    elif [ -n "$SESSION_DIR" ] && [ -f "$SESSION_DIR/unit-state.tsv" ]; then
      printf 'release_deploy_failed; unit_restore_started=true\n' >&2
      restore_unit_activity "$SESSION_DIR/unit-state.tsv"
      rollback_code=$?
      if [ "$rollback_code" -ne 0 ]; then
        printf 'release_unit_restore_failed=true\n' >&2
        code=1
      fi
    fi
  fi
  cleanup_session
  exit "$code"
}

trap handle_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

require_clean_checkout
if [ "$MODE" = "check" ]; then
  commit="$(fetch_and_verify_tag "$TARGET_TAG")"
  printf 'release_tag_valid=true\nrelease_tag=%s\nrelease_commit=%s\n' \
    "$TARGET_TAG" "$commit"
  exit 0
fi

require_safe_host_paths
validate_readiness_policy
acquire_deploy_lock
validate_no_conflicting_stream

if [ "$MODE" = "deploy" ]; then
  commit="$(fetch_and_verify_tag "$TARGET_TAG")"
  confirm_action "确认部署正式 Tag ${TARGET_TAG}" || {
    printf 'deployment_cancelled\n'
    exit 0
  }
  deploy_tag "$TARGET_TAG" "$commit"
  exit 0
fi

ROLLBACK_SET="$(verify_backup_set "$ROLLBACK_SET")"
confirm_action "确认从 ${ROLLBACK_SET} 回滚" || {
  printf 'rollback_cancelled\n'
  exit 0
}
SESSION_DIR="$(mktemp -d)"
record_and_stop_units "$SESSION_DIR/unit-state.tsv"
TARGET_ROLLBACK_SET="$ROLLBACK_SET"
backup_runtime "rollback-safety"
restore_backup_set "$TARGET_ROLLBACK_SET"
wait_for_deployed_runtime
ROLLBACK_READY=0
printf 'rollback_completed=true\nbackup=%s\n' "$TARGET_ROLLBACK_SET"
