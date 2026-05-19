"""environment-aware signed 交易启动守卫（USD-M / product=usdm）。"""

from __future__ import annotations

from roll.binance_client import (
    DEFAULT_LIVE_REST_BASE,
    DEFAULT_TESTNET_REST_BASE,
    is_binance_futures_testnet_url,
    is_binance_usdm_live_url,
)
from roll.binance_config import assert_usdm_api_prefix

_ALLOWED_ENVIRONMENTS = frozenset({"testnet", "live"})


class SignedTradingGuardError(RuntimeError):
    """`run-loop --no-dry-run` 等 signed 路径未通过 environment-aware 守卫。"""


class ReconcileStateGuardError(RuntimeError):
    """`reconcile-state` 未通过 environment / rest_base 校验。"""


def normalize_trading_environment(environment: str | None) -> str:
    """将配置 `environment` 规范为 `testnet` 或 `live`；空值视为 testnet。"""
    raw = (environment or "").strip().lower()
    if raw in {"", "testnet"}:
        return "testnet"
    return raw


def _reject_dapi_rest_base(rest_base: str, *, prefix: str) -> None:
    rb = (rest_base or "").strip().lower()
    if "dapi.binance.com" in rb:
        raise SignedTradingGuardError(
            f"{prefix} rest_base={rest_base!r} 指向 COIN-M 实盘 host（dapi.binance.com）；"
            f" USD-M 须使用 {DEFAULT_LIVE_REST_BASE!r}。"
        )


def assert_signed_trading_allowed(
    *,
    environment: str | None,
    rest_base: str,
    api_prefix: str,
    product: str = "usdm",
    testnet_signed_orders_enabled: bool,
    live_trading_enabled: bool,
    command_label: str = "run-loop",
) -> str:
    """校验 signed 自动交易启动条件；通过时返回规范后的 environment。

    不满足时抛出 `SignedTradingGuardError`，消息包含 environment、rest_base、缺失的安全开关。
    """
    env = normalize_trading_environment(environment)
    rb = (rest_base or "").strip() or DEFAULT_TESTNET_REST_BASE
    prefix = f"[{command_label}] --no-dry-run 被拒绝：environment={env!r} product={product!r} rest_base={rb!r}"

    if product.strip().lower() != "usdm":
        raise SignedTradingGuardError(
            f"{prefix} 不支持的 product（当前仅允许 usdm）。"
            " 缺失或未满足的安全开关：无（product 无效）。"
        )

    try:
        assert_usdm_api_prefix(api_prefix, context=command_label)
    except ValueError as exc:
        raise SignedTradingGuardError(str(exc)) from exc

    if env not in _ALLOWED_ENVIRONMENTS:
        raise SignedTradingGuardError(
            f"{prefix} 不支持的 environment（仅允许 testnet、live）。"
            " 缺失或未满足的安全开关：无（environment 无效）。"
        )

    if env == "testnet":
        if not is_binance_futures_testnet_url(rb):
            expected = DEFAULT_TESTNET_REST_BASE
            raise SignedTradingGuardError(
                f"{prefix} Testnet signed 仅允许官方 Futures Testnet REST host（{expected!r}）。"
                " 缺失或未满足的安全开关：无（rest_base 与 environment 不匹配）。"
            )
        _reject_dapi_rest_base(rb, prefix=prefix)
        if not testnet_signed_orders_enabled:
            raise SignedTradingGuardError(
                f"{prefix} 缺失或未满足的安全开关：strategy.testnet_signed_orders_enabled"
                "（当前为 false，须显式设为 true）。"
            )
        return env

    # env == "live"
    _reject_dapi_rest_base(rb, prefix=prefix)
    if not is_binance_usdm_live_url(rb):
        expected = DEFAULT_LIVE_REST_BASE
        raise SignedTradingGuardError(
            f"{prefix} live signed 仅允许官方 USD-M 实盘 REST host（{expected!r}）。"
            " 缺失或未满足的安全开关：无（rest_base 与 environment 不匹配）。"
        )
    if not live_trading_enabled:
        raise SignedTradingGuardError(
            f"{prefix} 缺失或未满足的安全开关：strategy.live_trading_enabled"
            "（当前为 false，须显式设为 true）。"
        )
    return env


def assert_reconcile_rest_host_allowed(
    *,
    environment: str | None,
    rest_base: str,
    api_prefix: str,
    product: str = "usdm",
    command_label: str = "reconcile-state",
) -> str:
    """校验对账命令的 environment 与 REST host 匹配；通过时返回规范后的 environment。"""
    env = normalize_trading_environment(environment)
    rb = (rest_base or "").strip() or DEFAULT_TESTNET_REST_BASE
    prefix = f"[{command_label}] environment={env!r} product={product!r} rest_base={rb!r}"

    if product.strip().lower() != "usdm":
        raise ReconcileStateGuardError(f"{prefix} 不支持的 product（当前仅允许 usdm）。")

    try:
        assert_usdm_api_prefix(api_prefix, context=command_label)
    except ValueError as exc:
        raise ReconcileStateGuardError(str(exc)) from exc

    if env not in _ALLOWED_ENVIRONMENTS:
        raise ReconcileStateGuardError(
            f"{prefix} 不支持的 environment（仅允许 testnet、live）。"
        )

    if env == "testnet":
        if not is_binance_futures_testnet_url(rb):
            expected = DEFAULT_TESTNET_REST_BASE
            raise ReconcileStateGuardError(
                f"{prefix} Testnet 对账仅允许官方 Futures Testnet REST host（{expected!r}）。"
            )
        _reject_dapi_rest_base(rb, prefix=prefix)
        return env

    _reject_dapi_rest_base(rb, prefix=prefix)
    if not is_binance_usdm_live_url(rb):
        expected = DEFAULT_LIVE_REST_BASE
        raise ReconcileStateGuardError(
            f"{prefix} live 对账仅允许官方 USD-M 实盘 REST host（{expected!r}）。"
        )
    return env
