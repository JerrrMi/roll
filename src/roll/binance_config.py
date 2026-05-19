"""从 settings YAML 解析 Binance USD-M 产品配置（3.0）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from roll.binance_client import (
    DEFAULT_API_PREFIX,
    DEFAULT_LIVE_REST_BASE,
    DEFAULT_PRODUCT,
    DEFAULT_TESTNET_REST_BASE,
    BinanceClientConfig,
)

_SUPPORTED_PRODUCTS = frozenset({"usdm"})


class BinanceConfigError(ValueError):
    """settings 中 binance 块无效或仍使用已废弃的 COIN-M 字段。"""


@dataclass(frozen=True)
class ParsedBinanceConfig:
    """`settings.binance` 解析结果。"""

    product: str
    rest_base: str
    api_prefix: str
    recv_window_ms: int

    def to_client_config(self, **overrides: Any) -> BinanceClientConfig:
        base = {
            "product": self.product,
            "rest_base": self.rest_base,
            "api_prefix": self.api_prefix,
            "recv_window_ms": self.recv_window_ms,
        }
        base.update(overrides)
        return BinanceClientConfig(**base)


def _reject_legacy_coin_m_fields(b: Mapping[str, Any]) -> None:
    if "coin_m_prefix" in b:
        raise BinanceConfigError(
            "binance.coin_m_prefix 已废弃（COIN-M /dapi）；"
            "请改用 binance.product: usdm 与 binance.api_prefix: /fapi/v1。"
        )
    rb = str(b.get("rest_base", "")).strip().lower()
    if "dapi.binance.com" in rb:
        raise BinanceConfigError(
            "binance.rest_base 不得指向 COIN-M 实盘 host（dapi.binance.com）；"
            "USD-M live 应使用 https://fapi.binance.com。"
        )


def _resolve_api_prefix(b: Mapping[str, Any]) -> str:
    if "api_prefix" in b:
        pref = str(b["api_prefix"]).strip()
    elif "usd_m_prefix" in b:
        pref = str(b["usd_m_prefix"]).strip()
    else:
        pref = DEFAULT_API_PREFIX
    if not pref.startswith("/"):
        pref = "/" + pref
    low = pref.lower()
    if "dapi" in low:
        raise BinanceConfigError(
            f"binance.api_prefix={pref!r} 含 COIN-M 路径 /dapi；USD-M 应使用 /fapi/v1。"
        )
    if not low.startswith("/fapi"):
        raise BinanceConfigError(
            f"binance.api_prefix={pref!r} 不是 USD-M 路径；当前 product=usdm 要求 /fapi 前缀。"
        )
    return pref


def parse_binance_settings(settings: Mapping[str, Any] | None) -> ParsedBinanceConfig:
    """解析 `settings['binance']`；缺省时返回 USD-M Testnet 默认值。"""
    if not settings:
        return ParsedBinanceConfig(
            product=DEFAULT_PRODUCT,
            rest_base=DEFAULT_TESTNET_REST_BASE,
            api_prefix=DEFAULT_API_PREFIX,
            recv_window_ms=5000,
        )

    b = settings.get("binance")
    if not isinstance(b, Mapping):
        return ParsedBinanceConfig(
            product=DEFAULT_PRODUCT,
            rest_base=DEFAULT_TESTNET_REST_BASE,
            api_prefix=DEFAULT_API_PREFIX,
            recv_window_ms=5000,
        )

    _reject_legacy_coin_m_fields(b)

    product = str(b.get("product", DEFAULT_PRODUCT)).strip().lower()
    if product not in _SUPPORTED_PRODUCTS:
        raise BinanceConfigError(
            f"不支持的 binance.product={product!r}（当前仅支持: {sorted(_SUPPORTED_PRODUCTS)}）。"
        )

    api_prefix = _resolve_api_prefix(b)
    rest_base = str(b.get("rest_base", DEFAULT_TESTNET_REST_BASE)).strip().rstrip("/") or DEFAULT_TESTNET_REST_BASE
    rw = b.get("recv_window_ms", 5000)
    recv_window_ms = int(rw) if isinstance(rw, (int, float)) and not isinstance(rw, bool) else 5000

    return ParsedBinanceConfig(
        product=product,
        rest_base=rest_base,
        api_prefix=api_prefix,
        recv_window_ms=max(recv_window_ms, 1000),
    )


def assert_usdm_api_prefix(api_prefix: str, *, context: str = "binance") -> None:
    """拒绝 COIN-M `/dapi` 前缀，防止误连币本位 API。"""
    pref = (api_prefix or "").strip()
    if not pref.startswith("/"):
        pref = "/" + pref
    low = pref.lower()
    if "dapi" in low:
        raise BinanceConfigError(
            f"{context} api_prefix={pref!r} 含 /dapi（COIN-M）；USD-M 须使用 /fapi/v1。"
        )
    if not low.startswith("/fapi"):
        raise BinanceConfigError(
            f"{context} api_prefix={pref!r} 不是 /fapi 路径；product=usdm 时须指向 USD-M REST。"
        )
