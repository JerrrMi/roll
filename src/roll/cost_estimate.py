"""MARKET 手续费与滑点估算（回测与 live 日志共用，不参与真实下单）。"""

from __future__ import annotations

from roll.risk import Side


def apply_slippage_entry(side: Side, px: float, slip_bps: float) -> float:
    s = slip_bps / 10_000.0
    if side == "long":
        return px * (1.0 + s)
    return px * (1.0 - s)


def apply_slippage_exit(side: Side, px: float, slip_bps: float) -> float:
    s = slip_bps / 10_000.0
    if side == "long":
        return px * (1.0 - s)
    return px * (1.0 + s)


def estimate_taker_fee_usdt(*, quantity: float, price: float, fee_rate: float) -> float:
    return abs(quantity) * abs(price) * fee_rate


def format_live_cost_est(
    *,
    intent: str,
    side: Side,
    quantity: float,
    mark_px: float,
    fee_bps: float,
    slippage_bps: float,
    is_exit: bool = False,
) -> str:
    fee_rate = fee_bps / 10_000.0
    fee = estimate_taker_fee_usdt(quantity=quantity, price=mark_px, fee_rate=fee_rate)
    if is_exit:
        eff_px = apply_slippage_exit(side, mark_px, slippage_bps)
    else:
        eff_px = apply_slippage_entry(side, mark_px, slippage_bps)
    slip_delta = abs(eff_px - mark_px)
    notional = abs(quantity) * mark_px
    return (
        f"[live][cost_est] {intent} side={side} qty≈{quantity:.8g} mark≈{mark_px:.8g} "
        f"fee≈{fee:.4f}USDT slip_px≈{slip_delta:.8g} eff_px≈{eff_px:.8g} "
        f"notional≈{notional:.4f}USDT (fee_bps={fee_bps} slip_bps={slippage_bps})"
    )
