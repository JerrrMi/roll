#!/usr/bin/env bash
# 根据当前用户与 roll-env Python 路径生成并安装 systemd 单元。
#
# 用法：
#   bash scripts/deploy/install-systemd.sh              # 仅 Testnet
#   bash scripts/deploy/install-systemd.sh --live       # Testnet + live
#   bash scripts/deploy/install-systemd.sh --live-only  # 仅 live
#
# 环境变量（可选）：
#   ROLL_USER=ubuntu
#   ROLL_HOME=/home/ubuntu
#   ROLL_PROJECT=/opt/roll
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

INSTALL_LIVE=0
INSTALL_TESTNET=1
for arg in "$@"; do
  case "$arg" in
    --live) INSTALL_LIVE=1 ;;
    --live-only) INSTALL_LIVE=1; INSTALL_TESTNET=0 ;;
    -h|--help)
      echo "用法: $0 [--live] [--live-only]"
      exit 0
      ;;
    *) _roll_deploy_die "未知参数: $arg" ;;
  esac
done

_roll_deploy_activate_conda
_roll_deploy_cd_root
ROOT="$(pwd)"

ROLL_USER="$(_roll_deploy_roll_user)"
ROLL_HOME="${ROLL_HOME:-$(_roll_deploy_roll_home)}"
ROLL_PROJECT="${ROLL_PROJECT:-$ROOT}"
PYTHON="$(_roll_deploy_python_path)"

echo "[deploy] User=$ROLL_USER"
echo "[deploy] WorkingDirectory=$ROLL_PROJECT"
echo "[deploy] Python=$PYTHON"

if [[ ! -f "$PYTHON" ]]; then
  _roll_deploy_die "Python 不存在: $PYTHON"
fi

if [[ "$EUID" -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

render_unit() {
  local src="$1" unit="$2" secrets_env="$3"
  local dst="/tmp/${unit}"
  sed \
    -e "s|^User=.*|User=${ROLL_USER}|" \
    -e "s|^Group=.*|Group=${ROLL_USER}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${ROLL_PROJECT}|" \
    -e "s|^EnvironmentFile=-.*|EnvironmentFile=-${ROLL_PROJECT}/config/secrets/${secrets_env}|" \
    -e "s|^ExecStart=/.*python|ExecStart=${PYTHON}|" \
    -e "s|file:///opt/roll/README.md|file://${ROLL_PROJECT}/README.md|" \
    "$src" >"$dst"
  echo "[deploy] 安装 ${unit}"
  $SUDO cp "$dst" "/etc/systemd/system/${unit}"
}

if [[ "$INSTALL_TESTNET" -eq 1 ]]; then
  render_unit "${ROOT}/deploy/systemd/roll-testnet.service" roll-testnet.service testnet.env
fi
if [[ "$INSTALL_LIVE" -eq 1 ]]; then
  render_unit "${ROOT}/deploy/systemd/roll-live.service" roll-live.service live.env
fi

$SUDO systemctl daemon-reload
echo "[deploy] systemctl daemon-reload 完成"

if [[ "$INSTALL_TESTNET" -eq 1 ]]; then
  echo "[deploy] Testnet: 对账后 sudo systemctl start roll-testnet"
fi
if [[ "$INSTALL_LIVE" -eq 1 ]]; then
  echo "[deploy] live: 完成阶段 1–4 验收后再 sudo systemctl start roll-live（默认勿 enable）"
fi
