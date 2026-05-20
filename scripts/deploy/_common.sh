# shellcheck shell=bash
# 云服务器部署脚本公共函数。

set -euo pipefail

_roll_deploy_die() {
  echo "[deploy] ERROR: $*" >&2
  exit 1
}

_roll_deploy_project_root() {
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  printf '%s' "$root"
}

_roll_deploy_activate_conda() {
  if command -v conda >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    local base
    base="$(conda info --base 2>/dev/null)" || _roll_deploy_die "conda info --base 失败"
    # shellcheck source=/dev/null
    source "${base}/etc/profile.d/conda.sh"
    conda activate roll-env || _roll_deploy_die "无法激活 roll-env"
    return 0
  fi
  if [[ -n "${CONDA_PREFIX:-}" ]] && [[ "${CONDA_PREFIX}" == *"roll-env"* ]]; then
    return 0
  fi
  _roll_deploy_die "未找到 conda 或 roll-env；请先运行 bootstrap-ubuntu.sh"
}

_roll_deploy_cd_root() {
  local root
  root="$(_roll_deploy_project_root)"
  cd "$root" || _roll_deploy_die "无法进入: $root"
}

_roll_deploy_roll_user() {
  if [[ -n "${ROLL_USER:-}" ]]; then
    printf '%s' "$ROLL_USER"
    return 0
  fi
  printf '%s' "${SUDO_USER:-${USER:-ubuntu}}"
}

_roll_deploy_roll_home() {
  local u
  u="$(_roll_deploy_roll_user)"
  getent passwd "$u" 2>/dev/null | cut -d: -f6 || printf '/home/%s' "$u"
}

_roll_deploy_python_path() {
  _roll_deploy_activate_conda
  command -v python
}
