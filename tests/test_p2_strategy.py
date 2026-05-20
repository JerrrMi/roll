"""P2 策略完善：ATR/追踪止损合并、最大持仓时间、margin type 解析。"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from roll.account_modes import (
    MarginModeSettings,
    normalize_margin_type_from_exchange,
    parse_margin_mode_settings,
    read_symbol_margin_type,
)
from roll.strategy_loop import parse_strategy_loop_params
from roll.trend_model import Candle, wilder_atr_last
from roll.usdm_auto_trade import (
    desired_protective_stop_price,
    should_exit_max_hold,
)


def test_desired_protective_stop_most_conservative_long() -> None:
    # 多头取最高止损：固定 98、追踪 107.8、ATR 97 → 107.8
    px = desired_protective_stop_price(
        "long",
        100.0,
        110.0,
        0.02,
        0.02,
        atr=2.0,
        atr_stop_k=1.5,
    )
    assert px == pytest.approx(107.8)


def test_desired_protective_stop_atr_tighter_short() -> None:
    # 空头取最低止损：固定 105、追踪 91.8、ATR 102 → 91.8
    px = desired_protective_stop_price(
        "short",
        100.0,
        90.0,
        0.05,
        0.02,
        atr=1.0,
        atr_stop_k=2.0,
    )
    assert px == pytest.approx(91.8)


def test_should_exit_max_hold_seconds_and_ms() -> None:
    assert not should_exit_max_hold(opened_unix_ms=None, max_hold_hours=48.0)
    assert not should_exit_max_hold(opened_unix_ms=1000.0, max_hold_hours=None)
    now = 1000.0 + 49 * 3600
    assert should_exit_max_hold(
        opened_unix_ms=1000.0,
        max_hold_hours=48.0,
        now_unix=now,
    )
    opened_ms = 1_700_000_000_000
    now_s = opened_ms / 1000.0 + 49 * 3600
    assert should_exit_max_hold(
        opened_unix_ms=opened_ms,
        max_hold_hours=48.0,
        now_unix=now_s,
    )


def test_parse_strategy_p2_params() -> None:
    p = parse_strategy_loop_params(
        {
            "strategy": {
                "trail_stop_fraction": 0.02,
                "atr_stop_k": 1.5,
                "atr_period": 20,
                "max_hold_hours": 48,
            }
        }
    )
    assert p.trail_stop_fraction == 0.02
    assert p.atr_stop_k == 1.5
    assert p.atr_period == 20
    assert p.max_hold_hours == 48.0


def test_wilder_atr_last() -> None:
    candles = [
        Candle(i * 60_000, 10.0, 11.0, 9.0, 10.0, 1.0) for i in range(30)
    ]
    atr = wilder_atr_last(candles, period=14)
    assert math.isfinite(atr) and atr > 0


def test_parse_margin_mode_settings() -> None:
    cfg = parse_margin_mode_settings(
        {"binance": {"margin_type": "ISOLATED", "apply_margin_type": True}}
    )
    assert cfg == MarginModeSettings(margin_type="ISOLATED", apply_margin_type=True)
    assert normalize_margin_type_from_exchange("cross") == "CROSSED"


def test_read_symbol_margin_type() -> None:
    client = MagicMock()
    client.position_risk.return_value = [
        {"symbol": "DOGEUSDT", "marginType": "isolated"},
    ]
    assert read_symbol_margin_type(client, "DOGEUSDT") == "ISOLATED"
