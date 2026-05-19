"""策略循环：排序与参数解析（纯函数，无网络）。"""

from __future__ import annotations

from roll.strategy_loop import parse_strategy_loop_params, rank_directional_signals
from roll.trend_model import SignalSide, TrendSignal


def _sig(side: SignalSide, score: float) -> TrendSignal:
    return TrendSignal(
        side=side,
        score=score,
        score_by_interval={},
        timeframe_assessments=(),
        reasons=(),
        rejection_reasons=(),
    )


def test_parse_strategy_loop_public_rest_base() -> None:
    p = parse_strategy_loop_params({"strategy": {"public_rest_base": " https://fapi.binance.com "}})
    assert p.public_rest_base == "https://fapi.binance.com"


def test_rank_directional_signals_orders_by_abs_score() -> None:
    assessed = [
        ("AAAUSD_PERP", _sig(SignalSide.LONG, 0.5)),
        ("BBBUSD_PERP", _sig(SignalSide.SHORT, -0.9)),
        ("CCCUSD_PERP", _sig(SignalSide.NO_TRADE, 0.1)),
    ]
    ranked = rank_directional_signals(assessed)
    assert [s for s, _ in ranked] == ["BBBUSD_PERP", "AAAUSD_PERP"]


def test_rank_directional_signals_excludes_no_trade() -> None:
    assessed = [
        ("X", _sig(SignalSide.NO_TRADE, 0.99)),
    ]
    assert rank_directional_signals(assessed) == []


def test_parse_strategy_loop_defaults() -> None:
    p = parse_strategy_loop_params({})
    assert p.min_monitor_symbols == 3
    assert p.loop_interval_sec == 60.0
    assert p.testnet_signed_orders_enabled is False
    assert p.live_trading_enabled is False


def test_parse_strategy_loop_safety_flags() -> None:
    p = parse_strategy_loop_params(
        {"strategy": {"testnet_signed_orders_enabled": True, "live_trading_enabled": True}},
    )
    assert p.testnet_signed_orders_enabled is True
    assert p.live_trading_enabled is True


def test_parse_strategy_loop_from_yaml_blob() -> None:
    p = parse_strategy_loop_params(
        {
            "strategy": {
                "loop_interval_sec": 30,
                "klines_limit": 400,
                "min_monitor_symbols": 5,
                "dry_run_equity": 5000,
                "initial_leverage": 10,
                "stop_adverse_fraction": 0.03,
                "kelly_p": 0.6,
                "kelly_b": 2.0,
            },
        },
    )
    assert p.loop_interval_sec == 30.0
    assert p.klines_limit == 400
    assert p.min_monitor_symbols == 5
    assert p.dry_run_equity == 5000.0
    assert p.initial_leverage == 10
    assert p.stop_adverse_fraction == 0.03
    assert p.kelly_p == 0.6
    assert p.kelly_b == 2.0


def test_parse_strategy_loop_trail_fraction() -> None:
    p = parse_strategy_loop_params({"strategy": {"trail_stop_fraction": 0.02}})
    assert p.trail_stop_fraction == 0.02
    q = parse_strategy_loop_params({"strategy": {"trail_stop_fraction": -1}})
    assert q.trail_stop_fraction is None
