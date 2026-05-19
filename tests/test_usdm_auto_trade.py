"""USD-M 自动交易闭环纯函数测试（无网络）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from roll.binance_client import UsdMFuturesSymbol
from roll.strategy_loop import StrategyLoopParams, parse_strategy_loop_params
from roll.trend_model import SignalSide, TrendModelParams, TrendSignal
from roll.usdm_auto_trade import (
    log_peer_symbol_signals_while_in_position,
    resolve_open_quantity_raw,
    should_exit_from_trend,
)


def _spec(symbol: str = "DOGEUSDT", *, min_q: str = "1") -> UsdMFuturesSymbol:
    base = symbol[:4] if len(symbol) > 4 else "DOGE"
    return UsdMFuturesSymbol(
        symbol=symbol,
        pair=symbol,
        base_asset=base.replace("USDT", "") or "DOGE",
        quote_asset="USDT",
        status="TRADING",
        contract_type="PERPETUAL",
        margin_asset="USDT",
        price_precision=5,
        quantity_precision=0,
        contract_size=1.0,
        tick_size="0.00001",
        step_size="1",
        min_qty="1",
        market_min_qty=min_q,
        market_step_size="1",
        min_notional="5",
        filters_raw=(),
    )


def test_resolve_open_quantity_min_market() -> None:
    sp = _spec(min_q="42")
    assert resolve_open_quantity_raw(spec=sp, risk_quantity=100.0, mode="min_market") == 42.0
    assert resolve_open_quantity_raw(spec=sp, risk_quantity=100.0, mode="risk") == 100.0


def test_parse_open_quantity_mode() -> None:
    p = parse_strategy_loop_params({"strategy": {"open_quantity_mode": "min_market"}})
    assert p.open_quantity_mode == "min_market"
    q = parse_strategy_loop_params({"strategy": {"open_quantity_mode": "invalid"}})
    assert q.open_quantity_mode == "risk"


def test_should_exit_from_trend_long_to_short() -> None:
    tpar = TrendModelParams(long_threshold=0.5, short_threshold=0.5)
    sig = TrendSignal(
        side=SignalSide.SHORT,
        score=-0.6,
        score_by_interval={},
        timeframe_assessments=(),
        reasons=(),
        rejection_reasons=(),
    )
    assert should_exit_from_trend(holding="long", sig=sig, tparams=tpar)


def test_log_peer_signals_skips_active_and_emits() -> None:
    lines: list[str] = []
    client = MagicMock()
    client.config.rest_base = "https://testnet.binancefuture.com"
    client.config.api_prefix = "/fapi/v1"

    from roll import usdm_auto_trade as mod

    def fake_eval(sym: str, **kwargs: object) -> TrendSignal:
        _ = kwargs
        side = SignalSide.LONG if sym == "AVAXUSDT" else SignalSide.NO_TRADE
        return TrendSignal(
            side=side,
            score=0.7 if sym == "AVAXUSDT" else 0.0,
            score_by_interval={},
            timeframe_assessments=(),
            reasons=(),
            rejection_reasons=(),
        )

    orig = mod.evaluate_symbol_offline_public
    mod.evaluate_symbol_offline_public = fake_eval  # type: ignore[method-assign]
    try:
        log_peer_symbol_signals_while_in_position(
            signed_client=client,
            active_symbol="DOGEUSDT",
            matched=[_spec("DOGEUSDT"), _spec("AVAXUSDT", min_q="1")],
            params=StrategyLoopParams(),
            intervals=("15m", "1h", "4h"),
            trend_params=None,
            emit=lines.append,
        )
    finally:
        mod.evaluate_symbol_offline_public = orig  # type: ignore[method-assign]

    peer_lines = [ln for ln in lines if "[live][signal_only] symbol=" in ln]
    assert any("AVAXUSDT" in ln and "no_order" in ln for ln in peer_lines)
    assert not any("DOGEUSDT" in ln and "symbol=DOGEUSDT" in ln for ln in peer_lines)
