"""binance 配置解析与 USD-M 产品守卫。"""

from __future__ import annotations

import pytest

from roll.binance_config import BinanceConfigError, assert_usdm_api_prefix, parse_binance_settings


def test_parse_defaults_usdm_testnet() -> None:
    bcfg = parse_binance_settings({})
    assert bcfg.product == "usdm"
    assert bcfg.rest_base == "https://testnet.binancefuture.com"
    assert bcfg.api_prefix == "/fapi/v1"


def test_parse_testnet_example_shape() -> None:
    bcfg = parse_binance_settings(
        {
            "binance": {
                "product": "usdm",
                "rest_base": "https://testnet.binancefuture.com",
                "api_prefix": "/fapi/v1",
            }
        }
    )
    assert bcfg.product == "usdm"
    assert bcfg.api_prefix == "/fapi/v1"


def test_parse_live_example_shape() -> None:
    bcfg = parse_binance_settings(
        {
            "binance": {
                "product": "usdm",
                "rest_base": "https://fapi.binance.com",
                "api_prefix": "/fapi/v1",
            }
        }
    )
    assert bcfg.rest_base == "https://fapi.binance.com"


def test_usd_m_prefix_alias() -> None:
    bcfg = parse_binance_settings({"binance": {"usd_m_prefix": "/fapi/v1"}})
    assert bcfg.api_prefix == "/fapi/v1"


def test_rejects_coin_m_prefix() -> None:
    with pytest.raises(BinanceConfigError, match="coin_m_prefix"):
        parse_binance_settings({"binance": {"coin_m_prefix": "/dapi/v1"}})


def test_rejects_dapi_api_prefix() -> None:
    with pytest.raises(BinanceConfigError, match="dapi"):
        parse_binance_settings({"binance": {"api_prefix": "/dapi/v1"}})


def test_rejects_dapi_live_rest_base() -> None:
    with pytest.raises(BinanceConfigError, match="dapi.binance.com"):
        parse_binance_settings(
            {"binance": {"rest_base": "https://dapi.binance.com", "api_prefix": "/fapi/v1"}}
        )


def test_assert_usdm_api_prefix_rejects_dapi() -> None:
    with pytest.raises(BinanceConfigError, match="dapi"):
        assert_usdm_api_prefix("/dapi/v1")


def test_example_yaml_files_use_fapi_not_dapi() -> None:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    for rel in (
        "config/settings.example.yaml",
        "config/settings.testnet.example.yaml",
        "config/settings.live.example.yaml",
    ):
        raw = yaml.safe_load((root / rel).read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        b = raw.get("binance")
        assert isinstance(b, dict), rel
        assert b.get("product") == "usdm", rel
        assert b.get("api_prefix") == "/fapi/v1", rel
        assert "coin_m_prefix" not in b, rel
        text = (root / rel).read_text(encoding="utf-8")
        assert "dapi.binance.com" not in text, rel
        assert "/dapi/v1" not in text, rel
