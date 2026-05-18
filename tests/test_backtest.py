"""回测工具：时间轴对齐等纯逻辑。"""

from __future__ import annotations

from roll.backtest import build_aligned_timeline
from roll.trend_model import Candle


def _c(ms: int, px: float = 1.0) -> Candle:
    return Candle(open_time_ms=ms, open=px, high=px, low=px, close=px, volume=1.0)


def test_build_aligned_timeline_intersection() -> None:
    data = {
        "A": {
            "15m": [_c(0), _c(900_000), _c(1_800_000)],
            "1h": [_c(0)],
            "4h": [_c(0)],
        },
        "B": {
            "15m": [_c(900_000), _c(1_800_000)],
            "1h": [_c(0)],
            "4h": [_c(0)],
        },
    }
    axis, _ = build_aligned_timeline(data, ["A", "B"])
    assert len(axis) == 2
    assert axis[0].open_time_ms == 900_000
