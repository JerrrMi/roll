#!/usr/bin/env bash
# Ubuntu 云服务器首次部署：Conda 环境、依赖、示例配置、密钥目录权限。
#
# 用法（在项目根目录或任意路径执行均可）：
#   bash scripts/deploy/bootstrap-ubuntu.sh
#
# 可选环境变量：
#   ROLL_SKIP_CONDA_INSTALL=1   已安装 Miniconda 时跳过下载
#   ROLL_SKIP_PYTEST=1          跳过 pytest
#   ROLL_PYTHON_VERSION=3.12    roll-env 的 Python 版本
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

ROOT="$(_roll_deploy_project_root)"
cd "$ROOT"
PY_VER="${ROLL_PYTHON_VERSION:-3.12}"

echo "[deploy] 项目根: $ROOT"

if ! command -v conda >/dev/null 2>&1; then
  if [[ "${ROLL_SKIP_CONDA_INSTALL:-}" == "1" ]]; then
    _roll_deploy_die "未找到 conda 且 ROLL_SKIP_CONDA_INSTALL=1"
  fi
  echo "[deploy] 安装 Miniconda（用户目录）…"
  INSTALLER="/tmp/miniconda.sh"
  curl -fsSL "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh" -o "$INSTALLER"
  bash "$INSTALLER" -b -p "${HOME}/miniconda3"
  # shellcheck disable=SC1091
  source "${HOME}/miniconda3/etc/profile.d/conda.sh"
fi

# shellcheck disable=SC1091
source "$(conda info --base)/etc/profile.d/conda.sh"

if ! conda env list | awk '{print $1}' | grep -qx 'roll-env'; then
  echo "[deploy] 创建 conda 环境 roll-env (python=${PY_VER})…"
  conda create -y -n roll-env "python=${PY_VER}"
fi

conda activate roll-env
echo "[deploy] Python: $(which python) ($(python -V))"

echo "[deploy] pip install -e \".[dev]\" …"
pip install -e ".[dev]"

mkdir -p config/secrets data logs/acceptance

copy_if_missing() {
  local src="$1" dst="$2"
  if [[ ! -f "$dst" ]]; then
    cp "$src" "$dst"
    echo "[deploy] 已复制 $dst（请编辑后勿提交 Git）"
  else
    echo "[deploy] 已存在，跳过: $dst"
  fi
}

copy_if_missing config/settings.testnet.example.yaml config/settings.testnet.yaml
copy_if_missing config/settings.live.example.yaml config/settings.live.yaml
copy_if_missing config/secrets/testnet.env.example config/secrets/testnet.env
copy_if_missing config/secrets/live.env.example config/secrets/live.env

chmod 700 config/secrets 2>/dev/null || true
chmod 600 config/secrets/testnet.env config/secrets/live.env 2>/dev/null || true

if command -v timedatectl >/dev/null 2>&1; then
  echo "[deploy] 时间同步:"
  timedatectl status 2>/dev/null | head -5 || true
fi

if [[ "${ROLL_SKIP_PYTEST:-}" != "1" ]]; then
  echo "[deploy] 运行 pytest …"
  pytest
fi

echo ""
echo "[deploy] 引导完成。下一步："
echo "  1. 编辑 config/secrets/testnet.env 与 live.env（填入 API Key；live 禁止提现）"
echo "  2. 审查 config/settings.testnet.yaml 与 settings.live.yaml"
echo "  3. bash scripts/acceptance/preflight.sh"
echo "  4. 按 docs/cloud-server-live-deployment.md 完成验收后安装 systemd"
