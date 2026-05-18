"""候选资产配置解析与 exchangeInfo 筛选集成测试（离线 fixture）。"""

from roll.binance_client import (
    parse_coin_m_specs_from_exchange_info,
    select_monitorable_coin_m_symbols,
)
from roll.market_data import parse_candidate_assets


def test_parse_candidate_assets() -> None:
    cfg = {"candidates": ["DOGE", "AVAX", "SHIB"]}
    assert parse_candidate_assets(cfg) == ["DOGE", "AVAX", "SHIB"]


def test_parse_candidate_assets_empty() -> None:
    assert parse_candidate_assets({}) == []


def test_settings_candidates_flow_with_exchange_info_fixture() -> None:
    cfg = {"candidates": ["AAA", "BBB", "CCC"]}
    candidates = parse_candidate_assets(cfg)
    raw = {
        "symbols": [
            {
                "symbol": "AAAUSD_PERP",
                "pair": "AAAUSD",
                "contractType": "PERPETUAL",
                "deliveryDate": 4133404800000,
                "onboardDate": 1596006000000,
                "contractStatus": "TRADING",
                "contractSize": 1,
                "marginAsset": "AAA",
                "quoteAsset": "USD",
                "baseAsset": "AAA",
                "pricePrecision": 5,
                "quantityPrecision": 0,
                "equalQtyPrecision": 4,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.00001", "minPrice": "1", "maxPrice": "9"},
                    {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "999999", "stepSize": "1"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "1", "maxQty": "99999", "stepSize": "1"},
                ],
            },
            {
                "symbol": "BBBUSD_PERP",
                "pair": "BBBUSD",
                "contractType": "PERPETUAL",
                "contractStatus": "TRADING",
                "deliveryDate": 4133404800000,
                "onboardDate": 1596006000000,
                "contractSize": 1,
                "marginAsset": "BBB",
                "quoteAsset": "USD",
                "baseAsset": "BBB",
                "pricePrecision": 4,
                "quantityPrecision": 0,
                "equalQtyPrecision": 4,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.0001", "minPrice": "1", "maxPrice": "9"},
                    {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "999999", "stepSize": "1"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "1", "maxQty": "99999", "stepSize": "1"},
                ],
            },
            {
                "symbol": "CCCUSD_PERP",
                "pair": "CCCUSD",
                "contractType": "PERPETUAL",
                "contractStatus": "TRADING",
                "deliveryDate": 4133404800000,
                "onboardDate": 1596006000000,
                "contractSize": 1,
                "marginAsset": "CCC",
                "quoteAsset": "USD",
                "baseAsset": "CCC",
                "pricePrecision": 3,
                "quantityPrecision": 0,
                "equalQtyPrecision": 4,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.001", "minPrice": "1", "maxPrice": "9"},
                    {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "999999", "stepSize": "1"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "1", "maxQty": "99999", "stepSize": "1"},
                ],
            },
        ]
    }
    specs = parse_coin_m_specs_from_exchange_info(raw)
    matched, _ = select_monitorable_coin_m_symbols(specs, candidates, min_count=3)
    assert {s.base_asset for s in matched} == {"AAA", "BBB", "CCC"}
