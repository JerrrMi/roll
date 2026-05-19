#!/usr/bin/env bash
# 阶段 1：Testnet 对账 → 单轮 signed → 停止后对账。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

CONFIG="config/settings.testnet.yaml"
SECRETS="config/secrets/testnet.env"
DIR="$(_roll_acceptance_ensure_session_dir)"

echo "[acceptance] 会话目录: $DIR"
echo "[acceptance] 阶段 1 — Testnet 开仓→平仓闭环"

if ! grep -q "testnet_signed_orders_enabled: true" config/settings.testnet.yaml 2>/dev/null; then
  echo "[acceptance] WARN: settings.testnet.yaml 中 testnet_signed_orders_enabled 可能未设为 true" >&2
fi

PRE="${DIR}/testnet-reconcile-pre.txt"
POST="${DIR}/testnet-reconcile-post.txt"
RUNLOG="${DIR}/testnet-run-once.log"

_roll_acceptance_reconcile "$CONFIG" "$SECRETS" "Testnet 启动前对账" "$PRE"
_roll_acceptance_assert_reconcile_clean "$PRE"

echo "[acceptance] 运行 run-loop --once --no-dry-run（日志: $RUNLOG）"
_roll_acceptance_activate_conda
_roll_acceptance_cd_root
python -m main run-loop \
  --config "$CONFIG" \
  --secrets-file "$SECRETS" \
  --once --no-dry-run 2>&1 | tee "$RUNLOG"

_roll_acceptance_reconcile "$CONFIG" "$SECRETS" "Testnet 停止后对账" "$POST"
_roll_acceptance_assert_reconcile_clean "$POST"

cp docs/templates/live-acceptance-record.template.md "${DIR}/record.md" 2>/dev/null || true

echo "[acceptance] 阶段 1 完成。请填写 ${DIR}/record.md 并复核 Testnet 网页 U 本位合约 / USD-M。"
