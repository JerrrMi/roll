"""趋势模型骨架测试。"""

from roll.trend_model import SignalSide, TrendModel


def test_trend_model_default_is_no_trade() -> None:
    m = TrendModel()
    sig = m.evaluate()
    assert sig.side == SignalSide.NO_TRADE
    assert "skeleton_stub" in sig.reasons
