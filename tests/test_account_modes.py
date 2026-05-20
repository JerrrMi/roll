"""账户模式：margin type 与 one-way / hedge 持仓模式。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from roll.account_modes import (
    HedgePositionModeError,
    ensure_one_way_position_mode_before_open,
    parse_dual_side_position_from_account,
    position_mode_label,
    read_dual_side_position,
)


def test_parse_dual_side_position_from_account_bool() -> None:
    assert parse_dual_side_position_from_account({"dualSidePosition": False}) is False
    assert parse_dual_side_position_from_account({"dualSidePosition": True}) is True


def test_parse_dual_side_position_from_account_string() -> None:
    assert parse_dual_side_position_from_account({"dualSidePosition": "false"}) is False
    assert parse_dual_side_position_from_account({"dualSidePosition": "true"}) is True


def test_position_mode_label() -> None:
    assert position_mode_label(False) == "one_way"
    assert position_mode_label(True) == "hedge"
    assert position_mode_label(None) is None


def test_ensure_one_way_rejects_hedge_mode() -> None:
    client = MagicMock()
    client.dual_side_position.return_value = True
    logs: list[str] = []
    with pytest.raises(HedgePositionModeError, match="hedge"):
        ensure_one_way_position_mode_before_open(client, emit=logs.append)


def test_ensure_one_way_allows_one_way_mode() -> None:
    client = MagicMock()
    client.dual_side_position.return_value = False
    logs: list[str] = []
    ensure_one_way_position_mode_before_open(client, emit=logs.append)
    assert any("one-way" in line for line in logs)


def test_read_dual_side_position_falls_back_to_account() -> None:
    from roll.binance_client import BinanceHTTPError

    client = MagicMock()
    client.dual_side_position.side_effect = BinanceHTTPError("endpoint unavailable")
    client.account.return_value = {"dualSidePosition": False}
    assert read_dual_side_position(client) is False
