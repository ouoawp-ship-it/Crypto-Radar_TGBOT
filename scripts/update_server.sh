#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="${SERVICE_NAME:-paopao-radar}"
STRUCTURE_SERVICE_NAME="${STRUCTURE_SERVICE_NAME:-paopao-structure}"
CLEANUP_SERVICE_NAME="${CLEANUP_SERVICE_NAME:-paopao-cleanup}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-paopao-web}"
FRONTEND_SERVICE_NAME="${FRONTEND_SERVICE_NAME:-paopao-frontend}"
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
  FRONTEND_SERVICE_NAME=paopao-frontend
  AI_SERVICE_NAME=paopao-ai
EOF
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
    printf '[paopao-update] Node.js 已满足 Next.js 前台构建要求: %s\n' "$(node --version)"
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    printf '[paopao-update] 未检测到 Node.js 20+，且当前系统没有 apt-get。请手动安装 Node.js LTS。\n' >&2
    exit 1
  fi

  printf '[paopao-update] 安装 Node.js 22 LTS，用于构建 Next.js 公开前台\n'
  run_root apt-get update
  run_root apt-get install -y curl ca-certificates gnupg
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

build_frontend_dashboard() {
  if [ ! -f "${APP_DIR}/frontend/package.json" ]; then
    printf '[paopao-update] 未发现 frontend/package.json，跳过 Next.js 公开前台构建\n'
    return 0
  fi

  ensure_node_runtime
  printf '[paopao-update] 构建 Next.js 公开前台\n'
  (
    cd "${APP_DIR}/frontend"
    frontend_install_deps
    npm run build
  )
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
    printf '[paopao-update] 后台账号密码未完整配置。请执行: .venv/bin/python main.py admin-password set\n'
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
  printf '[paopao-update] 如果上方“趋势变化”显示“发生回退”或“趋势变差”，请优先打开 Web 控制台 -> 诊断报告处理趋势告警。\n'
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

install_or_update_frontend_service() {
  command -v systemctl >/dev/null 2>&1 || return 0
  [ -f "${APP_DIR}/frontend/package.json" ] || return 0
  ensure_node_runtime
  local npm_bin
  npm_bin="$(npm_bin_path)"
  if [ -z "$npm_bin" ]; then
    printf '[paopao-update] 未检测到 npm，无法安装 Next.js 公开前台 systemd 服务\n' >&2
    exit 1
  fi
  local service_path="/etc/systemd/system/${FRONTEND_SERVICE_NAME}.service"
  run_root tee "$service_path" >/dev/null <<EOF
[Unit]
Description=Paopao Next.js Public Frontend
After=network.target ${WEB_SERVICE_NAME}.service

[Service]
Type=simple
User=${SUDO_USER:-$(id -un)}
WorkingDirectory=${APP_DIR}/frontend
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=HOSTNAME=127.0.0.1
Environment=PAOXX_PUBLIC_API_INTERNAL_BASE=http://127.0.0.1:8080
Environment=PAOXX_PUBLIC_API_TIMEOUT_MS=15000
ExecStart=${npm_bin} run start -- --hostname 127.0.0.1 --port 3000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  run_root systemctl daemon-reload
  run_root systemctl enable --now "$FRONTEND_SERVICE_NAME"
}

nginx_frontend_fail() {
  printf '[paopao-update] %s\n' "$*" >&2
  exit 1
}

resolve_nginx_path() {
  readlink -f "$1" 2>/dev/null || printf '%s' "$1"
}

backup_nginx_legacy_entry() {
  local path="$1"
  local backup_dir="$2"
  local stamp="$3"
  [ -e "$path" ] || [ -L "$path" ] || return 0

  run_root mkdir -p "$backup_dir"
  local backup_name
  backup_name="$(printf '%s' "$path" | sed 's#^/##; s#/#__#g')"
  run_root cp -a "$path" "${backup_dir}/${backup_name}.${stamp}" || true

  if [ -L "$path" ]; then
    run_root rm -f "$path"
  else
    run_root mv -f "$path" "${path}.disabled.${stamp}"
  fi
}

find_paoxx_nginx_server_files() {
  local domain="$1"
  local domain_regex
  domain_regex="$(printf '%s' "$domain" | sed 's/\./\\./g')"
  local pattern="server_name[[:space:]][^;]*(www\\.)?${domain_regex}"
  local dir file
  {
    for file in \
      "/etc/nginx/sites-enabled/default" \
      "/etc/nginx/sites-enabled/${domain}" \
      "/etc/nginx/sites-enabled/paoxx.com"; do
      if [ -e "$file" ] || [ -L "$file" ]; then
        printf '%s\n' "$file"
      fi
    done
    for dir in /etc/nginx/sites-enabled /etc/nginx/conf.d; do
      [ -d "$dir" ] || continue
      find "$dir" -maxdepth 1 \( -type f -o -type l \) -print 2>/dev/null | while IFS= read -r file; do
        [ -n "$file" ] || continue
        case "$file" in
          *.disabled.*) continue ;;
        esac
        if grep -Iq . "$file" 2>/dev/null && grep -Eq "$pattern" "$file" 2>/dev/null; then
          printf '%s\n' "$file"
        fi
      done
    done
  } | awk '!seen[$0]++'
}

cleanup_duplicate_paoxx_nginx_servers() {
  local domain="$1"
  local keep_path="$2"
  local backup_root="${NGINX_BACKUP_DIR:-/etc/nginx/backup-paopao}"
  local stamp="${PAOPAO_NGINX_DISABLE_STAMP:-$(date -u +%Y%m%d%H%M%S)}"
  local backup_dir="${backup_root}/duplicate-cleanup.${stamp}"
  local keep_real
  keep_real="$(resolve_nginx_path "$keep_path")"
  local file file_real disabled_count=0

  while IFS= read -r file; do
    [ -e "$file" ] || [ -L "$file" ] || continue
    file_real="$(resolve_nginx_path "$file")"
    if [ "$file_real" = "$keep_real" ]; then
      continue
    fi
    backup_nginx_legacy_entry "$file" "$backup_dir" "$stamp"
    disabled_count=$((disabled_count + 1))
    printf '[paopao-update] Disabled duplicate nginx entry: %s\n' "$file"
  done < <(find_paoxx_nginx_server_files "$domain")

  local remaining=""
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    file_real="$(resolve_nginx_path "$file")"
    if [ "$file_real" != "$keep_real" ]; then
      remaining="${remaining}${file}
"
    fi
  done < <(find_paoxx_nginx_server_files "$domain")
  if [ -n "$remaining" ]; then
    printf '%s\n' "$remaining" >&2
    printf '[paopao-update] 定位命令: sudo grep -RIn "server_name .*%s" /etc/nginx/sites-enabled /etc/nginx/conf.d\n' "$domain" >&2
    nginx_frontend_fail "Nginx duplicate ${domain} server blocks remain after cleanup"
  fi

  printf '[paopao-update] Nginx duplicate cleanup complete, disabled=%s, backup=%s\n' "$disabled_count" "$backup_dir"
}

verify_active_nginx_frontend_route() {
  local domain="$1"
  local active_path="$2"
  local test_output
  if ! test_output="$(run_root nginx -t 2>&1)"; then
    printf '%s\n' "$test_output" >&2
    nginx_frontend_fail "nginx -t failed after writing frontend route"
  fi
  if printf '%s' "$test_output" | grep -Fq "conflicting server name \"${domain}\""; then
    printf '%s\n' "$test_output" >&2
    printf '[paopao-update] 定位命令: sudo grep -RIn "server_name .*%s" /etc/nginx/sites-enabled /etc/nginx/conf.d\n' "$domain" >&2
    nginx_frontend_fail "Nginx has duplicate ${domain} server_name blocks"
  fi

  local active_config
  active_config="$(run_root nginx -T 2>&1 || true)"
  if printf '%s' "$active_config" | grep -Fq "conflicting server name \"${domain}\""; then
    printf '%s\n' "$active_config" | grep -F "conflicting server name" >&2 || true
    printf '[paopao-update] 定位命令: sudo grep -RIn "server_name .*%s" /etc/nginx/sites-enabled /etc/nginx/conf.d\n' "$domain" >&2
    nginx_frontend_fail "Nginx active config still has duplicate ${domain} server_name blocks"
  fi
  if ! printf '%s' "$active_config" | grep -Fq "$active_path"; then
    nginx_frontend_fail "Nginx active config missing ${active_path}"
  fi
  if ! printf '%s' "$active_config" | grep -Fq 'proxy_pass http://127.0.0.1:3000;'; then
    nginx_frontend_fail "Nginx active config missing Next.js proxy_pass 127.0.0.1:3000"
  fi
  if ! printf '%s' "$active_config" | grep -Fq 'proxy_pass http://127.0.0.1:8080;'; then
    nginx_frontend_fail "Nginx active config missing Python backend proxy_pass 127.0.0.1:8080"
  fi
  if ! printf '%s' "$active_config" | grep -Fq 'location ^~ /_next/'; then
    nginx_frontend_fail "Nginx active config missing /_next/ route"
  fi

  local duplicate_declarations
  duplicate_declarations="$(printf '%s\n' "$active_config" | awk -v domain="$domain" -v active="$active_path" '
    /^# configuration file / {file=$4; sub(/:$/, "", file)}
    index($0, "server_name") && index($0, domain) && file != active {print file ": " $0}
  ')"
  if [ -n "$duplicate_declarations" ]; then
    printf '%s\n' "$duplicate_declarations" >&2
    printf '[paopao-update] 定位命令: sudo grep -RIn "server_name .*%s" /etc/nginx/sites-enabled /etc/nginx/conf.d\n' "$domain" >&2
    nginx_frontend_fail "Nginx active config has extra ${domain} server_name declarations"
  fi
}

install_or_update_nginx_frontend_routes() {
  command -v nginx >/dev/null 2>&1 || {
    printf '[paopao-update] 未找到 nginx，跳过公开前台反代配置\n'
    return 0
  }
  local domain="${PUBLIC_DOMAIN:-paoxx.com}"
  local active_path="${NGINX_ACTIVE_SITE_PATH:-/etc/nginx/conf.d/00-paoxx-frontend.conf}"
  local fullchain="/etc/letsencrypt/live/${domain}/fullchain.pem"
  local privkey="/etc/letsencrypt/live/${domain}/privkey.pem"
  if ! run_root test -f "$fullchain" || ! run_root test -f "$privkey"; then
    printf '[paopao-update] 未找到 %s 证书文件，跳过 Nginx 反代配置写入\n' "$domain"
    return 0
  fi

  printf '[paopao-update] 写入 Nginx 公开前台 active 反代配置: %s\n' "$active_path"
  run_root mkdir -p "$(dirname "$active_path")"
  run_root tee "$active_path" >/dev/null <<EOF
server {
    listen 80;
    server_name ${domain};

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name ${domain};

    ssl_certificate ${fullchain};
    ssl_certificate_key ${privkey};
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

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

    location ^~ /_next/ {
        proxy_pass http://127.0.0.1:3000;
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
  cleanup_duplicate_paoxx_nginx_servers "$domain" "$active_path"
  verify_active_nginx_frontend_route "$domain" "$active_path"

  if command -v systemctl >/dev/null 2>&1; then
    run_root systemctl reload nginx
  else
    run_root nginx -s reload
  fi
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
export FRONTEND_SERVICE_NAME="${FRONTEND_SERVICE_NAME}"
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

  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${FRONTEND_SERVICE_NAME}.service" >/dev/null 2>&1; then
    run_root systemctl restart "$FRONTEND_SERVICE_NAME"
    run_root systemctl --no-pager --full status "$FRONTEND_SERVICE_NAME" || true
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
    build_frontend_dashboard
    install_shortcut_command
    install_or_update_structure_service
    install_or_update_cleanup_timer
    install_or_update_web_service
    install_or_update_frontend_service
    install_or_update_ai_service
    restart_services_if_present
    install_or_update_nginx_frontend_routes
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
build_frontend_dashboard

install_shortcut_command
install_or_update_structure_service
install_or_update_cleanup_timer
install_or_update_web_service
install_or_update_frontend_service
install_or_update_ai_service
restart_services_if_present
install_or_update_nginx_frontend_routes
run_post_update_stable_check

printf '\n[paopao-update] 更新完成: %s (%s)  %s\n' "$(version_for_ref HEAD)" "$(short_commit HEAD)" "$(commit_title HEAD)"
printf '[paopao-update] 公开前台: https://paoxx.com/ （Next.js Dashboard）\n'
printf '[paopao-update] 后台控制台: https://paoxx.com/admin\n'
printf '[paopao-update] 本机前台入口 3000 仅供 Nginx 反代使用，不作为公网访问入口。\n'
printf '[paopao-update] 本机后端入口 8080 仅供 Nginx 反代使用，不作为公网访问入口。\n'
