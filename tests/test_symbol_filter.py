"""候选标的配置解析（骨架；exchangeInfo 筛选在后续阶段）。"""

from roll.market_data import parse_candidate_assets


def test_parse_candidate_assets() -> None:
    cfg = {"candidates": ["DOGE", "AVAX", "SHIB"]}
    assert parse_candidate_assets(cfg) == ["DOGE", "AVAX", "SHIB"]


def test_parse_candidate_assets_empty() -> None:
    assert parse_candidate_assets({}) == []
