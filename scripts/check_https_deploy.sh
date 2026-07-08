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
CERTBOT_DRY_RUN_OK=0
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
  页面检查使用 GET，不使用 HEAD；公开前台由 paopao-frontend 提供，/admin 和 API 由 paopao-web 提供。
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

sanitize_file_for_summary() {
  local path="$1"
  sed -E \
    -e 's/[0-9]{6,12}:[A-Za-z0-9_-]{20,}/<redacted:telegram-token>/g' \
    -e 's/(sk|rk|pk)-[A-Za-z0-9_-]{12,}/<redacted:api-key>/g' \
    -e 's/([Tt]oken|[Aa]uthorization|[Aa][Pp][Ii][_-]?[Kk]ey|[Ss]ecret|[Pp]assword)[^[:space:]]*/\1=<redacted>/g' \
    "${path}" | head -n 8
}

curl_get_to_file() {
  local url="$1"
  local output_file="$2"
  curl -sS -L \
    --connect-timeout "${CONNECT_TIMEOUT}" \
    --max-time "${TIMEOUT}" \
    -w '%{http_code} %{size_download}' \
    -o "${output_file}" \
    "${url}"
}

check_page_any_contains() {
  local label="$1"
  local url="$2"
  shift 2
  local tmp_file
  tmp_file="$(mktemp)"
  local curl_meta
  if ! curl_meta="$(curl_get_to_file "${url}" "${tmp_file}" 2>/dev/null)"; then
    record_block "${label} GET 请求失败: ${url}"
    rm -f "${tmp_file}"
    return
  fi

  local http_code
  local size_download
  http_code="$(printf '%s' "${curl_meta}" | awk '{print $1}')"
  size_download="$(printf '%s' "${curl_meta}" | awk '{print $2}')"
  local matched=""
  local needle
  for needle in "$@"; do
    if grep -aFq "${needle}" "${tmp_file}"; then
      matched="${needle}"
      break
    fi
  done

  if [ -n "${matched}" ]; then
    record_pass "${label} GET 返回 ${matched}"
  else
    record_block "${label} 未找到任一预期内容；HTTP_CODE=${http_code:-unknown}，下载字节数=${size_download:-0}"
    echo "${label} 页面前 8 行摘要:"
    sanitize_file_for_summary "${tmp_file}" || true
  fi
  rm -f "${tmp_file}"
}

check_public_api() {
  local url="${BASE_URL}${PUBLIC_API_PATH}"
  local tmp_file
  tmp_file="$(mktemp)"
  local curl_meta
  if ! curl_meta="$(curl_get_to_file "${url}" "${tmp_file}" 2>/dev/null)"; then
    record_block "公开 API 请求失败"
    rm -f "${tmp_file}"
    return
  fi
  if grep -aEq '"ok"[[:space:]]*:[[:space:]]*true' "${tmp_file}"; then
    record_pass "公开 API 返回 ok=true"
  else
    local http_code
    local size_download
    http_code="$(printf '%s' "${curl_meta}" | awk '{print $1}')"
    size_download="$(printf '%s' "${curl_meta}" | awk '{print $2}')"
    record_block "公开 API 未返回 ok=true；HTTP_CODE=${http_code:-unknown}，下载字节数=${size_download:-0}"
  fi
  rm -f "${tmp_file}"
}

check_private_api_protected() {
  local url="${BASE_URL}${PRIVATE_API_PATH}"
  local response
  if ! response="$(curl -sS -i -L --connect-timeout "${CONNECT_TIMEOUT}" --max-time "${TIMEOUT}" "${url}" 2>/dev/null)"; then
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
  local has_3000_loopback=0
  local has_3000_public=0
  local has_8080=0
  printf '%s\n' "${output}" | grep -E '(^|[[:space:]])[^[:space:]]*:80[[:space:]]' | grep -qi nginx && has_80=1
  printf '%s\n' "${output}" | grep -E '(^|[[:space:]])[^[:space:]]*:443[[:space:]]' | grep -qi nginx && has_443=1
  printf '%s\n' "${output}" | grep -E '127\.0\.0\.1:3000|localhost:3000|\[::1\]:3000' >/dev/null 2>&1 && has_3000_loopback=1
  printf '%s\n' "${output}" | grep -E '0\.0\.0\.0:3000|\*:3000|\[::\]:3000' >/dev/null 2>&1 && has_3000_public=1
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
  if [ "${has_3000_loopback}" -eq 1 ]; then
    record_pass "Next.js 前台监听 127.0.0.1:3000"
  else
    record_block "Next.js 前台未监听 127.0.0.1:3000"
  fi
  if [ "${has_3000_public}" -eq 1 ]; then
    record_block "Next.js 前台监听在公网地址 3000，请改为 127.0.0.1"
  fi
}

check_services() {
  if [ "${SKIP_SERVICES}" -eq 1 ]; then
    record_warn "已跳过 systemd 服务检查"
    return
  fi
  local service
  for service in paopao-frontend paopao-web paopao-radar paopao-structure paopao-ai; do
    if ! systemctl list-unit-files "${service}.service" --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "${service}.service"; then
      if [ "${service}" = "paopao-ai" ]; then
        record_warn "systemd 服务不存在: ${service}；如果生产配置关闭 AI 助手可以忽略"
      else
        record_block "systemd 服务不存在: ${service}"
      fi
      continue
    fi
    if systemctl is-active --quiet "${service}"; then
      record_pass "systemd 服务 active: ${service}"
    else
      if [ "${service}" = "paopao-ai" ]; then
        record_warn "paopao-ai 未 active；如果生产配置关闭 AI 助手可以忽略"
      else
        record_block "systemd 服务未 active: ${service}"
      fi
    fi
  done
}

path_exists_maybe_sudo() {
  local path="$1"
  if command -v sudo >/dev/null 2>&1; then
    if sudo -n true >/dev/null 2>&1; then
      sudo test -f "${path}" 2>/dev/null
      return $?
    fi
  fi
  test -f "${path}" 2>/dev/null
}

certbot_certificate_exists() {
  command -v certbot >/dev/null 2>&1 || return 1
  local output=""
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    output="$(sudo certbot certificates --cert-name "${DOMAIN}" 2>/dev/null || true)"
  else
    output="$(certbot certificates --cert-name "${DOMAIN}" 2>/dev/null || true)"
  fi
  printf '%s' "${output}" | grep -aFq "Certificate Name: ${DOMAIN}"
}

check_certbot_dry_run() {
  if [ "${WITH_CERTBOT_DRY_RUN}" -ne 1 ]; then
    record_warn "未执行 certbot renew --dry-run；需要时传入 --with-certbot-dry-run"
    return
  fi
  if sudo certbot renew --dry-run >/tmp/paopao_certbot_dry_run.out 2>/tmp/paopao_certbot_dry_run.err; then
    CERTBOT_DRY_RUN_OK=1
    record_pass "certbot renew --dry-run 成功"
  else
    record_block "certbot renew --dry-run 失败，请在服务器查看 certbot 输出"
  fi
  rm -f /tmp/paopao_certbot_dry_run.out /tmp/paopao_certbot_dry_run.err
}

check_cert_files() {
  local renewal="/etc/letsencrypt/renewal/${DOMAIN}.conf"
  local fullchain="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  local privkey="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"
  local missing=0
  path_exists_maybe_sudo "${renewal}" || missing=$((missing + 1))
  path_exists_maybe_sudo "${fullchain}" || missing=$((missing + 1))
  path_exists_maybe_sudo "${privkey}" || missing=$((missing + 1))

  if [ "${missing}" -eq 0 ]; then
    record_pass "Let's Encrypt 证书和续期配置存在"
    return
  fi

  if [ "${CERTBOT_DRY_RUN_OK}" -eq 1 ]; then
    record_warn "普通用户无法直接读取部分证书路径，但 certbot dry-run 已通过，证书链路按通过处理"
    return
  fi

  if certbot_certificate_exists; then
    record_warn "普通用户无法直接读取部分证书路径，但 certbot certificates 能找到 ${DOMAIN}，证书存在检查降为警告"
    return
  fi

  record_block "证书文件或续期配置不可验证，请使用 sudo 检查 /etc/letsencrypt 下的 ${DOMAIN} 证书"
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
  elif printf '%s' "${out}" | grep -aEq '状态:[[:space:]]*关注|attention|基本可运行|建议关注'; then
    record_warn "stable-check 返回 ${rc}，输出显示为关注项，请打开诊断报告确认"
  elif printf '%s' "${out}" | grep -aEq '阻断项|未达稳定版标准|failed|Traceback'; then
    record_block "stable-check 返回 ${rc}，输出显示存在阻断或异常"
  else
    record_block "stable-check 返回 ${rc}，无法识别为关注状态"
  fi
}

check_logs() {
  if [ "${SKIP_LOGS}" -eq 1 ]; then
    record_warn "已跳过 journalctl 日志检查"
    return
  fi
  local services=(paopao-frontend paopao-web paopao-radar paopao-structure paopao-ai)
  local lines=(200 300 150 150 150)
  local pattern='Traceback|Exception occurred during processing|sqlite database is locked|no such table|/api/.* 500|/public-api/.* 500|/admin 500| 500 |Web JS error|EADDRINUSE|ECONNREFUSED|TelegramGateway\._record failed'
  local noise='BrokenPipeError|ConnectionResetError|client disconnected|ReadTimeout'
  local total=0
  local i
  for i in "${!services[@]}"; do
    local service="${services[$i]}"
    local since=""
    local journal_output
    local matches
    since="$(systemctl show "${service}" -p ActiveEnterTimestamp --value 2>/dev/null || true)"
    if [ -n "${since}" ] && [ "${since}" != "n/a" ]; then
      journal_output="$(journalctl -u "${service}" --since "${since}" -n "${lines[$i]}" --no-pager 2>/dev/null || true)"
    else
      journal_output="$(journalctl -u "${service}" -n "${lines[$i]}" --no-pager 2>/dev/null || true)"
    fi
    matches="$(printf '%s\n' "${journal_output}" | grep -aE "${pattern}" | grep -aEv "${noise}" || true)"
    if [ -n "${matches}" ]; then
      local count
      count="$(printf '%s\n' "${matches}" | wc -l | tr -d ' ')"
      total=$((total + count))
      record_block "日志发现 ${service} 阻断关键词 ${count} 条，匹配片段如下"
      echo "${service} 日志匹配片段:"
      printf '%s\n' "${matches}" | tail -n 8 | sed -E \
        -e 's/[0-9]{6,12}:[A-Za-z0-9_-]{20,}/<redacted:telegram-token>/g' \
        -e 's/(sk|rk|pk)-[A-Za-z0-9_-]{12,}/<redacted:api-key>/g' \
        -e 's/([Tt]oken|[Aa]uthorization|[Aa][Pp][Ii][_-]?[Kk]ey|[Ss]ecret|[Pp]assword)[^[:space:]]*/\1=<redacted>/g'
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
check_page_any_contains "本机 Next.js 前台" "http://127.0.0.1:3000/" "paoxx-frontend" "nextjs-dashboard"
check_page_any_contains "HTTPS 公开前台" "${BASE_URL}${ROOT_PATH}" "paoxx-frontend" "nextjs-dashboard" "专业加密数据仪表盘"
check_page_any_contains "HTTPS 后台" "${BASE_URL}${ADMIN_PATH}" "泡泡雷达控制台" "brand-title" "/admin"
check_public_api
check_private_api_protected
check_services
check_certbot_dry_run
check_cert_files
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
