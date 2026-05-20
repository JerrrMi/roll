#!/usr/bin/env bash
# 阶段 5：验收通过后启动 roll-live.service（需显式确认）。
#
# 用法：
#   export ROLL_ALLOW_SYSTEMD_START=1
#   bash scripts/acceptance/phase5-live-systemd-start.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

if [[ "${ROLL_ALLOW_SYSTEMD_START:-}" != "1" ]]; then
  _roll_acceptance_die "为防误启实盘，请先执行: export ROLL_ALLOW_SYSTEMD_START=1"
fi

if ! grep -q "live_trading_enabled: true" config/settings.live.yaml 2>/dev/null; then
  _roll_acceptance_die "settings.live.yaml 中 live_trading_enabled 须为 true"
fi

echo "[acceptance] 阶段 5 — 启动 roll-live.service"

if [[ ! -f /etc/systemd/system/roll-live.service ]]; then
  _roll_acceptance_die "未找到 /etc/systemd/system/roll-live.service；请先运行: bash scripts/deploy/install-systemd.sh --live-only"
fi

if pgrep -af "run-loop.*settings.live.yaml.*--no-dry-run" 2>/dev/null | grep -v "pgrep" >/dev/null; then
  _roll_acceptance_die "检测到前台 live run-loop，请先 Ctrl+C 或 stop 后再启动 systemd"
fi

LOCK="data/roll_state_live.json.lock"
if [[ -f "$LOCK" ]]; then
  _roll_acceptance_die "存在锁文件 $LOCK，可能有残留 live 进程"
fi

echo "[acceptance] 启动前 live 对账…"
"${SCRIPT_DIR}/phase3-live-reconcile.sh"

if systemctl is-active --quiet roll-live 2>/dev/null; then
  echo "[acceptance] roll-live 已在运行"
  sudo systemctl status roll-live --no-pager || true
  exit 0
fi

echo "[acceptance] sudo systemctl start roll-live"
sudo systemctl start roll-live
sleep 2
sudo systemctl status roll-live --no-pager

DIR="$(_roll_acceptance_ensure_session_dir)"
if command -v journalctl >/dev/null 2>&1; then
  journalctl -u roll-live -n 100 --no-pager | tee "${DIR}/journal-roll-live-start.txt"
fi

echo "[acceptance] 阶段 5 已启动。默认勿执行: sudo systemctl enable roll-live"
echo "[acceptance] 停止: sudo systemctl stop roll-live && bash scripts/acceptance/phase3-live-reconcile.sh"
