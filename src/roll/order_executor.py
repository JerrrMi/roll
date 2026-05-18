"""下单、撤单、平仓与订单确认（骨架）。

实盘/ Testnet 执行层在发单前应调用注入的 ``PositionManager``：
``begin_enter(symbol) → confirm_in_position`` / ``begin_exit``
等（见 ``roll.position_manager``），并以 ``restore_from_exchange`` 与 Testnet REST 快照对齐，
避免本地状态漂移导致双标的并行下单。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from roll.position_manager import PositionManager


class OrderExecutor:
    """订单执行器占位；后续接入 signed API 与最小数量验收。"""

    def __init__(self, position_manager: PositionManager | None = None, **_kwargs: Any) -> None:
        self._pm = position_manager

    def _guard_symbol(self, symbol: str, *, intent: str) -> None:
        if self._pm is None:
            return
        self._pm.assert_single_focus_or_raise(symbol, intent=intent)

    def place_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("骨架阶段禁止真实下单")

    def cancel_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("骨架阶段禁止真实撤单")
