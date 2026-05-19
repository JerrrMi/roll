#!/usr/bin/env bash
# 阶段 2：检查 live dry-run 是否已连续运行满 24 小时。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

ROOT="$(_roll_acceptance_project_root)"
STAMP_FILE="${ROOT}/logs/acceptance/dry-run-started-at.txt"
REQUIRED_HOURS="${ROLL_DRY_RUN_MIN_HOURS:-24}"

if [[ ! -f "$STAMP_FILE" ]]; then
  _roll_acceptance_die "未找到 $STAMP_FILE — 请先运行 phase2-live-dry-run-start.sh"
fi

START="$(tr -d '\r\n' <"$STAMP_FILE")"
START_EPOCH="$(date -u -d "$START" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$START" +%s 2>/dev/null || _roll_acceptance_die "无法解析开始时间: $START")"
NOW_EPOCH="$(date -u +%s)"
ELAPSED_H=$(( (NOW_EPOCH - START_EPOCH) / 3600 ))
ELAPSED_SEC=$(( NOW_EPOCH - START_EPOCH ))

echo "[acceptance] dry-run 开始 (UTC): $START"
echo "[acceptance] 当前 (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[acceptance] 已运行: ${ELAPSED_H} 小时 (${ELAPSED_SEC} 秒)；要求 ≥ ${REQUIRED_HOURS} 小时"

if [[ "$ELAPSED_SEC" -lt $(( REQUIRED_HOURS * 3600 )) ]]; then
  echo "[acceptance] 未满 ${REQUIRED_HOURS}h — 请继续运行 dry-run" >&2
  exit 1
fi

echo "[acceptance] 已满 ${REQUIRED_HOURS} 小时，可进入阶段 3（停止 dry-run 后执行 live 对账）"
exit 0
