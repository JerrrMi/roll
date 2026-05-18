"""全局单标的交易锁与持仓视图（骨架）。"""

from __future__ import annotations

from enum import Enum


class TradeLockState(str, Enum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    IN_POSITION = "IN_POSITION"
    EXITING = "EXITING"
    COOLDOWN = "COOLDOWN"


class PositionManager:
    """维护交易锁状态；后续与交易所持仓查询对齐。"""

    def __init__(self) -> None:
        self._lock = TradeLockState.IDLE
        self._active_symbol: str | None = None

    @property
    def lock_state(self) -> TradeLockState:
        return self._lock

    @property
    def active_symbol(self) -> str | None:
        return self._active_symbol

    def transition(self, new_state: TradeLockState, symbol: str | None = None) -> None:
        """骨架占位：不做合法性校验。"""
        self._lock = new_state
        self._active_symbol = symbol
