"""离线拉取公有 K 线并评估趋势模型（不写仓、不加杠杆、无签名请求）。"""

from __future__ import annotations

from typing import Any, Mapping

from roll.binance_client import BinanceClientConfig, BinanceUsdmClient
from roll.binance_config import parse_binance_settings
from roll.trend_model import Candle, TrendModel, TrendModelParams, TrendSignal, parse_binance_klines


def evaluate_symbol_offline_public(
    symbol: str,
    *,
    rest_base: str,
    api_prefix: str = "/fapi/v1",
    model: TrendModel | None = None,
    klines_limit: int = 600,
    intervals: tuple[str, ...] = ("15m", "1h", "4h"),
) -> TrendSignal:
    """仅使用公共 REST：`GET /klines`，适合历史样本验收。"""
    m = model or TrendModel()
    cli = BinanceUsdmClient(
        BinanceClientConfig(rest_base=rest_base, api_prefix=api_prefix),
    )

    buckets: dict[str, list[Candle]] = {}
    for iv in intervals:
        rows = cli.klines(symbol, iv, limit=klines_limit)
        buckets[iv] = parse_binance_klines(rows)

    need = tuple(m.params.intervals_required)
    merged: dict[str, list[Candle]] = {k: v for k, v in buckets.items() if k in need}

    missing = set(need) - set(merged)
    if missing:
        raise KeyError(f"缺少周期 K 线: {sorted(missing)}（已拉取 {list(buckets.keys())}）")

    return m.evaluate({k: merged[k] for k in need})


def settings_to_offline_urls(settings: Mapping[str, Any]) -> tuple[str, str]:
    """返回 (rest_base, api_prefix)。"""
    bcfg = parse_binance_settings(settings)
    return bcfg.rest_base, bcfg.api_prefix
