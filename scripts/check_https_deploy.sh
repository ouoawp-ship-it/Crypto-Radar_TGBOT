#!/usr/bin/env bash
set -uo pipefail

BASE_URL="https://paoxx.com"
ROOT_PATH="/"
ADMIN_PATH="/admin"
PUBLIC_API_PATH="/public-api/signals?limit=1"
PRIVATE_API_PATH="/api/dashboard"
TIMEOUT=10
CONNECT_TIMEOUT=5
WITH_STABLE_CHECK=0
WITH_CERTBOT_DRY_RUN=0
SKIP_SERVICES=0
SKIP_LOGS=0
APP_DIR="${APP_DIR:-/home/ubuntu/paopao-crypto-radar}"

PASS_COUNT=0
WARN_COUNT=0
BLOCK_COUNT=0
RESULTS=()

usage() {
  cat <<'EOF'
泡泡雷达 HTTPS 部署验收

用法:
  scripts/check_https_deploy.sh
  scripts/check_https_deploy.sh --base-url https://paoxx.com
  scripts/check_https_deploy.sh --with-stable-check
  scripts/check_https_deploy.sh --with-certbot-dry-run
  scripts/check_https_deploy.sh --skip-services
  scripts/check_https_deploy.sh --skip-logs
  scripts/check_https_deploy.sh --timeout 10

说明:
  默认不执行 certbot renew --dry-run。
  页面检查使用 GET，不使用 HEAD；paopao-web 对 HEAD 可能返回 501。
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --with-stable-check)
      WITH_STABLE_CHECK=1
      shift
      ;;
    --with-certbot-dry-run)
      WITH_CERTBOT_DRY_RUN=1
      shift
      ;;
    --skip-services)
      SKIP_SERVICES=1
      shift
      ;;
    --skip-logs)
      SKIP_LOGS=1
      shift
      ;;
    --timeout)
      TIMEOUT="${2:-10}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ ! -d "${APP_DIR}" ]; then
  APP_DIR="${REPO_DIR}"
fi

DOMAIN="${BASE_URL#*://}"
DOMAIN="${DOMAIN%%/*}"
DOMAIN="${DOMAIN%%:*}"

record_pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  RESULTS+=("[通过] $1")
}

record_warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  RESULTS+=("[警告] $1")
}

record_block() {
  BLOCK_COUNT=$((BLOCK_COUNT + 1))
  RESULTS+=("[阻断] $1")
}

curl_get_body() {
  local url="$1"
  curl -sS --connect-timeout "${CONNECT_TIMEOUT}" --max-time "${TIMEOUT}" "${url}"
}

check_page_contains() {
  local label="$1"
  local url="$2"
  local needle="$3"
  local body
  if ! body="$(curl_get_body "${url}" 2>/dev/null)"; then
    record_block "${label} 请求失败: ${url}"
    return
  fi
  if printf '%s' "${body}" | grep -Fq "${needle}"; then
    record_pass "${label} 返回 ${needle}"
  else
    record_block "${label} 未找到预期内容: ${needle}"
  fi
}

check_public_api() {
  local url="${BASE_URL}${PUBLIC_API_PATH}"
  local body
  if ! body="$(curl_get_body "${url}" 2>/dev/null)"; then
    record_block "公开 API 请求失败"
    return
  fi
  if printf '%s' "${body}" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
    record_pass "公开 API 返回 ok=true"
  else
    record_block "公开 API 未返回 ok=true"
  fi
}

check_private_api_protected() {
  local url="${BASE_URL}${PRIVATE_API_PATH}"
  local response
  if ! response="$(curl -sS -i --connect-timeout "${CONNECT_TIMEOUT}" --max-time "${TIMEOUT}" "${url}" 2>/dev/null)"; then
    record_block "私有 API 请求失败"
    return
  fi
  if printf '%s' "${response}" | grep -Eq '^HTTP/[0-9.]+[[:space:]]+401'; then
    record_pass "私有 API 未授权返回 401"
  elif printf '%s' "${response}" | grep -Eq '^HTTP/[0-9.]+[[:space:]]+200'; then
    record_block "私有 API 未带令牌返回 200，后台 API 可能已暴露"
  else
    record_block "私有 API 未返回预期的 401 Unauthorized"
  fi
}

check_nginx_ports() {
  local output
  if command -v ss >/dev/null 2>&1; then
    if sudo -n true >/dev/null 2>&1; then
      output="$(sudo ss -ltnp 2>/dev/null || true)"
    else
      output="$(ss -ltnp 2>/dev/null || true)"
    fi
  else
    record_block "系统缺少 ss，无法检查 Nginx 80/443 监听"
    return
  fi

  local has_80=0
  local has_443=0
  local has_8080=0
  printf '%s\n' "${output}" | grep -E '(^|[[:space:]])[^[:space:]]*:80[[:space:]]' | grep -qi nginx && has_80=1
  printf '%s\n' "${output}" | grep -E '(^|[[:space:]])[^[:space:]]*:443[[:space:]]' | grep -qi nginx && has_443=1
  printf '%s\n' "${output}" | grep -E '(^|[[:space:]])[^[:space:]]*:8080[[:space:]]' >/dev/null 2>&1 && has_8080=1

  if [ "${has_80}" -eq 1 ] && [ "${has_443}" -eq 1 ]; then
    record_pass "Nginx 80/443 监听正常"
  else
    [ "${has_80}" -eq 1 ] || record_block "Nginx 未监听 80，HTTP 到 HTTPS 跳转可能不可用"
    [ "${has_443}" -eq 1 ] || record_block "Nginx 未监听 443，HTTPS 正式入口不可用"
  fi

  if [ "${has_8080}" -eq 1 ]; then
    record_warn "本机 8080 仍在监听，这是 Nginx 反代后端入口；请确认云安全组已关闭公网 8080"
  fi
}

check_services() {
  if [ "${SKIP_SERVICES}" -eq 1 ]; then
    record_warn "已跳过 systemd 服务检查"
    return
  fi
  local service
  local failed=0
  for service in paopao-web paopao-radar paopao-structure paopao-ai; do
    if systemctl is-active --quiet "${service}"; then
      record_pass "systemd 服务 active: ${service}"
    else
      failed=1
      if [ "${service}" = "paopao-ai" ]; then
        record_warn "paopao-ai 未 active；如果生产配置关闭 AI 助手可以忽略"
      else
        record_block "systemd 服务未 active: ${service}"
      fi
    fi
  done
  [ "${failed}" -eq 0 ] || true
}

check_cert_files() {
  local renewal="/etc/letsencrypt/renewal/${DOMAIN}.conf"
  local fullchain="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  local privkey="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"
  local missing=0
  [ -f "${renewal}" ] || { record_block "缺少证书续期配置: ${renewal}"; missing=1; }
  [ -f "${fullchain}" ] || { record_block "缺少证书文件: ${fullchain}"; missing=1; }
  [ -f "${privkey}" ] || { record_block "缺少证书私钥文件: ${privkey}"; missing=1; }
  [ "${missing}" -eq 1 ] || record_pass "Let's Encrypt 证书和续期配置存在"
}

check_certbot_dry_run() {
  if [ "${WITH_CERTBOT_DRY_RUN}" -ne 1 ]; then
    record_warn "未执行 certbot renew --dry-run；需要时传入 --with-certbot-dry-run"
    return
  fi
  if sudo certbot renew --dry-run >/tmp/paopao_certbot_dry_run.out 2>/tmp/paopao_certbot_dry_run.err; then
    record_pass "certbot renew --dry-run 成功"
  else
    record_block "certbot renew --dry-run 失败，请在服务器查看 certbot 输出"
  fi
  rm -f /tmp/paopao_certbot_dry_run.out /tmp/paopao_certbot_dry_run.err
}

check_stable_check() {
  if [ "${WITH_STABLE_CHECK}" -ne 1 ]; then
    record_warn "未执行 stable-check；需要时传入 --with-stable-check"
    return
  fi
  # 生产服务器固定使用: .venv/bin/python main.py stable-check
  local py="${APP_DIR}/.venv/bin/python"
  if [ ! -x "${py}" ]; then
    record_block "缺少生产 Python: ${py}"
    return
  fi
  local out
  local rc
  out="$(cd "${APP_DIR}" && "${py}" main.py stable-check 2>&1)"
  rc=$?
  if [ "${rc}" -eq 0 ]; then
    record_pass "stable-check 通过"
  elif printf '%s' "${out}" | grep -Eq '基本可运行|attention|关注'; then
    record_warn "stable-check 返回 ${rc}，输出显示为关注项，请打开诊断报告确认"
  else
    record_block "stable-check 返回 ${rc}，存在阻断项"
  fi
}

check_logs() {
  if [ "${SKIP_LOGS}" -eq 1 ]; then
    record_warn "已跳过 journalctl 日志检查"
    return
  fi
  local services=(paopao-web paopao-radar paopao-structure paopao-ai)
  local lines=(300 150 150 150)
  local pattern='Traceback|Exception occurred during processing|sqlite database is locked|no such table|/api/.* 500|/public-api/.* 500|/admin 500|Web JS error|TelegramGateway\._record failed'
  local noise='BrokenPipeError|ConnectionResetError|client disconnected|ReadTimeout'
  local total=0
  local i
  for i in "${!services[@]}"; do
    local service="${services[$i]}"
    local count
    count="$(journalctl -u "${service}" -n "${lines[$i]}" --no-pager 2>/dev/null | grep -E "${pattern}" | grep -Ev "${noise}" | wc -l | tr -d ' ')"
    if [ "${count:-0}" -gt 0 ]; then
      total=$((total + count))
      record_block "日志发现 ${service} 阻断关键词 ${count} 条"
    fi
  done
  if [ "${total}" -eq 0 ]; then
    record_pass "日志未发现阻断错误"
  fi
}

echo "泡泡雷达 HTTPS 部署验收"
echo "时间: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "目标: ${BASE_URL}"
echo

check_nginx_ports
check_page_contains "HTTPS 公开前台" "${BASE_URL}${ROOT_PATH}" "Paoxx Signal Radar"
check_page_contains "HTTPS 后台" "${BASE_URL}${ADMIN_PATH}" "泡泡雷达控制台"
check_public_api
check_private_api_protected
check_services
check_cert_files
check_certbot_dry_run
check_stable_check
check_logs

echo "检查结果:"
printf '%s\n' "${RESULTS[@]}"
echo
echo "结论:"
if [ "${BLOCK_COUNT}" -gt 0 ]; then
  echo "部署验收未通过：阻断 ${BLOCK_COUNT}，警告 ${WARN_COUNT}，通过 ${PASS_COUNT}"
  exit 1
fi

echo "部署验收通过：通过 ${PASS_COUNT}，警告 ${WARN_COUNT}，阻断 0"
exit 0
