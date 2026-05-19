#!/usr/bin/env bash
# 阶段 0：配置/密钥/状态路径隔离预检（不下单、不对账交易所）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "${SCRIPT_DIR}/_common.sh"

_roll_acceptance_activate_conda
_roll_acceptance_cd_root

echo "[acceptance] 项目根: $(pwd)"
echo "[acceptance] Python: $(which python)"

fail=0
_warn() { echo "[acceptance] WARN: $*" >&2; }
_fail() { echo "[acceptance] FAIL: $*" >&2; fail=1; }

for f in config/settings.testnet.yaml config/settings.live.yaml; do
  if [[ ! -f "$f" ]]; then
    _fail "缺少 $f — 请从 *.example.yaml 复制"
  fi
done

for f in config/secrets/testnet.env config/secrets/live.env; do
  if [[ ! -f "$f" ]]; then
    _warn "缺少 $f — signed / 对账阶段需要"
  elif [[ "$(uname -s 2>/dev/null || echo)" != MINGW* ]] && [[ "$(uname -s 2>/dev/null || echo)" != MSYS* ]]; then
    perm="$(stat -c '%a' "$f" 2>/dev/null || stat -f '%OLp' "$f" 2>/dev/null || echo '')"
    if [[ -n "$perm" && "$perm" != "600" ]]; then
      _warn "$f 权限为 $perm，建议 chmod 600"
    fi
  fi
done

python <<'PY'
import sys
from pathlib import Path

import yaml

root = Path(".")
fail = False

def load(p: Path) -> dict:
    if not p.is_file():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}

tn = load(root / "config/settings.testnet.yaml")
lv = load(root / "config/settings.live.yaml")

def secrets_path(cfg: dict) -> str:
    s = cfg.get("secrets")
    if isinstance(s, dict) and s.get("file"):
        return str(s["file"])
    return ""

def state_path(cfg: dict) -> str:
    st = cfg.get("state")
    if isinstance(st, dict) and st.get("path"):
        return str(st["path"])
    return ""

def strat(cfg: dict, key: str) -> bool:
    st = cfg.get("strategy")
    if isinstance(st, dict):
        return bool(st.get(key))
    return False

tn_sec, lv_sec = secrets_path(tn), secrets_path(lv)
tn_st, lv_st = state_path(tn), state_path(lv)

if tn_sec == lv_sec and tn_sec:
    print(f"FAIL: Testnet 与 live secrets.file 相同: {tn_sec}", file=sys.stderr)
    fail = True
if tn_st == lv_st and tn_st:
    print(f"FAIL: Testnet 与 live state.path 相同: {tn_st}", file=sys.stderr)
    fail = True

tn_rb = (tn.get("binance") or {}).get("rest_base", "")
lv_rb = (lv.get("binance") or {}).get("rest_base", "")
if "testnet.binancefuture.com" not in str(tn_rb):
    print(f"WARN: Testnet rest_base 异常: {tn_rb!r}", file=sys.stderr)
if "dapi.binance.com" in str(tn_rb).lower() or "dapi.binance.com" in str(lv_rb).lower():
    print("FAIL: rest_base 不得指向 COIN-M host dapi.binance.com；USD-M live 应使用 fapi.binance.com", file=sys.stderr)
    fail = True
if "fapi.binance.com" not in str(lv_rb):
    print(f"FAIL: live rest_base 应为 https://fapi.binance.com（USD-M），当前: {lv_rb!r}", file=sys.stderr)
    fail = True

tn_prefix = (tn.get("binance") or {}).get("api_prefix", "")
lv_prefix = (lv.get("binance") or {}).get("api_prefix", "")
if "dapi" in str(tn_prefix).lower() or "dapi" in str(lv_prefix).lower():
    print("FAIL: api_prefix 不得含 /dapi（COIN-M）", file=sys.stderr)
    fail = True
if tn.get("binance", {}).get("product") != "usdm" or lv.get("binance", {}).get("product") != "usdm":
    print("FAIL: binance.product 须均为 usdm（USD-M / U 本位）", file=sys.stderr)
    fail = True

if strat(lv, "live_trading_enabled"):
    print("INFO: live_trading_enabled=true（阶段 4 signed 需要；阶段 2 dry-run 应暂时为 false）")
else:
    print("INFO: live_trading_enabled=false（适合阶段 2 dry-run）")

if not strat(tn, "testnet_signed_orders_enabled"):
    print("WARN: testnet_signed_orders_enabled=false — 阶段 1 前请改为 true", file=sys.stderr)

sys.exit(1 if fail else 0)
PY
py_rc=$?
if [[ "$py_rc" -ne 0 ]]; then
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  _roll_acceptance_die "预检未通过"
fi

echo "[acceptance] 预检通过"
