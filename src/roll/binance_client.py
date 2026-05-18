"""Binance COIN-M Futures HTTP 客户端占位。

后续阶段在此实现 public / signed 请求、HMAC 签名与时间偏移；当前不发起网络请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_TESTNET_REST_BASE = "https://testnet.binancefuture.com"
DEFAULT_COIN_M_PREFIX = "/dapi/v1"


@dataclass
class BinanceClientConfig:
    rest_base: str = DEFAULT_TESTNET_REST_BASE
    coin_m_prefix: str = DEFAULT_COIN_M_PREFIX
    api_key: str | None = None
    api_secret: str | None = None
    recv_window_ms: int = 5000


class BinanceCoinMClient:
    """COIN-M Futures REST 封装（骨架）。"""

    def __init__(self, config: BinanceClientConfig | None = None) -> None:
        self._config = config or BinanceClientConfig()

    @property
    def config(self) -> BinanceClientConfig:
        return self._config

    def ping(self) -> Any:
        """占位：后续映射 GET /dapi/v1/ping。"""
        raise NotImplementedError("骨架阶段未实现网络层")

    def server_time(self) -> Any:
        """占位：后续映射 GET /dapi/v1/time。"""
        raise NotImplementedError("骨架阶段未实现网络层")
