"""风控骨架测试。"""

from roll.risk import RiskEngine, RiskLimits


def test_risk_engine_defaults() -> None:
    engine = RiskEngine()
    assert engine.limits.max_single_loss_fraction == 0.02
    assert engine.can_open() is False
