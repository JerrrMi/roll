"""environment-aware signed 守卫单元测试。"""

from __future__ import annotations

import pytest

from roll.signed_guard import (
    ReconcileStateGuardError,
    SignedTradingGuardError,
    assert_reconcile_rest_host_allowed,
    assert_signed_trading_allowed,
)


def test_testnet_passes_with_switch_and_host() -> None:
    env = assert_signed_trading_allowed(
        environment="testnet",
        rest_base="https://testnet.binancefuture.com",
        testnet_signed_orders_enabled=True,
        live_trading_enabled=False,
    )
    assert env == "testnet"


def test_testnet_rejects_live_host() -> None:
    with pytest.raises(SignedTradingGuardError, match="rest_base") as exc:
        assert_signed_trading_allowed(
            environment="testnet",
            rest_base="https://dapi.binance.com",
            testnet_signed_orders_enabled=True,
            live_trading_enabled=False,
        )
    assert "testnet_signed_orders_enabled" not in str(exc.value)
    assert "environment='testnet'" in str(exc.value)


def test_testnet_rejects_missing_switch() -> None:
    with pytest.raises(SignedTradingGuardError, match="testnet_signed_orders_enabled") as exc:
        assert_signed_trading_allowed(
            environment="testnet",
            rest_base="https://testnet.binancefuture.com",
            testnet_signed_orders_enabled=False,
            live_trading_enabled=False,
        )
    assert "environment='testnet'" in str(exc.value)


def test_live_passes_with_switch_and_host() -> None:
    env = assert_signed_trading_allowed(
        environment="live",
        rest_base="https://dapi.binance.com",
        testnet_signed_orders_enabled=False,
        live_trading_enabled=True,
    )
    assert env == "live"


def test_live_rejects_when_switch_off() -> None:
    with pytest.raises(SignedTradingGuardError, match="live_trading_enabled") as exc:
        assert_signed_trading_allowed(
            environment="live",
            rest_base="https://dapi.binance.com",
            testnet_signed_orders_enabled=False,
            live_trading_enabled=False,
        )
    assert "environment='live'" in str(exc.value)


def test_live_rejects_testnet_host() -> None:
    with pytest.raises(SignedTradingGuardError, match="rest_base") as exc:
        assert_signed_trading_allowed(
            environment="live",
            rest_base="https://testnet.binancefuture.com",
            testnet_signed_orders_enabled=False,
            live_trading_enabled=True,
        )
    assert "live_trading_enabled" not in str(exc.value)
    assert "environment='live'" in str(exc.value)


def test_unknown_environment_rejected() -> None:
    with pytest.raises(SignedTradingGuardError, match="environment") as exc:
        assert_signed_trading_allowed(
            environment="staging",
            rest_base="https://dapi.binance.com",
            testnet_signed_orders_enabled=True,
            live_trading_enabled=True,
        )
    assert "staging" in str(exc.value)


def test_reconcile_testnet_passes() -> None:
    env = assert_reconcile_rest_host_allowed(
        environment="testnet",
        rest_base="https://testnet.binancefuture.com",
    )
    assert env == "testnet"


def test_reconcile_live_passes() -> None:
    env = assert_reconcile_rest_host_allowed(
        environment="live",
        rest_base="https://dapi.binance.com",
    )
    assert env == "live"


def test_reconcile_live_rejects_testnet_host() -> None:
    with pytest.raises(ReconcileStateGuardError, match="rest_base") as exc:
        assert_reconcile_rest_host_allowed(
            environment="live",
            rest_base="https://testnet.binancefuture.com",
        )
    assert "environment='live'" in str(exc.value)


def test_reconcile_testnet_rejects_live_host() -> None:
    with pytest.raises(ReconcileStateGuardError, match="rest_base") as exc:
        assert_reconcile_rest_host_allowed(
            environment="testnet",
            rest_base="https://dapi.binance.com",
        )
    assert "environment='testnet'" in str(exc.value)
