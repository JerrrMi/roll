"""滚仓降杠杆分层规则测试。"""

from __future__ import annotations

from roll.position_roll import target_leverage_for_profit


def test_target_leverage_tiers_match_plan() -> None:
    assert target_leverage_for_profit(0.05, initial_leverage=25) == 25
    assert target_leverage_for_profit(0.10, initial_leverage=25) == 20
    assert target_leverage_for_profit(0.20, initial_leverage=25) == 15
    assert target_leverage_for_profit(0.35, initial_leverage=25) == 10
    assert target_leverage_for_profit(0.50, initial_leverage=25) == 5


def test_target_leverage_respects_initial_cap() -> None:
    assert target_leverage_for_profit(0.0, initial_leverage=10) == 10
    assert target_leverage_for_profit(0.50, initial_leverage=10) == 5
