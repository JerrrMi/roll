#!/usr/bin/env bash
# 阶段 3：live 对账（不下单）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

CONFIG="config/settings.live.yaml"
SECRETS="config/secrets/live.env"
DIR="$(_roll_acceptance_ensure_session_dir)"
OUT="${DIR}/live-reconcile.txt"

echo "[acceptance] 阶段 3 — live 对账"
_roll_acceptance_reconcile "$CONFIG" "$SECRETS" "live 对账" "$OUT"
_roll_acceptance_assert_reconcile_clean "$OUT"
echo "[acceptance] live 对账通过: $OUT"
exit 0
