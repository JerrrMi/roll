"""下单、撤单、平仓与订单确认（骨架）。"""

from __future__ import annotations

from typing import Any


class OrderExecutor:
    """订单执行器占位；后续接入 signed API 与最小数量验收。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def place_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("骨架阶段禁止真实下单")

    def cancel_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("骨架阶段禁止真实撤单")
