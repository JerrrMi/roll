"""滚仓（浮盈加仓）规则单元测试。"""

from __future__ import annotations

from roll.position_roll import has_float_profit, parse_add_count, trend_allows_add
from roll.trend_model import SignalSide, TrendModelParams, TrendSignal


def _sig(side: SignalSide, score: float) -> TrendSignal:
    return TrendSignal(
        side=side,
        score=score,
        score_by_interval={},
        timeframe_assessments=(),
        reasons=(),
        rejection_reasons=(),
    )


def test_trend_allows_add_long_strong() -> None:
    tpar = TrendModelParams(long_threshold=0.7, short_threshold=0.7)
    assert trend_allows_add(
        holding="long",
        sig=_sig(SignalSide.LONG, 0.75),
        tparams=tpar,
    )


def test_trend_allows_add_rejects_weak_long() -> None:
    tpar = TrendModelParams(long_threshold=0.7, short_threshold=0.7)
    assert not trend_allows_add(
        holding="long",
        sig=_sig(SignalSide.LONG, 0.65),
        tparams=tpar,
    )


def test_has_float_profit_long() -> None:
    assert has_float_profit(side="long", quantity=10.0, avg_entry=100.0, mark=105.0)
    assert not has_float_profit(side="long", quantity=10.0, avg_entry=100.0, mark=99.0)


def test_parse_add_count() -> None:
    assert parse_add_count({}) == 0
    assert parse_add_count({"add_count": 2}) == 2
