"""滚仓（浮盈加仓）共享规则：趋势门槛、浮盈判定、均价与状态合并。

Plan 3.0 §6.4：加仓须趋势仍强、有浮盈、次数上限，且增量数量受 Kelly / 最大仓位 / 单笔亏损约束（见 `risk.evaluate_add`）。
"""
from __future__ import annotations

from typing import Any, Mapping

from roll.risk import Side, linear_pnl_usdt
from roll.trend_model import SignalSide, TrendModelParams, TrendSignal


def trend_allows_add(*, holding: Side, sig: TrendSignal, tparams: TrendModelParams) -> bool:
    """加仓时趋势仍须「强」：与开仓同向且 |score| 达到 long/short_threshold。"""
    if holding == "long":
        return sig.side is SignalSide.LONG and sig.score >= tparams.long_threshold
    if holding == "short":
        return sig.side is SignalSide.SHORT and (-sig.score) >= tparams.short_threshold
    return False


def unrealized_return_fraction(*, side: Side, avg_entry: float, mark: float) -> float:
    """持仓未实现收益率（相对均价，多头为正表示盈利）。"""
    if avg_entry <= 0.0 or mark <= 0.0:
        return 0.0
    if side == "long":
        return (mark - avg_entry) / avg_entry
    return (avg_entry - mark) / avg_entry


def has_float_profit(
    *,
    side: Side,
    quantity: float,
    avg_entry: float,
    mark: float,
    min_return_fraction: float = 0.0,
) -> bool:
    """浮盈再投入：持仓未实现盈亏为正，且收益率不低于配置下限。"""
    if quantity <= 0.0 or avg_entry <= 0.0:
        return False
    upnl = linear_pnl_usdt(side, quantity, avg_entry, mark)
    if upnl <= 0.0:
        return False
    return unrealized_return_fraction(side=side, avg_entry=avg_entry, mark=mark) >= min_return_fraction


def weighted_avg_entry(
    *,
    prev_qty: float,
    prev_avg: float,
    add_qty: float,
    add_price: float,
) -> float:
    if add_qty <= 0.0:
        return prev_avg
    if prev_qty <= 0.0:
        return add_price
    return (prev_qty * prev_avg + add_qty * add_price) / (prev_qty + add_qty)


def parse_add_count(live_leaf: Mapping[str, Any]) -> int:
    raw = live_leaf.get("add_count")
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return max(raw, 0)
    if isinstance(raw, float) and raw == raw:
        return max(int(raw), 0)
    return 0


def merge_roll_live_state(
    live_leaf: Mapping[str, Any],
    *,
    add_count: int | None = None,
    entry_reference: float | None = None,
    extreme: float | None = None,
    total_qty: float | None = None,
) -> dict[str, Any]:
    out = dict(live_leaf)
    if add_count is not None:
        out["add_count"] = int(max(add_count, 0))
    if entry_reference is not None:
        out["entry_reference"] = float(entry_reference)
    if extreme is not None:
        out["extreme"] = float(extreme)
    if total_qty is not None:
        out["qty"] = float(total_qty)
    return out
