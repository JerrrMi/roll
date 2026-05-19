"""USD-M 账户权益、线性 PnL 与下单前校验。"""

from __future__ import annotations

import pytest

from roll.binance_client import UsdMFuturesSymbol
from roll.risk import KellyVariant, RiskEngine, RiskLimits, linear_pnl_usdt, notional_usdt
from roll.usdm_account import (
    estimate_initial_margin_usdt,
    format_market_quantity_str,
    parse_usdt_account_snapshot,
    precheck_usdm_market_open,
)


def _spec(**kw) -> UsdMFuturesSymbol:
    base = dict(
        symbol="DOGEUSDT",
        pair="DOGEUSDT",
        base_asset="DOGE",
        quote_asset="USDT",
        status="TRADING",
        contract_type="PERPETUAL",
        margin_asset="USDT",
        price_precision=4,
        quantity_precision=0,
        contract_size=1.0,
        tick_size="0.0001",
        step_size="1",
        min_qty="1",
        market_min_qty="1",
        market_step_size="1",
        min_notional="5",
        filters_raw=(),
    )
    base.update(kw)
    return UsdMFuturesSymbol(**base)


def test_parse_usdt_account_v2_top_level() -> None:
    acct = {
        "totalWalletBalance": "1000.50",
        "totalUnrealizedProfit": "12.25",
        "availableBalance": "800.00",
    }
    snap = parse_usdt_account_snapshot(acct)
    assert snap.wallet_balance_usdt == pytest.approx(1000.50)
    assert snap.unrealized_profit_usdt == pytest.approx(12.25)
    assert snap.equity_usdt == pytest.approx(1012.75)
    assert snap.available_margin_usdt == pytest.approx(800.0)


def test_parse_usdt_account_assets_fallback() -> None:
    acct = {
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "500",
                "unrealizedProfit": "-10",
                "availableBalance": "200",
            }
        ]
    }
    snap = parse_usdt_account_snapshot(acct)
    assert snap.equity_usdt == pytest.approx(490.0)
    assert snap.available_margin_usdt == pytest.approx(200.0)


def test_linear_pnl_and_notional() -> None:
    assert linear_pnl_usdt("long", 10.0, 100.0, 105.0) == pytest.approx(50.0)
    assert linear_pnl_usdt("short", 10.0, 100.0, 95.0) == pytest.approx(50.0)
    assert notional_usdt(3.0, 2.5) == pytest.approx(7.5)
    assert notional_usdt(-3.0, 2.5) == pytest.approx(7.5)


def test_precheck_rejects_below_min_notional() -> None:
    sp = _spec(min_notional="100")
    pre = precheck_usdm_market_open(
        spec=sp,
        quantity_raw=1.0,
        entry_price=10.0,
        equity_usdt=10_000.0,
        available_margin_usdt=5_000.0,
        limits_max_position_fraction=0.15,
        limits_max_single_loss_fraction=0.02,
        implied_loss_at_stop_fraction=0.01,
        initial_leverage=25,
    )
    assert not pre.ok
    assert any("below_min_notional" in r for r in pre.reasons)


def test_precheck_rejects_insufficient_margin() -> None:
    sp = _spec(min_notional="5")
    pre = precheck_usdm_market_open(
        spec=sp,
        quantity_raw=100.0,
        entry_price=100.0,
        equity_usdt=50_000.0,
        available_margin_usdt=10.0,
        limits_max_position_fraction=0.5,
        limits_max_single_loss_fraction=0.5,
        implied_loss_at_stop_fraction=0.01,
        initial_leverage=25,
    )
    assert not pre.ok
    assert any("insufficient_available_margin" in r for r in pre.reasons)


def test_risk_engine_min_notional_gate() -> None:
    eng = RiskEngine(RiskLimits(kelly_variant=KellyVariant.FULL))
    stop = 98.0
    ev = eng.evaluate_open(
        ts=1_700_000_000.0,
        equity=10_000.0,
        entry_price=100.0,
        stop_price=stop,
        side="long",
        p=0.6,
        b=1.5,
        min_notional_usdt=1_000_000.0,
    )
    assert not ev.allow
    assert any("below_min_notional" in r for r in ev.reasons)


def test_format_market_quantity_respects_step() -> None:
    sp = _spec(market_step_size="0.1", market_min_qty="0.1")
    s = format_market_quantity_str(sp, 1.27)
    assert s == "1.2"


def test_estimate_initial_margin() -> None:
    assert estimate_initial_margin_usdt(1000.0, 25) == pytest.approx(40.0)
