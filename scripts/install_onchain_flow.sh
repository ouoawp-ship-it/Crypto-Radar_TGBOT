#!/usr/bin/env bash
set -euo pipefail

enable_service=false
case "${1:-}" in
  "")
    ;;
  --enable)
    enable_service=true
    ;;
  --help)
    echo "Usage: scripts/install_onchain_flow.sh [--enable]"
    exit 0
    ;;
  *)
    echo "Unknown argument: $1" >&2
    exit 2
    ;;
esac

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "${script_dir}/.." && pwd)"
python_bin="${ONCHAIN_PYTHON_BIN:-${repo_dir}/.venv/bin/python}"
systemd_dir="${ONCHAIN_SYSTEMD_DIR:-/etc/systemd/system}"
unit_name="paopao-onchain-flow.service"
unit_path="${systemd_dir}/${unit_name}"
env_file="${repo_dir}/.env.onchain"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
if [[ -z "${SERVICE_USER}" ]]; then
  echo "SERVICE_USER cannot be empty" >&2
  exit 1
fi
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"

mkdir -p "${systemd_dir}"
temporary="$(mktemp)"
trap 'rm -f "${temporary}"' EXIT

cat >"${temporary}" <<EOF
[Unit]
Description=Paopao isolated Base on-chain CEX flow listener
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${repo_dir}
EnvironmentFile=-${repo_dir}/.env.oi
EnvironmentFile=-${repo_dir}/.env.onchain
ExecStart=${python_bin} ${repo_dir}/onchain_main.py live
Restart=always
RestartSec=10
MemoryHigh=256M
MemoryMax=384M
TasksMax=128
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

install -m 0644 "${temporary}" "${unit_path}"
grep -Fq "ExecStart=${python_bin} ${repo_dir}/onchain_main.py live" "${unit_path}"
echo "Validated ${unit_path}; no service was enabled, started, stopped, or restarted."

if [[ "${enable_service}" != "true" ]]; then
  exit 0
fi

if [[ "${SERVICE_USER}" == "root" && "${ONCHAIN_ALLOW_ROOT_SERVICE:-false}" != "true" ]]; then
  echo "Refusing to enable the service as root without ONCHAIN_ALLOW_ROOT_SERVICE=true" >&2
  exit 1
fi

if [[ ! -f "${env_file}" ]]; then
  echo "${env_file} is required for --enable" >&2
  exit 1
fi
if ! grep -Eq '^ONCHAIN_ENABLE=(true|1|yes|on)$' "${env_file}"; then
  echo "ONCHAIN_ENABLE=true is required for --enable" >&2
  exit 1
fi
if ! grep -Eq '^ONCHAIN_BASE_ENABLE=(true|1|yes|on)$' "${env_file}"; then
  echo "ONCHAIN_BASE_ENABLE=true is required for --enable" >&2
  exit 1
fi
if ! grep -Eq '^ONCHAIN_BASE_HTTP_RPC_URL=.+$' "${env_file}"; then
  echo "A Base HTTP RPC is required for --enable" >&2
  exit 1
fi

(
  cd "${repo_dir}"
  "${python_bin}" onchain_main.py doctor
  "${python_bin}" onchain_main.py labels-check
  "${python_bin}" onchain_main.py provider-check --chain base
)
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" \
  "${repo_dir}/data/onchain"
if ! runuser -u "${SERVICE_USER}" -- test -w "${repo_dir}/data/onchain"; then
  echo "${repo_dir}/data/onchain is not writable by ${SERVICE_USER}" >&2
  exit 1
fi
systemctl daemon-reload
systemctl enable --now "${unit_name}"
