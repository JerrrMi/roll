"""USD-M public client：exchangeInfo 解析、候选筛选、时间偏移（mock）。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from roll.binance_client import (
    BinanceClientConfig,
    BinanceUsdmClient,
    BinanceCoinMSignedClient,
    BinanceSignerError,
    InsufficientMonitorableSymbolsError,
    build_hmac_sha256,
    build_signed_query_string,
    format_floor_to_step_decimal_str,
    is_binance_futures_testnet_url,
    is_binance_usdm_live_url,
    parse_usdm_specs_from_exchange_info,
    redact_signed_query_url,
    select_monitorable_usdm_symbols,
)


def _sample_usdm_symbol(
    *,
    symbol: str,
    base: str,
    status: str = "TRADING",
    ctype: str = "PERPETUAL",
    quote: str = "USDT",
    margin: str = "USDT",
    tick: str = "0.01",
    min_q: str = "1",
    step: str = "1",
    min_notional: str = "5",
) -> dict:
    return {
        "symbol": symbol,
        "pair": symbol,
        "contractType": ctype,
        "deliveryDate": 4133404800000,
        "onboardDate": 1596006000000,
        "status": status,
        "baseAsset": base,
        "quoteAsset": quote,
        "marginAsset": margin,
        "pricePrecision": 2,
        "quantityPrecision": 0,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": "0.01", "maxPrice": "999999"},
            {"filterType": "LOT_SIZE", "minQty": min_q, "maxQty": "999999", "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": min_q, "maxQty": "99999", "stepSize": step},
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
        ],
    }


def test_parse_exchange_info_extracts_precision_filters_and_min_notional() -> None:
    raw = {"timezone": "UTC", "symbols": [_sample_usdm_symbol(symbol="BTCUSDT", base="BTC")]}
    specs = parse_usdm_specs_from_exchange_info(raw)
    assert len(specs) == 1
    s = specs[0]
    assert s.symbol == "BTCUSDT"
    assert s.base_asset == "BTC"
    assert s.quote_asset == "USDT"
    assert s.margin_asset == "USDT"
    assert s.status == "TRADING"
    assert s.contract_type == "PERPETUAL"
    assert s.contract_size == 1.0
    assert s.tick_size == "0.01"
    assert s.min_qty == "1"
    assert s.step_size == "1"
    assert s.min_notional == "5"


def test_select_requires_three_usdt_perpetual_trading() -> None:
    raw = {
        "symbols": [
            _sample_usdm_symbol(symbol="AAAUSDT", base="AAA"),
            _sample_usdm_symbol(symbol="BBBUSDT", base="BBB"),
            _sample_usdm_symbol(symbol="CCCUSDT", base="CCC"),
        ]
    }
    specs = parse_usdm_specs_from_exchange_info(raw)
    matched, report = select_monitorable_usdm_symbols(specs, ["AAA", "BBB", "CCC"])
    assert [x.symbol for x in matched] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert len(report.matched) == 3


def test_select_raises_below_min_with_reasons() -> None:
    raw = {"symbols": [_sample_usdm_symbol(symbol="AAAUSDT", base="AAA")]}
    specs = parse_usdm_specs_from_exchange_info(raw)
    with pytest.raises(InsufficientMonitorableSymbolsError) as ei:
        select_monitorable_usdm_symbols(specs, ["AAA", "BBB", "CCC"], min_count=3)
    assert "BBB" in str(ei.value)
    texts = [r.reason or "" for r in ei.value.report.rows]
    assert any("baseAsset" in t for t in texts)


def test_select_skips_non_trading_or_non_usdt_perpetual() -> None:
    raw = {
        "symbols": [
            _sample_usdm_symbol(symbol="AAAUSDT", base="AAA"),
            _sample_usdm_symbol(symbol="BBBUSD_PERP", base="BBB", quote="USD", margin="BBB"),
            _sample_usdm_symbol(symbol="CCCUSDT", base="CCC", status="HALT"),
            _sample_usdm_symbol(symbol="DDDUSDT", base="DDD", ctype="CURRENT_QUARTER"),
            _sample_usdm_symbol(symbol="EEEUSDT", base="EEE"),
        ]
    }
    specs = parse_usdm_specs_from_exchange_info(raw)
    matched, _ = select_monitorable_usdm_symbols(
        specs,
        ["AAA", "BBB", "CCC", "DDD", "EEE"],
        min_count=2,
    )
    bases = [s.base_asset for s in matched]
    assert bases == ["AAA", "EEE"]


def test_select_does_not_guess_1000shib_for_shib() -> None:
    raw = {
        "symbols": [
            _sample_usdm_symbol(symbol="1000SHIBUSDT", base="1000SHIB"),
            _sample_usdm_symbol(symbol="DOGEUSDT", base="DOGE"),
            _sample_usdm_symbol(symbol="AVAXUSDT", base="AVAX"),
            _sample_usdm_symbol(symbol="BTCUSDT", base="BTC"),
        ]
    }
    specs = parse_usdm_specs_from_exchange_info(raw)
    matched, report = select_monitorable_usdm_symbols(
        specs,
        ["SHIB", "1000SHIB", "DOGE", "AVAX", "BTC"],
        min_count=3,
    )
    assert {s.base_asset for s in matched} == {"1000SHIB", "DOGE", "AVAX", "BTC"}
    shib_row = next(r for r in report.rows if r.candidate == "SHIB")
    assert shib_row.matched is None


def test_sync_server_time_offset_cached() -> None:
    cfg = BinanceClientConfig(rest_base="https://example.invalid")
    client = BinanceUsdmClient(cfg)
    with patch.object(BinanceUsdmClient, "fetch_server_time_ms", return_value=7000):
        with patch("roll.binance_client._millis_now", side_effect=[4000, 6000]):
            off = client.sync_server_time()
    assert off == 2000  # 7000 - (4000 + 6000) // 2
    assert client.server_offset_ms == 2000
    assert client.estimated_server_time_ms() >= 2000


def test_build_signed_hmac_matches_payload() -> None:
    params = {"recvWindow": 5000, "timestamp": 1700000000000}
    out = build_signed_query_string(params, signing_secret="s")
    body, sep, sig = out.partition("&signature=")
    assert sep
    assert sig == build_hmac_sha256("s", body)


def test_signed_query_lexicographic_and_bool() -> None:
    out = build_signed_query_string(
        {"timestamp": 1, "z": False, "a": True, "recvWindow": 2},
        signing_secret="x",
    )
    base, _, _sig = out.partition("&signature=")
    assert base == "a=true&recvWindow=2&timestamp=1&z=false"


def test_redact_signature_param() -> None:
    raw = (
        "https://testnet.binancefuture.com/fapi/v1/order?"
        "symbol=ABC&signature=aaaaaaaa&recvWindow=1"
    )
    clean = redact_signed_query_url(raw)
    assert "signature=" not in clean
    assert "recvWindow=1" in clean


def test_is_testnet_url_https_only() -> None:
    assert is_binance_futures_testnet_url("https://testnet.binancefuture.com")
    assert not is_binance_futures_testnet_url("http://testnet.binancefuture.com")
    assert not is_binance_futures_testnet_url("https://fapi.binance.com")


def test_is_live_url_https_only() -> None:
    assert is_binance_usdm_live_url("https://fapi.binance.com")
    assert not is_binance_usdm_live_url("http://fapi.binance.com")
    assert not is_binance_usdm_live_url("https://dapi.binance.com")
    assert not is_binance_usdm_live_url("https://testnet.binancefuture.com")


def test_floor_quantity_to_step_and_signed_client_requires_creds() -> None:
    assert format_floor_to_step_decimal_str("1.99", "0.5") == "1.5"

    cfg = BinanceClientConfig(rest_base="https://testnet.binancefuture.com")
    with pytest.raises(ValueError, match="api_key"):
        BinanceCoinMSignedClient(cfg)

    cfg_k = BinanceClientConfig(rest_base="https://testnet.binancefuture.com", api_key="k")
    with pytest.raises(ValueError, match="api_secret"):
        BinanceCoinMSignedClient(cfg_k)


def test_signer_rejects_signature_keys() -> None:
    with pytest.raises(BinanceSignerError):
        build_signed_query_string({"signature": "x"}, signing_secret="k")
