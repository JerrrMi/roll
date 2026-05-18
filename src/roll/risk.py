"""Kelly、仓位、止损、账户熔断等（骨架：仅占位类型）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    """风险参数占位；后续与配置联动。"""

    max_single_loss_fraction: float = 0.02
    max_position_fraction: float = 0.15
    kelly_multiplier: float = 0.25


class RiskEngine:
    """风控引擎占位。"""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self._limits = limits or RiskLimits()

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def can_open(self, *_args: object, **_kwargs: object) -> bool:
        """占位：始终返回 False，避免误用骨架。"""
        return False
