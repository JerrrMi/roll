#!/usr/bin/env bash
# 阶段 4：live 对账 → --once --no-dry-run → 停止后对账（极小资金须事先配置）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

CONFIG="config/settings.live.yaml"
SECRETS="config/secrets/live.env"
DIR="$(_roll_acceptance_ensure_session_dir)"

if ! grep -q "live_trading_enabled: true" config/settings.live.yaml 2>/dev/null; then
  _roll_acceptance_die "settings.live.yaml 中 live_trading_enabled 须为 true（阶段 4）"
fi

echo "[acceptance] 确认无其它 live signed 进程…"
if pgrep -af "run-loop.*settings.live.yaml.*--no-dry-run" 2>/dev/null | grep -v "pgrep"; then
  _roll_acceptance_die "检测到已有 live run-loop --no-dry-run 进程，请先停止"
fi

PRE="${DIR}/live-reconcile-pre.txt"
POST="${DIR}/live-reconcile-post.txt"
RUNLOG="${DIR}/live-run-once.log"

echo "[acceptance] 会话目录: $DIR"
echo "[acceptance] 阶段 4 — live 首次 signed 单轮（--once --no-dry-run）"

_roll_acceptance_reconcile "$CONFIG" "$SECRETS" "live 启动前对账" "$PRE"
_roll_acceptance_assert_reconcile_clean "$PRE"

echo "[acceptance] 运行 run-loop --once --no-dry-run（日志: $RUNLOG）"
_roll_acceptance_activate_conda
_roll_acceptance_cd_root
python -m main run-loop \
  --config "$CONFIG" \
  --secrets-file "$SECRETS" \
  --once --no-dry-run 2>&1 | tee "$RUNLOG"

_roll_acceptance_reconcile "$CONFIG" "$SECRETS" "live 停止后对账" "$POST"
_roll_acceptance_assert_reconcile_clean "$POST"

if [[ ! -f "${DIR}/record.md" ]]; then
  cp docs/templates/live-acceptance-record.template.md "${DIR}/record.md" 2>/dev/null || true
fi

echo "[acceptance] 阶段 4 完成。请填写 ${DIR}/record.md、复核 Binance 实盘 USD-M / U 本位合约网页。"
