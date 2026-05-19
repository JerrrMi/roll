"""风险管理模块单元测试（纯计算）。"""

from __future__ import annotations

import pytest

from roll.risk import (
    AccountRiskMonitor,
    KellyVariant,
    RiskEngine,
    RiskLimits,
    adverse_fraction_from_prices,
    atr_stop_price,
    compute_position_quantity,
    effective_position_fraction,
    fixed_stop_price,
    floor_to_step,
    kelly_fraction,
    linear_pnl_usdt,
    notional_usdt,
    trailing_stop_price,
)

T0 = 1_700_000_000.0


def test_kelly_fraction_reference() -> None:
    p, b = 0.55, 1.1
    f = kelly_fraction(p, b)
    assert f == pytest.approx((p * (b + 1) - 1) / b)


def test_kelly_variants_scale() -> None:
    p, b = 0.6, 1.5
    raw = kelly_fraction(p, b)
    assert raw > 0
    lim = RiskLimits(max_position_fraction=1.0)
    assert effective_position_fraction(p, b, KellyVariant.FULL, lim.max_position_fraction) == pytest.approx(
        raw
    )
    assert effective_position_fraction(p, b, KellyVariant.HALF, lim.max_position_fraction) == pytest.approx(
        raw * 0.5
    )
    assert effective_position_fraction(
        p, b, KellyVariant.QUARTER, lim.max_position_fraction
    ) == pytest.approx(raw * 0.25)


def test_kelly_extra_multiplier_scales_effective_fraction() -> None:
    p, b = 0.6, 1.5
    raw = kelly_fraction(p, b)
    lim = RiskLimits(max_position_fraction=1.0, kelly_variant=KellyVariant.FULL, kelly_extra_multiplier=0.5)
    assert effective_position_fraction(
        p, b, lim.kelly_variant, lim.max_position_fraction, kelly_extra_multiplier=lim.kelly_extra_multiplier
    ) == pytest.approx(raw * 0.5)


def test_kelly_non_positive_parameters() -> None:
    assert kelly_fraction(0.5, -1.0) == 0.0
    assert kelly_fraction(0.0, 2.0) == 0.0
    assert kelly_fraction(1.0, 2.0) == 0.0


def test_effective_clamped_by_max_position() -> None:
    p, b = 0.99, 10.0
    raw = kelly_fraction(p, b)
    assert raw > 0.2
    eff = effective_position_fraction(p, b, KellyVariant.FULL, max_position_fraction=0.15)
    assert eff == pytest.approx(0.15)


def test_fixed_atr_trailing_stops() -> None:
    assert fixed_stop_price("long", 100.0, 0.02) == pytest.approx(98.0)
    assert fixed_stop_price("short", 100.0, 0.02) == pytest.approx(102.0)
    assert atr_stop_price("long", 100.0, 2.0, 1.5) == pytest.approx(97.0)
    assert atr_stop_price("short", 100.0, 2.0, 1.5) == pytest.approx(103.0)
    assert trailing_stop_price("long", 110.0, trail_fraction=0.1) == pytest.approx(99.0)
    assert trailing_stop_price("short", 90.0, trail_fraction=0.1) == pytest.approx(99.0)
    assert trailing_stop_price("long", 110.0, atr=2.0, k_atr=2.0) == pytest.approx(106.0)


def test_adverse_fraction() -> None:
    assert adverse_fraction_from_prices(100.0, 97.0, "long") == pytest.approx(0.03)


def test_position_size_loss_cap_vs_position_cap() -> None:
    limits = RiskLimits(
        max_single_loss_fraction=0.02,
        max_position_fraction=0.15,
        kelly_variant=KellyVariant.FULL,
    )
    entry = 100.0
    stop = 98.0  # 2% adverse
    p, b = 0.99, 50.0
    # Kelly 很大但被 max_position 截断
    res = compute_position_quantity(
        equity=10_000.0,
        entry_price=entry,
        stop_price=stop,
        side="long",
        p=p,
        b=b,
        limits=limits,
    )
    assert res.quantity > 0
    assert res.notional <= 10_000.0 * limits.max_position_fraction + 1e-6
    assert res.implied_loss_at_stop_fraction_of_equity <= limits.max_single_loss_fraction + 1e-9
    m = min(res.qty_from_loss_cap, res.qty_from_max_position, res.qty_from_kelly_notional)
    assert res.quantity == pytest.approx(m)


def test_risk_engine_kelly_blocks_negative() -> None:
    eng = RiskEngine(RiskLimits(kelly_variant=KellyVariant.FULL))
    ev = eng.evaluate_open(
        ts=T0,
        equity=10_000.0,
        entry_price=100.0,
        stop_price=99.0,
        side="long",
        p=0.4,
        b=1.0,
    )
    assert not ev.allow
    assert "kelly_non_positive" in ev.reasons


def test_risk_engine_drawdown_latch() -> None:
    limits = RiskLimits(max_drawdown_fraction=0.10, max_daily_loss_fraction=0.99)
    eng = RiskEngine(limits)
    eng.monitor.update_equity(T0, 100.0)
    eng.monitor.update_equity(T0 + 1, 89.0)  # 11% 从峰值 100
    ev = eng.evaluate_open(
        ts=T0 + 2,
        equity=95.0,
        entry_price=10.0,
        stop_price=9.5,
        side="long",
        p=0.6,
        b=1.5,
    )
    assert not ev.allow
    assert any("trading_halted:max_drawdown" in r for r in ev.reasons)


def test_daily_loss_resets_next_utc_day() -> None:
    limits = RiskLimits(max_drawdown_fraction=0.99, max_daily_loss_fraction=0.05)
    monitor = AccountRiskMonitor(limits)
    # 2023-11-14 12:00 UTC
    t1 = 1_699_957_200.0
    monitor.update_equity(t1, 1000.0)
    monitor.update_equity(t1 + 3600, 940.0)  # 6% 日内亏损
    assert monitor.state.halted_daily_loss
    gate = monitor.gate_open(t1 + 7200, 940.0)
    assert not gate.allowed

    # 次日 UTC
    t2 = t1 + 86400 * 3
    monitor.update_equity(t2, 900.0)
    assert not monitor.state.halted_daily_loss
    assert monitor.state.day_anchor_equity == pytest.approx(900.0)


def test_consecutive_loss_cooldown() -> None:
    limits = RiskLimits(
        max_drawdown_fraction=0.99,
        max_daily_loss_fraction=0.99,
        max_consecutive_losses=2,
        cooldown_seconds=100.0,
    )
    eng = RiskEngine(limits)
    eng.monitor.update_equity(T0, 10_000.0)
    eng.monitor.record_realized_pnl(T0 + 1, -1.0)
    eng.monitor.record_realized_pnl(T0 + 2, -1.0)
    assert eng.monitor.state.cooldown_until_ts == pytest.approx(T0 + 2 + 100.0)
    ev = eng.evaluate_open(
        ts=T0 + 50,
        equity=10_000.0,
        entry_price=100.0,
        stop_price=98.0,
        side="long",
        p=0.6,
        b=1.2,
    )
    assert not ev.allow
    assert any("cooldown" in r for r in ev.reasons)

    ev2 = eng.evaluate_open(
        ts=T0 + 200,
        equity=10_000.0,
        entry_price=100.0,
        stop_price=98.0,
        side="long",
        p=0.6,
        b=1.2,
    )
    assert ev2.allow


def test_quantity_step_floors_to_zero() -> None:
    limits = RiskLimits(kelly_variant=KellyVariant.FULL, max_position_fraction=0.5)
    res = compute_position_quantity(
        equity=100.0,
        entry_price=50.0,
        stop_price=49.0,
        side="long",
        p=0.55,
        b=1.1,
        limits=limits,
        quantity_step=10.0,
        min_quantity=0.01,
    )
    assert res.quantity == 0.0


def test_floor_to_step() -> None:
    assert floor_to_step(1.99, 0.5) == pytest.approx(1.5)


def test_never_exceed_max_position_notional() -> None:
    limits = RiskLimits(
        max_single_loss_fraction=0.5,
        max_position_fraction=0.12,
        kelly_variant=KellyVariant.FULL,
    )
    entry, stop = 1.0, 0.5
    equity = 50_000.0
    res = compute_position_quantity(
        equity=equity,
        entry_price=entry,
        stop_price=stop,
        side="long",
        p=0.7,
        b=2.0,
        limits=limits,
    )
    cap = equity * limits.max_position_fraction
    assert res.notional <= cap + 1e-5
    assert res.quantity * entry <= cap + 1e-5


def test_linear_pnl_usdt_helpers() -> None:
    assert linear_pnl_usdt("long", 2.0, 50.0, 55.0) == pytest.approx(10.0)
    assert linear_pnl_usdt("short", 2.0, 50.0, 45.0) == pytest.approx(10.0)
    assert notional_usdt(4.0, 12.5) == pytest.approx(50.0)


def test_risk_engine_available_margin_gate() -> None:
    eng = RiskEngine(RiskLimits(kelly_variant=KellyVariant.FULL, max_position_fraction=0.5))
    stop = fixed_stop_price("long", 100.0, 0.02)
    ev = eng.evaluate_open(
        ts=T0,
        equity=10_000.0,
        entry_price=100.0,
        stop_price=stop,
        side="long",
        p=0.6,
        b=1.5,
        available_margin_usdt=1.0,
        initial_leverage=25,
    )
    assert not ev.allow
    assert any("insufficient_available_margin" in r for r in ev.reasons)


def test_risk_limits_validation() -> None:
    with pytest.raises(ValueError):
        RiskLimits(max_single_loss_fraction=0.0)


def test_risk_engine_defaults_evaluate_open() -> None:
    eng = RiskEngine()
    stop = fixed_stop_price("long", 100.0, 0.02)
    ev = eng.evaluate_open(
        ts=T0,
        equity=10_000.0,
        entry_price=100.0,
        stop_price=stop,
        side="long",
        p=0.58,
        b=1.2,
    )
    assert ev.allow
    assert ev.quantity > 0
    assert ev.position is not None
    assert ev.position.implied_loss_at_stop_fraction_of_equity <= eng.limits.max_single_loss_fraction + 1e-9
