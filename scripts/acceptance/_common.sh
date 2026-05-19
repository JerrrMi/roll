# shellcheck shell=bash
# 验收脚本公共函数：项目根、conda、会话目录。

set -euo pipefail

_roll_acceptance_die() {
  echo "[acceptance] ERROR: $*" >&2
  exit 1
}

_roll_acceptance_project_root() {
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  printf '%s' "$root"
}

_roll_acceptance_activate_conda() {
  # 用户要求：所有 python 命令前须 conda activate roll-env
  if command -v conda >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    local base
    base="$(conda info --base 2>/dev/null)" || _roll_acceptance_die "conda info --base 失败"
    # shellcheck source=/dev/null
    source "${base}/etc/profile.d/conda.sh"
    conda activate roll-env || _roll_acceptance_die "无法激活 roll-env，请先创建该环境"
    return 0
  fi
  if [[ -n "${CONDA_PREFIX:-}" ]] && [[ "${CONDA_PREFIX}" == *"roll-env"* ]]; then
    return 0
  fi
  _roll_acceptance_die "未找到 conda，且当前未在 roll-env 中；请先执行: conda activate roll-env"
}

_roll_acceptance_cd_root() {
  local root
  root="$(_roll_acceptance_project_root)"
  cd "$root" || _roll_acceptance_die "无法进入项目目录: $root"
}

_roll_acceptance_session_id() {
  if [[ -n "${ROLL_ACCEPTANCE_SESSION:-}" ]]; then
    printf '%s' "$ROLL_ACCEPTANCE_SESSION"
    return 0
  fi
  date -u +%Y%m%dT%H%M%SZ
}

_roll_acceptance_session_dir() {
  local root sid
  root="$(_roll_acceptance_project_root)"
  sid="$(_roll_acceptance_session_id)"
  printf '%s/logs/acceptance/%s' "$root" "$sid"
}

_roll_acceptance_ensure_session_dir() {
  local dir
  dir="$(_roll_acceptance_session_dir)"
  mkdir -p "$dir"
  printf '%s' "$dir"
}

_roll_acceptance_run_python() {
  _roll_acceptance_activate_conda
  _roll_acceptance_cd_root
  python -m main "$@"
}

_roll_acceptance_reconcile() {
  local config secrets label outfile
  config="$1"
  secrets="$2"
  label="$3"
  outfile="${4:-}"
  _roll_acceptance_activate_conda
  _roll_acceptance_cd_root
  if [[ -n "$outfile" ]]; then
    echo "[acceptance] $label -> $outfile"
    python -m main reconcile-state --config "$config" --secrets-file "$secrets" | tee "$outfile"
    return "${PIPESTATUS[0]}"
  fi
  echo "[acceptance] $label"
  python -m main reconcile-state --config "$config" --secrets-file "$secrets"
}

_roll_acceptance_assert_reconcile_clean() {
  local outfile="$1"
  if grep -q "halt_automatic_trading=True" "$outfile" 2>/dev/null; then
    _roll_acceptance_die "对账 halt_automatic_trading=True，见: $outfile"
  fi
  if grep -E "nonzero_position_symbols=\[[^]]+\]" "$outfile" 2>/dev/null; then
    _roll_acceptance_die "对账存在持仓 nonzero_position_symbols，见: $outfile"
  fi
  if grep -E "symbols_with_open_orders=\[[^]]+\]" "$outfile" 2>/dev/null; then
    _roll_acceptance_die "对账存在挂单 symbols_with_open_orders，见: $outfile"
  fi
}
