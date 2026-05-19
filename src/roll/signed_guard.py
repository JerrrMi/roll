"""environment-aware signed 交易启动守卫（不改变策略模型）。"""

from __future__ import annotations

from roll.binance_client import (
    DEFAULT_LIVE_REST_BASE,
    DEFAULT_TESTNET_REST_BASE,
    is_binance_coin_m_live_url,
    is_binance_coin_m_testnet_url,
)

_ALLOWED_ENVIRONMENTS = frozenset({"testnet", "live"})


class SignedTradingGuardError(RuntimeError):
    """`run-loop --no-dry-run` 等 signed 路径未通过 environment-aware 守卫。"""


def normalize_trading_environment(environment: str | None) -> str:
    """将配置 `environment` 规范为 `testnet` 或 `live`；空值视为 testnet。"""
    raw = (environment or "").strip().lower()
    if raw in {"", "testnet"}:
        return "testnet"
    return raw


def assert_signed_trading_allowed(
    *,
    environment: str | None,
    rest_base: str,
    testnet_signed_orders_enabled: bool,
    live_trading_enabled: bool,
    command_label: str = "run-loop",
) -> str:
    """校验 signed 自动交易启动条件；通过时返回规范后的 environment。

    不满足时抛出 `SignedTradingGuardError`，消息包含 environment、rest_base、缺失的安全开关。
    """
    env = normalize_trading_environment(environment)
    rb = (rest_base or "").strip() or DEFAULT_TESTNET_REST_BASE
    prefix = f"[{command_label}] --no-dry-run 被拒绝：environment={env!r} rest_base={rb!r}"

    if env not in _ALLOWED_ENVIRONMENTS:
        raise SignedTradingGuardError(
            f"{prefix} 不支持的 environment（仅允许 testnet、live）。"
            " 缺失或未满足的安全开关：无（environment 无效）。"
        )

    if env == "testnet":
        if not is_binance_coin_m_testnet_url(rb):
            expected = DEFAULT_TESTNET_REST_BASE
            raise SignedTradingGuardError(
                f"{prefix} Testnet signed 仅允许官方 Futures Testnet REST host（{expected!r}）。"
                " 缺失或未满足的安全开关：无（rest_base 与 environment 不匹配）。"
            )
        if not testnet_signed_orders_enabled:
            raise SignedTradingGuardError(
                f"{prefix} 缺失或未满足的安全开关：strategy.testnet_signed_orders_enabled"
                "（当前为 false，须显式设为 true）。"
            )
        return env

    # env == "live"
    if not is_binance_coin_m_live_url(rb):
        expected = DEFAULT_LIVE_REST_BASE
        raise SignedTradingGuardError(
            f"{prefix} live signed 仅允许官方 COIN-M 实盘 REST host（{expected!r}）。"
            " 缺失或未满足的安全开关：无（rest_base 与 environment 不匹配）。"
        )
    if not live_trading_enabled:
        raise SignedTradingGuardError(
            f"{prefix} 缺失或未满足的安全开关：strategy.live_trading_enabled"
            "（当前为 false，须显式设为 true）。"
        )
    return env
