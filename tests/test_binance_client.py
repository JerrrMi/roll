"""COIN-M public client：exchangeInfo 解析、候选筛选、时间偏移（mock）。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from roll.binance_client import (
    BinanceCoinMClient,
    BinanceClientConfig,
    CoinMFuturesSymbol,
    InsufficientMonitorableSymbolsError,
    parse_coin_m_specs_from_exchange_info,
    select_monitorable_coin_m_symbols,
)


def _sample_symbol(
    *,
    symbol: str,
    base: str,
    status: str = "TRADING",
    ctype: str = "PERPETUAL",
    tick: str = "0.1",
    min_q: str = "1",
    step: str = "1",
) -> dict:
    return {
        "symbol": symbol,
        "pair": base + "USD",
        "contractType": ctype,
        "deliveryDate": 4133404800000,
        "onboardDate": 1596006000000,
        "contractStatus": status,
        "contractSize": 10,
        "marginAsset": base,
        "quoteAsset": "USD",
        "baseAsset": base,
        "pricePrecision": 1,
        "quantityPrecision": 0,
        "equalQtyPrecision": 4,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": "1", "maxPrice": "9"},
            {"filterType": "LOT_SIZE", "minQty": min_q, "maxQty": "999", "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": min_q, "maxQty": "99", "stepSize": step},
        ],
    }


def test_parse_exchange_info_extracts_precision_and_filters() -> None:
    raw = {"timezone": "UTC", "symbols": [_sample_symbol(symbol="BTCUSD_PERP", base="BTC")]}
    specs = parse_coin_m_specs_from_exchange_info(raw)
    assert len(specs) == 1
    s = specs[0]
    assert s.symbol == "BTCUSD_PERP"
    assert s.base_asset == "BTC"
    assert s.contract_status == "TRADING"
    assert s.contract_type == "PERPETUAL"
    assert s.tick_size == "0.1"
    assert s.min_qty == "1"
    assert s.step_size == "1"


def test_select_requires_three_defaults_perpetual_trading() -> None:
    raw = {
        "symbols": [
            _sample_symbol(symbol="AAAUSD_PERP", base="AAA"),
            _sample_symbol(symbol="BBBUSD_PERP", base="BBB"),
            _sample_symbol(symbol="CCCUSD_PERP", base="CCC"),
        ]
    }
    specs = parse_coin_m_specs_from_exchange_info(raw)
    matched, report = select_monitorable_coin_m_symbols(specs, ["AAA", "BBB", "CCC"])
    assert [x.symbol for x in matched] == ["AAAUSD_PERP", "BBBUSD_PERP", "CCCUSD_PERP"]
    assert len(report.matched) == 3


def test_select_raises_below_min_with_reasons() -> None:
    raw = {"symbols": [_sample_symbol(symbol="AAAUSD_PERP", base="AAA")]}
    specs = parse_coin_m_specs_from_exchange_info(raw)
    with pytest.raises(InsufficientMonitorableSymbolsError) as ei:
        select_monitorable_coin_m_symbols(specs, ["AAA", "BBB", "CCC"], min_count=3)
    assert "BBB" in str(ei.value)
    texts = [r.reason or "" for r in ei.value.report.rows]
    assert any("baseAsset" in t for t in texts)


def test_select_skips_non_trading_or_wrong_contract_type() -> None:
    raw = {
        "symbols": [
            _sample_symbol(symbol="AAAUSD_PERP", base="AAA"),
            _sample_symbol(symbol="BBBUSD_PERP", base="BBB", status="HALT"),
            _sample_symbol(symbol="CCCUSD_241227", base="CCC", ctype="CURRENT_QUARTER"),
            _sample_symbol(symbol="DDDUSD_PERP", base="DDD"),
            _sample_symbol(symbol="EEEUSD_PERP", base="EEE"),
        ]
    }
    specs = parse_coin_m_specs_from_exchange_info(raw)
    matched, _ = select_monitorable_coin_m_symbols(
        specs,
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        min_count=3,
        allowed_contract_types=frozenset({"PERPETUAL"}),
    )
    bases = [s.base_asset for s in matched]
    assert bases == ["AAA", "DDD", "EEE"]


def test_sync_server_time_offset_cached() -> None:
    cfg = BinanceClientConfig(rest_base="https://example.invalid")
    client = BinanceCoinMClient(cfg)
    with patch.object(BinanceCoinMClient, "fetch_server_time_ms", return_value=7000):
        with patch("roll.binance_client._millis_now", side_effect=[4000, 6000]):
            off = client.sync_server_time()
    assert off == 2000  # 7000 - (4000 + 6000) // 2
    assert client.server_offset_ms == 2000
    assert client.estimated_server_time_ms() >= 2000
