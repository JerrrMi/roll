"""K 线、价格、订单簿等行情接入占位。"""

from __future__ import annotations

from typing import Any


def parse_candidate_assets(settings: dict[str, Any]) -> list[str]:
    """从配置字典解析候选资产列表（骨架，无 exchangeInfo 校验）。"""
    c = settings.get("candidates")
    if isinstance(c, list):
        return [str(x) for x in c]
    return []


class MarketDataService:
    """行情服务占位；后续从 REST/WebSocket 拉取 K 线与 ticker。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 500,
    ) -> Any:
        """占位：后续调用 GET /dapi/v1/klines。"""
        raise NotImplementedError("骨架阶段未实现行情拉取")
