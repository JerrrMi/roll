"""多周期趋势评分与信号解释（骨架：仅类型与占位结果）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SignalSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    NO_TRADE = "no_trade"


@dataclass(frozen=True)
class TrendSignal:
    """趋势输出：方向、总分、可读原因（后续填充各因子）。"""

    side: SignalSide
    score: float
    reasons: tuple[str, ...]


class TrendModel:
    """趋势模型占位；后续实现斜率、ADX、EMA、Donchian 等。"""

    def evaluate(self, *args: object, **kwargs: object) -> TrendSignal:
        """占位：返回保守的 no_trade。"""
        return TrendSignal(side=SignalSide.NO_TRADE, score=0.0, reasons=("skeleton_stub",))
