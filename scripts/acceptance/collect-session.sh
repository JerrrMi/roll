#!/usr/bin/env bash
# 归档验收会话：对账输出、状态 JSON 快照、可选 journalctl、记录模板。
# 用法: bash scripts/acceptance/collect-session.sh [会话ID]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

if [[ -n "${1:-}" ]]; then
  export ROLL_ACCEPTANCE_SESSION="$1"
fi

DIR="$(_roll_acceptance_ensure_session_dir)"
_roll_acceptance_activate_conda
_roll_acceptance_cd_root

echo "[acceptance] 收集会话: $DIR"

_roll_acceptance_reconcile config/settings.testnet.yaml config/secrets/testnet.env \
  "归档 Testnet 对账" "${DIR}/archive-testnet-reconcile.txt" || true

_roll_acceptance_reconcile config/settings.live.yaml config/secrets/live.env \
  "归档 live 对账" "${DIR}/archive-live-reconcile.txt" || true

for f in data/roll_state_testnet.json data/roll_state_live.json; do
  if [[ -f "$f" ]]; then
    cp "$f" "${DIR}/$(basename "$f").snapshot"
    echo "[acceptance] 已复制 $f"
  fi
done

if command -v journalctl >/dev/null 2>&1; then
  journalctl -u roll-testnet -n 500 --no-pager >"${DIR}/journal-roll-testnet.txt" 2>/dev/null || true
  journalctl -u roll-live -n 500 --no-pager >"${DIR}/journal-roll-live.txt" 2>/dev/null || true
fi

if [[ ! -f "${DIR}/record.md" ]]; then
  cp docs/templates/live-acceptance-record.template.md "${DIR}/record.md"
fi

echo "[acceptance] 完成。请编辑: ${DIR}/record.md"
