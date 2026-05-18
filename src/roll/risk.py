"""Kelly 风险预算、止损价位、仓位数量与账户级熔断（纯计算，不接下单）。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

Side = Literal["long", "short"]


class KellyVariant(Enum):
    """对完整 Kelly 分数的缩放：全、半、四分之一。"""

    FULL = 1.0
    HALF = 0.5
    QUARTER = 0.25

    @property
    def multiplier(self) -> float:
        return float(self.value)


def kelly_fraction(p: float, b: float) -> float:
    """
    经典二元结果 Kelly：f* = (p * (b + 1) - 1) / b。
    p 为胜率（0~1），b 为“盈/亏幅度比”（平均盈利 / 平均亏损）。
    b <= 0 或 p 不在 (0,1) 时返回 0（视为不可用这个公式 sizing）。
    """
    if not (0.0 < p < 1.0):
        return 0.0
    if b <= 0.0 or not math.isfinite(b):
        return 0.0
    return (p * (b + 1.0) - 1.0) / b


def effective_position_fraction(
    p: float,
    b: float,
    variant: KellyVariant,
    max_position_fraction: float,
) -> float:
    """
    按计划文档：effective = clamp(kelly_full * multiplier, 0, max_position_fraction)。
    """
    if max_position_fraction <= 0.0:
        return 0.0
    raw = kelly_fraction(p, b)
    scaled = raw * variant.multiplier
    return max(0.0, min(scaled, max_position_fraction))


def floor_to_step(value: float, step: float) -> float:
    """将数量向下取整到 step（step<=0 时原样返回）。"""
    if step <= 0.0 or not math.isfinite(step):
        return value
    # 避免浮点边界抖动
    return math.floor(value / step + 1e-12) * step


def fixed_stop_price(side: Side, entry: float, adverse_fraction: float) -> float:
    """
    固定比例止损：价格向不利方向移动 adverse_fraction（相对入场价的分数）。
    多头止损在下方：entry * (1 - adverse_fraction)；空头在上方：entry * (1 + adverse_fraction)。
    """
    if entry <= 0.0:
        raise ValueError("entry 必须为正")
    if adverse_fraction <= 0.0:
        raise ValueError("adverse_fraction 必须为正")
    if side == "long":
        return entry * (1.0 - adverse_fraction)
    return entry * (1.0 + adverse_fraction)


def atr_stop_price(side: Side, entry: float, atr: float, k: float) -> float:
    """ATR 止损：多头 entry - k*ATR，空头 entry + k*ATR。"""
    if entry <= 0.0:
        raise ValueError("entry 必须为正")
    if atr < 0.0 or k < 0.0:
        raise ValueError("atr、k 必须非负")
    offset = k * atr
    if side == "long":
        return entry - offset
    return entry + offset


def trailing_stop_price(
    side: Side,
    extreme_price: float,
    *,
    trail_fraction: float | None = None,
    atr: float | None = None,
    k_atr: float | None = None,
) -> float:
    """
    追踪止损参考价（基于开仓以来最有利极值价）。
    - 仅比例：多头用最高价，止损 = extreme * (1 - trail_fraction)；空头用最低价，止损 = extreme * (1 + trail_fraction)。
    - 仅 ATR：多头 extreme - k_atr*atr，空头 extreme + k_atr*atr。
    二者互斥，必须且只能提供一种。
    """
    if extreme_price <= 0.0:
        raise ValueError("extreme_price 必须为正")
    use_frac = trail_fraction is not None
    use_atr = atr is not None and k_atr is not None
    if use_frac == use_atr:
        raise ValueError("需指定 trail_fraction，或同时指定 atr 与 k_atr，且不能混用")
    if use_frac:
        if trail_fraction <= 0.0 or trail_fraction >= 1.0:
            raise ValueError("trail_fraction 应在 (0,1)")
        if side == "long":
            return extreme_price * (1.0 - trail_fraction)
        return extreme_price * (1.0 + trail_fraction)
    if atr < 0.0 or k_atr < 0.0:
        raise ValueError("atr、k_atr 必须非负")
    offset = k_atr * atr
    if side == "long":
        return extreme_price - offset
    return extreme_price + offset


def adverse_price_distance(entry: float, stop: float, side: Side) -> float:
    """单位标的（报价币）的不利变动幅度，恒为非负。"""
    if side == "long":
        if stop >= entry:
            raise ValueError("多头止损价必须低于入场价")
        return entry - stop
    if stop <= entry:
        raise ValueError("空头止损价必须高于入场价")
    return stop - entry


def adverse_fraction_from_prices(entry: float, stop: float, side: Side) -> float:
    """不利方向价格距离占入场价的比例。"""
    d = adverse_price_distance(entry, stop, side)
    return d / entry


@dataclass(frozen=True)
class RiskLimits:
    """与计划文档一致的风控参数；可与后续 YAML 配置对接。"""

    max_single_loss_fraction: float = 0.02
    max_position_fraction: float = 0.15
    kelly_variant: KellyVariant = KellyVariant.HALF
    max_drawdown_fraction: float = 0.15
    max_daily_loss_fraction: float = 0.05
    max_consecutive_losses: int = 3
    cooldown_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_single_loss_fraction <= 0.0:
            raise ValueError("max_single_loss_fraction 必须为正")
        if self.max_position_fraction <= 0.0:
            raise ValueError("max_position_fraction 必须为正")
        if self.max_drawdown_fraction <= 0.0 or self.max_daily_loss_fraction <= 0.0:
            raise ValueError("回撤/日内亏损阈值必须为正")
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses 至少为 1")
        if self.cooldown_seconds <= 0.0:
            raise ValueError("cooldown_seconds 必须为正")


@dataclass(frozen=True)
class PositionSizeResult:
    """下单数量纯计算结果（contract_multiplier=1 时每单位价格变动对应报价币盈亏与数量线性近似）。"""

    quantity: float
    effective_kelly_fraction: float
    qty_from_kelly_notional: float
    qty_from_loss_cap: float
    qty_from_max_position: float
    notional: float
    implied_loss_at_stop_fraction_of_equity: float


def compute_position_quantity(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    side: Side,
    p: float,
    b: float,
    limits: RiskLimits,
    contract_multiplier: float = 1.0,
    quantity_step: float = 0.0,
    min_quantity: float = 0.0,
) -> PositionSizeResult:
    """
    根据权益、Kelly 有效仓位上限、最大单笔亏损、最大仓位比例与止损价反推数量。
    亏损约束：quantity * adverse_distance * contract_multiplier <= equity * max_single_loss_fraction
    名义约束：quantity * entry_price * contract_multiplier <= equity * max(effective_kelly, ...) 与 max_position
    """
    if equity <= 0.0 or entry_price <= 0.0:
        raise ValueError("equity、entry_price 必须为正")
    if contract_multiplier <= 0.0:
        raise ValueError("contract_multiplier 必须为正")

    adv = adverse_price_distance(entry_price, stop_price, side)
    eff = effective_position_fraction(
        p, b, limits.kelly_variant, limits.max_position_fraction
    )

    loss_budget = equity * limits.max_single_loss_fraction
    kelly_notional_cap = equity * eff
    max_pos_notional_cap = equity * limits.max_position_fraction

    loss_per_unit = adv * contract_multiplier
    if loss_per_unit <= 0.0:
        raise ValueError("止损与入场重合，无法计算数量")

    qty_loss = loss_budget / loss_per_unit
    qty_kelly = kelly_notional_cap / (entry_price * contract_multiplier)
    qty_max_pos = max_pos_notional_cap / (entry_price * contract_multiplier)

    qty_raw = min(qty_loss, qty_kelly, qty_max_pos)
    if quantity_step > 0.0:
        qty_raw = floor_to_step(qty_raw, quantity_step)
    if qty_raw < min_quantity:
        qty_raw = 0.0

    notional = qty_raw * entry_price * contract_multiplier
    implied_loss = qty_raw * loss_per_unit
    implied_frac = implied_loss / equity if equity > 0.0 else 0.0

    return PositionSizeResult(
        quantity=qty_raw,
        effective_kelly_fraction=eff,
        qty_from_kelly_notional=qty_kelly,
        qty_from_loss_cap=qty_loss,
        qty_from_max_position=qty_max_pos,
        notional=notional,
        implied_loss_at_stop_fraction_of_equity=implied_frac,
    )


@dataclass
class AccountRiskState:
    """账户熔断用可变状态（回测/仿真时逐步喂 equity 与平仓盈亏）。"""

    peak_equity: float | None = None
    day_anchor_equity: float | None = None
    day_anchor_date: str | None = None
    consecutive_losses: int = 0
    cooldown_until_ts: float | None = None
    halted_max_drawdown: bool = False
    halted_daily_loss: bool = False


def _utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


@dataclass
class CircuitGateResult:
    allowed: bool
    block_reasons: list[str] = field(default_factory=list)


class AccountRiskMonitor:
    """
    账户级：最大回撤、日内亏损、连续亏损 + 冷却期。
    使用前对每个决策点调用 update_equity；平仓后调用 record_realized_pnl。
    """

    def __init__(self, limits: RiskLimits, state: AccountRiskState | None = None) -> None:
        self._limits = limits
        self.state = state or AccountRiskState()

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def update_equity(self, ts: float, equity: float) -> CircuitGateResult:
        """更新峰值、日内锚点，并检测回撤/日内熔断（一旦触发则闩锁halted标记）。"""
        reasons: list[str] = []
        if equity <= 0.0 or not math.isfinite(equity):
            return CircuitGateResult(False, ["invalid_or_nonpositive_equity"])

        if self.state.peak_equity is None:
            self.state.peak_equity = equity
        else:
            self.state.peak_equity = max(self.state.peak_equity, equity)

        day = _utc_date(ts)
        if self.state.day_anchor_date != day:
            self.state.day_anchor_date = day
            self.state.day_anchor_equity = equity
            self.state.halted_daily_loss = False

        peak = self.state.peak_equity
        if peak and peak > 0.0:
            dd = (peak - equity) / peak
            if dd >= self._limits.max_drawdown_fraction:
                self.state.halted_max_drawdown = True
                reasons.append(
                    f"max_drawdown: {(dd * 100):.2f}% >= {(self._limits.max_drawdown_fraction * 100):.2f}%"
                )

        anchor = self.state.day_anchor_equity
        if anchor and anchor > 0.0 and not self.state.halted_daily_loss:
            day_loss = (anchor - equity) / anchor
            if day_loss >= self._limits.max_daily_loss_fraction:
                self.state.halted_daily_loss = True
                reasons.append(
                    f"daily_loss: {(day_loss * 100):.2f}% >= {(self._limits.max_daily_loss_fraction * 100):.2f}%"
                )

        allowed = not self.state.halted_max_drawdown and not self.state.halted_daily_loss
        return CircuitGateResult(allowed and not reasons, reasons)

    def record_realized_pnl(self, ts: float, realized_pnl: float) -> None:
        """统计连续亏损并在超限时启动冷却期。"""
        if realized_pnl < 0.0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        if self.state.consecutive_losses >= self._limits.max_consecutive_losses:
            self.state.cooldown_until_ts = ts + self._limits.cooldown_seconds
            self.state.consecutive_losses = 0

    def gate_open(self, ts: float, _equity: float) -> CircuitGateResult:
        """综合冷却期与闩锁状态，判断是否允许开新仓。"""
        reasons: list[str] = []
        if self.state.halted_max_drawdown:
            reasons.append("trading_halted:max_drawdown_latched")
        if self.state.halted_daily_loss:
            reasons.append("trading_halted:daily_loss_latched")

        until = self.state.cooldown_until_ts
        if until is not None and ts < until:
            reasons.append(f"cooldown:active_until_ts={until:.3f}")

        return CircuitGateResult(len(reasons) == 0, reasons)

    def clear_cooldown_if_expired(self, ts: float) -> None:
        if self.state.cooldown_until_ts is not None and ts >= self.state.cooldown_until_ts:
            self.state.cooldown_until_ts = None


@dataclass(frozen=True)
class OpenEvaluation:
    """一次开仓评估：是否允许、原因、建议数量与Kelly有效比例。"""

    allow: bool
    reasons: tuple[str, ...]
    quantity: float
    effective_kelly_fraction: float
    position: PositionSizeResult | None
    raw_kelly_fraction: float


class RiskEngine:
    """组合 Kelly 门槛、仓位计算与账户熔断。"""

    def __init__(
        self,
        limits: RiskLimits | None = None,
        monitor: AccountRiskMonitor | None = None,
    ) -> None:
        lim = limits or RiskLimits()
        self._limits = lim
        self._monitor = monitor or AccountRiskMonitor(lim)

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    @property
    def monitor(self) -> AccountRiskMonitor:
        return self._monitor

    def evaluate_open(
        self,
        *,
        ts: float,
        equity: float,
        entry_price: float,
        stop_price: float,
        side: Side,
        p: float,
        b: float,
        quantity_step: float = 0.0,
        min_quantity: float = 0.0,
        contract_multiplier: float = 1.0,
    ) -> OpenEvaluation:
        reasons: list[str] = []

        raw_k = kelly_fraction(p, b)
        if raw_k <= 0.0:
            reasons.append("kelly_non_positive")

        circ_update = self._monitor.update_equity(ts, equity)
        if not circ_update.allowed:
            reasons.extend(circ_update.block_reasons)

        self._monitor.clear_cooldown_if_expired(ts)
        circ_gate = self._monitor.gate_open(ts, equity)
        if not circ_gate.allowed:
            reasons.extend(circ_gate.block_reasons)

        pos: PositionSizeResult | None = None
        qty = 0.0

        if not reasons:
            try:
                pos = compute_position_quantity(
                    equity=equity,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    side=side,
                    p=p,
                    b=b,
                    limits=self._limits,
                    contract_multiplier=contract_multiplier,
                    quantity_step=quantity_step,
                    min_quantity=min_quantity,
                )
                qty = pos.quantity
                if qty <= 0.0:
                    reasons.append("quantity_zero_after_caps_or_step")
                else:
                    # 数值安全：确保隐含亏损不超过单笔上限（含浮点误差）
                    if (
                        pos.implied_loss_at_stop_fraction_of_equity
                        > self._limits.max_single_loss_fraction + 1e-9
                    ):
                        reasons.append("implied_loss_exceeds_cap_rounding")
                        qty = 0.0
                        pos = None
            except ValueError as e:
                reasons.append(f"sizing_error:{e}")

        eff = effective_position_fraction(
            p, b, self._limits.kelly_variant, self._limits.max_position_fraction
        )

        allow = len(reasons) == 0 and qty > 0.0
        return OpenEvaluation(
            allow=allow,
            reasons=tuple(dict.fromkeys(reasons)),
            quantity=qty if allow else 0.0,
            effective_kelly_fraction=eff,
            position=pos if pos and allow else None,
            raw_kelly_fraction=raw_k,
        )

