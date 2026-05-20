"""已废弃：订单执行由 ``roll.usdm_auto_trade`` 承担。

Plan 1.0 骨架模块；3.0 实盘/Testnet 的 MARKET 开平仓、STOP 维护、加仓均在
``usdm_auto_trade.py`` 内实现。本模块仅保留向后兼容，新代码请勿依赖。
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from roll.position_manager import PositionManager


class OrderExecutor:
    """已废弃占位；请使用 ``roll.usdm_auto_trade`` 中的执行函数。"""

    def __init__(self, position_manager: PositionManager | None = None, **_kwargs: Any) -> None:
        warnings.warn(
            "OrderExecutor 已废弃；下单/撤单/平仓请使用 roll.usdm_auto_trade",
            DeprecationWarning,
            stacklevel=2,
        )
        self._pm = position_manager

    def _guard_symbol(self, symbol: str, *, intent: str) -> None:
        if self._pm is None:
            return
        self._pm.assert_single_focus_or_raise(symbol, intent=intent)

    def place_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("OrderExecutor 已废弃；请使用 roll.usdm_auto_trade")

    def cancel_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("OrderExecutor 已废弃；请使用 roll.usdm_auto_trade")
