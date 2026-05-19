"""Binance USD-M（U 本位 USDT 线性永续）账户权益、名义价值与下单前校验。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from roll.binance_client import UsdMFuturesSymbol, format_floor_to_step_decimal_str
from roll.risk import Side, notional_usdt


@dataclass(frozen=True)
class UsdtAccountSnapshot:
    """从 USD-M account 响应解析的 USDT 风控快照。"""

    equity_usdt: float
    available_margin_usdt: float
    wallet_balance_usdt: float
    unrealized_profit_usdt: float


def _dec_field(row: Mapping[str, Any], *keys: str) -> Decimal:
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return Decimal(str(row[k]))
            except InvalidOperation:
                continue
    return Decimal(0)


def parse_usdt_account_snapshot(account: Mapping[str, Any]) -> UsdtAccountSnapshot:
    """
    从 GET /fapi/v1/account 或 /fapi/v2/account 解析 USDT 权益与可用保证金。

    权益（风控预算）::
        equity_usdt = USDT wallet balance + USDT unrealized profit
    不将 margin asset 币余额乘以标记价折算。

    优先使用账户级 total* 字段；否则回退 assets[] 中 USDT 行。
    """
    # v2 / 部分 v1 顶层字段
    top_wallet = _dec_field(
        account,
        "totalWalletBalance",
        "total_wallet_balance",
    )
    top_upnl = _dec_field(
        account,
        "totalUnrealizedProfit",
        "total_unrealized_profit",
        "totalCrossUnPnl",
    )
    top_avail = _dec_field(
        account,
        "availableBalance",
        "available_balance",
        "maxWithdrawAmount",
    )
    top_margin = _dec_field(account, "totalMarginBalance", "total_margin_balance")

    assets = account.get("assets")
    asset_rows = assets if isinstance(assets, list) else []

    usdt_wallet = Decimal(0)
    usdt_upnl = Decimal(0)
    usdt_avail = Decimal(0)
    found_usdt = False
    for row in asset_rows:
        if not isinstance(row, Mapping):
            continue
        ast = str(row.get("asset", "")).upper()
        if ast != "USDT":
            continue
        found_usdt = True
        usdt_wallet = _dec_field(row, "walletBalance", "wallet_balance")
        usdt_upnl = _dec_field(row, "unrealizedProfit", "unrealized_profit", "crossUnPnl")
        usdt_avail = _dec_field(
            row,
            "availableBalance",
            "available_balance",
            "maxWithdrawAmount",
            "crossWalletBalance",
        )
        break

    if top_wallet > 0 or top_upnl != 0 or top_margin > 0:
        wallet = top_wallet if top_wallet > 0 else usdt_wallet
        upnl = top_upnl if top_upnl != 0 or not found_usdt else usdt_upnl
        if top_margin > 0:
            equity = top_margin
        else:
            equity = wallet + upnl
        avail = top_avail if top_avail > 0 else usdt_avail
    elif found_usdt:
        wallet = usdt_wallet
        upnl = usdt_upnl
        equity = wallet + upnl
        avail = usdt_avail
    else:
        wallet = Decimal(0)
        upnl = Decimal(0)
        equity = Decimal(0)
        avail = Decimal(0)

    return UsdtAccountSnapshot(
        equity_usdt=float(equity),
        available_margin_usdt=float(avail),
        wallet_balance_usdt=float(wallet),
        unrealized_profit_usdt=float(upnl),
    )


def linear_pnl_usdt(
    side: Side,
    quantity: float,
    entry_price: float,
    exit_price: float,
) -> float:
    """USDT 线性永续盈亏：多头 qty*(exit-entry)，空头 qty*(entry-exit)。"""
    if quantity <= 0.0 or entry_price <= 0.0 or exit_price <= 0.0:
        return 0.0
    if side == "long":
        return quantity * (exit_price - entry_price)
    return quantity * (entry_price - exit_price)


def usdm_linear_contract_multiplier(_spec: UsdMFuturesSymbol | None = None) -> float:
    """USD-M USDT 线性永续：每 1 base 对应 1 USDT 名义/盈亏单位（contractSize 通常为 1）。"""
    if _spec is None:
        return 1.0
    cs = float(_spec.contract_size)
    if not math.isfinite(cs) or cs <= 0.0:
        return 1.0
    return cs


def market_lot_tuple(spec: UsdMFuturesSymbol) -> tuple[str, str, str]:
    """(tick_size, min_qty, step_size) — 市价单使用 MARKET_LOT_SIZE。"""
    return (
        spec.tick_size,
        spec.market_min_qty or spec.min_qty,
        spec.market_step_size or spec.step_size,
    )


def format_market_quantity_str(spec: UsdMFuturesSymbol, quantity: float) -> str:
    """按 MARKET_LOT_SIZE 步长向下取整为交易所 quantity 字符串。"""
    _, _, step = market_lot_tuple(spec)
    if not step:
        raise ValueError(f"{spec.symbol} 缺少 MARKET_LOT_SIZE step")
    return format_floor_to_step_decimal_str(str(quantity), step)


def parse_min_notional_usdt(spec: UsdMFuturesSymbol) -> float:
    raw = spec.min_notional
    if not raw or not str(raw).strip():
        return 0.0
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return 0.0


def estimate_initial_margin_usdt(notional: float, leverage: int) -> float:
    lev = max(int(leverage), 1)
    return notional / lev


@dataclass(frozen=True)
class UsdmOrderPrecheck:
    ok: bool
    reasons: tuple[str, ...]
    quantity: float
    quantity_str: str
    notional_usdt: float
    initial_margin_usdt: float


def precheck_usdm_market_open(
    *,
    spec: UsdMFuturesSymbol,
    quantity_raw: float,
    entry_price: float,
    equity_usdt: float,
    available_margin_usdt: float,
    limits_max_position_fraction: float,
    limits_max_single_loss_fraction: float,
    implied_loss_at_stop_fraction: float,
    initial_leverage: int,
) -> UsdmOrderPrecheck:
    """
    下单前校验：MARKET_LOT_SIZE、MIN_NOTIONAL、最大仓位/单笔风险、可用保证金。
    LOT_SIZE 步长与 min qty 应在 RiskEngine 数量计算阶段已应用；此处复核格式化后数量。
    """
    reasons: list[str] = []
    tick, min_q_s, step_s = market_lot_tuple(spec)
    _ = tick

    if quantity_raw <= 0.0 or not math.isfinite(quantity_raw):
        return UsdmOrderPrecheck(
            ok=False,
            reasons=("quantity_non_positive",),
            quantity=0.0,
            quantity_str="0",
            notional_usdt=0.0,
            initial_margin_usdt=0.0,
        )

    try:
        qty_s = format_market_quantity_str(spec, quantity_raw)
        qty = float(qty_s)
    except (ValueError, Exception) as e:
        return UsdmOrderPrecheck(
            ok=False,
            reasons=(f"quantity_format_error:{e}",),
            quantity=0.0,
            quantity_str="0",
            notional_usdt=0.0,
            initial_margin_usdt=0.0,
        )

    try:
        min_q = float(min_q_s) if min_q_s else 0.0
    except (TypeError, ValueError):
        min_q = 0.0
    if min_q > 0.0 and qty < min_q - 1e-12:
        reasons.append(f"below_market_min_qty:{qty}<{min_q}")

    try:
        step_f = float(step_s) if step_s else 0.0
    except (TypeError, ValueError):
        step_f = 0.0
    if step_f > 0.0:
        n_steps = qty / step_f
        if abs(n_steps - round(n_steps)) > 1e-6:
            reasons.append("quantity_not_on_market_step")

    ntl = notional_usdt(qty, entry_price)
    min_ntl = parse_min_notional_usdt(spec)
    if min_ntl > 0.0 and ntl + 1e-9 < min_ntl:
        reasons.append(f"below_min_notional:{ntl:.8g}<{min_ntl:.8g}")

    if equity_usdt > 0.0:
        pos_frac = ntl / equity_usdt
        if pos_frac > limits_max_position_fraction + 1e-9:
            reasons.append(
                f"exceeds_max_position_fraction:{pos_frac:.6f}>{limits_max_position_fraction:.6f}"
            )
        if implied_loss_at_stop_fraction > limits_max_single_loss_fraction + 1e-9:
            reasons.append(
                f"exceeds_max_single_loss_fraction:{implied_loss_at_stop_fraction:.6f}"
            )

    init_margin = estimate_initial_margin_usdt(ntl, initial_leverage)
    if available_margin_usdt > 0.0 and init_margin > available_margin_usdt + 1e-6:
        reasons.append(
            f"insufficient_available_margin:need≈{init_margin:.4f}>avail={available_margin_usdt:.4f}"
        )
    elif available_margin_usdt <= 0.0 and init_margin > 0.0:
        reasons.append("available_margin_non_positive")

    ok = len(reasons) == 0
    return UsdmOrderPrecheck(
        ok=ok,
        reasons=tuple(reasons),
        quantity=qty,
        quantity_str=qty_s,
        notional_usdt=ntl,
        initial_margin_usdt=init_margin,
    )
