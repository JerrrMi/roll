"""P3 工程质量：别名、废弃模块、资金费率、成本估算。"""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock

import pytest

from roll.backtest import funding_cashflow_usdt
from roll.binance_client import (
    BinanceCoinMClient,
    BinanceCoinMSignedClient,
    BinanceUsdmClient,
    BinanceUsdmSignedClient,
)
from roll.cost_estimate import format_live_cost_est
from roll.order_executor import OrderExecutor
from roll.strategy_loop import parse_strategy_loop_params


def test_usdm_signed_client_is_primary_name() -> None:
    assert BinanceUsdmSignedClient is BinanceCoinMSignedClient
    assert BinanceUsdmClient is BinanceCoinMClient
    assert "Usdm" in BinanceUsdmSignedClient.__name__


def test_order_executor_emits_deprecation_warning() -> None:
    with pytest.warns(DeprecationWarning, match="OrderExecutor"):
        OrderExecutor()


def test_order_executor_place_order_raises() -> None:
    with pytest.warns(DeprecationWarning):
        ex = OrderExecutor()
    with pytest.raises(NotImplementedError, match="usdm_auto_trade"):
        ex.place_order()


def test_funding_cashflow_long_pays_when_rate_positive() -> None:
    pay = funding_cashflow_usdt(side="long", quantity=10.0, mark_px=100.0, funding_rate=0.0001)
    assert pay == pytest.approx(0.1)


def test_funding_cashflow_short_receives_when_rate_positive() -> None:
    pay = funding_cashflow_usdt(side="short", quantity=10.0, mark_px=100.0, funding_rate=0.0001)
    assert pay == pytest.approx(-0.1)


def test_format_live_cost_est_contains_fee_and_slip() -> None:
    msg = format_live_cost_est(
        intent="open",
        side="long",
        quantity=100.0,
        mark_px=1.0,
        fee_bps=5.0,
        slippage_bps=2.0,
    )
    assert "[live][cost_est]" in msg
    assert "fee≈" in msg
    assert "slip_px≈" in msg


def test_parse_strategy_loop_cost_estimate_defaults() -> None:
    p = parse_strategy_loop_params({})
    assert p.estimated_taker_fee_bps == 5.0
    assert p.estimated_slippage_bps == 2.0


def test_fetch_funding_rate_series_deduplicates() -> None:
    from roll.backtest import fetch_funding_rate_series

    client = MagicMock()
    client.funding_rate_history.side_effect = [
        [
            {"fundingTime": 1000, "fundingRate": "0.0001"},
            {"fundingTime": 2000, "fundingRate": "0.0002"},
        ],
        [],
    ]
    rows = fetch_funding_rate_series(client, "BTCUSDT", start_ms=0, end_ms=5000)
    assert rows == [(1000, 0.0001), (2000, 0.0002)]


def test_funding_rate_history_parses_response() -> None:
    client = BinanceUsdmClient()
    payload = [
        {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": 1700000000000},
        {"symbol": "BTCUSDT", "fundingRate": "-0.0002", "fundingTime": 1700028800000},
    ]

    def fake_request(method: str, path: str, params=None):
        assert path == "/fundingRate"
        return payload

    client._request_json = fake_request  # type: ignore[method-assign]
    rows = client.funding_rate_history("BTCUSDT", start_time_ms=0, end_time_ms=9999999999999)
    assert len(rows) == 2
    assert int(rows[0]["fundingTime"]) == 1700000000000
