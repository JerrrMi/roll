"""单标的锁与交易所快照对账（纯内存；无 REST）。"""
from __future__ import annotations

import pytest

from roll.position_manager import (
    PositionManager,
    TradeLockState,
    TransitionError,
    reconcile_coin_m_account,
)


def test_reconcile_idle_flat() -> None:
    poses = [{"symbol": "DOGEUSD_PERP", "positionAmt": "0"}]
    orders: list[dict] = []
    out = reconcile_coin_m_account(poses, orders)
    assert out.lock_state is TradeLockState.IDLE
    assert out.active_symbol is None
    assert not out.halt_automatic_trading


def test_reconcile_multi_position_halts() -> None:
    poses = [
        {"symbol": "DOGEUSD_PERP", "positionAmt": "1"},
        {"symbol": "TONUSD_PERP", "positionAmt": "-2"},
    ]
    out = reconcile_coin_m_account(poses, [])
    assert out.halt_automatic_trading
    assert {"DOGEUSD_PERP", "TONUSD_PERP"} == set(out.position_symbols)


def test_reconcile_orders_two_symbols_halts() -> None:
    orders = [
        {"symbol": "AUSD_PERP", "reduceOnly": False},
        {"symbol": "BUSD_PERP", "reduceOnly": True},
    ]
    out = reconcile_coin_m_account([], orders)
    assert out.halt_automatic_trading


def test_reconcile_position_plus_stray_order_halts() -> None:
    poses = [{"symbol": "DOGEUSD_PERP", "positionAmt": "1"}]
    orders = [{"symbol": "TONUSD_PERP", "reduceOnly": False}]
    out = reconcile_coin_m_account(poses, orders)
    assert out.halt_automatic_trading


def test_reconcile_orders_only_entering_infer() -> None:
    orders = [{"symbol": "DOGEUSD_PERP", "reduceOnly": False}]
    out = reconcile_coin_m_account([], orders)
    assert out.lock_state is TradeLockState.ENTERING
    assert out.active_symbol == "DOGEUSD_PERP"
    assert not out.halt_automatic_trading


def test_full_enter_exit_transition_line() -> None:
    mgr = PositionManager()
    mgr.begin_enter("dogeusd_perp")
    assert mgr.lock_state is TradeLockState.ENTERING
    mgr.confirm_in_position("dogeusd_perp")
    assert mgr.lock_state is TradeLockState.IN_POSITION
    mgr.begin_exit("DOGEUSD_PERP")
    mgr.mark_exit_finished_to_cooldown("DOGEUSD_PERP")
    mgr.finish_cooldown_to_idle()
    assert mgr.lock_state is TradeLockState.IDLE
    assert mgr.active_symbol is None


def test_cannot_steal_other_symbol_mid_session() -> None:
    mgr = PositionManager()
    mgr.begin_enter("AAAUSD_PERP")
    mgr.confirm_in_position("AAAUSD_PERP")
    with pytest.raises(TransitionError):
        mgr.begin_exit("BBBUSD_PERP")


def test_halt_blocks_transitions() -> None:
    mgr = PositionManager()
    mgr.set_halt_for_manual_review("simulate multi-book")
    with pytest.raises(TransitionError):
        mgr.begin_enter("DOG")
