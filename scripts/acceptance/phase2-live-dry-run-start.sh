#!/usr/bin/env bash
# 阶段 2：启动 live 配置 dry-run（无 --no-dry-run），连续观察 ≥24h。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

CONFIG="config/settings.live.yaml"
DIR="$(_roll_acceptance_ensure_session_dir)"
ROOT="$(_roll_acceptance_project_root)"
STAMP_FILE="${ROOT}/logs/acceptance/dry-run-started-at.txt"
LOG="${DIR}/live-dry-run.log"

if grep -q "live_trading_enabled: true" config/settings.live.yaml 2>/dev/null; then
  echo "[acceptance] WARN: live_trading_enabled=true — 本脚本不会加 --no-dry-run，但仍请确认你理解 dry-run 与 signed 区别" >&2
fi

UTC_NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$UTC_NOW" >"$STAMP_FILE"
echo "[acceptance] dry-run 开始 UTC: $UTC_NOW"
echo "[acceptance] 时间戳文件: $STAMP_FILE"
echo "[acceptance] 日志: $LOG"
echo "[acceptance] 满 24 小时后 Ctrl+C 停止，再运行: bash scripts/acceptance/phase2-live-dry-run-check.sh"

_roll_acceptance_activate_conda
_roll_acceptance_cd_root
exec python -m main run-loop --config "$CONFIG" 2>&1 | tee "$LOG"
