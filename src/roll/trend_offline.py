"""离线趋势评估工具：仅从 Binance COIN-M 公共接口拉历史 K 线并运行 TrendModel。

不发送订单、不涉及签名私有接口。
"""

from __future__ import annotations

import argparse
from typing import Mapping, Sequence

from roll.binance_client import BinanceClientConfig, BinanceCoinMClient
from roll.trend_model import Candle, TrendModel, TrendModelParams, parse_binance_klines


DEFAULT_LIMIT_PER_TF = 500


def load_klines_three_tf(
    client: BinanceCoinMClient,
    symbol: str,
    *,
    limit: int = DEFAULT_LIMIT_PER_TF,
) -> Mapping[str, list[Candle]]:
    out: dict[str, list[Candle]] = {}
    for iv in ("15m", "1h", "4h"):
        rows = client.klines(symbol, iv, limit=int(limit))
        out[iv] = parse_binance_klines(rows)
    return out


def run_public_offline_evaluation(
    symbol: str,
    *,
    rest_base: str | None,
    coin_m_prefix: str | None,
    limit: int = DEFAULT_LIMIT_PER_TF,
    params: TrendModelParams | None = None,
) -> str:
    """拉取三节 K 线并返回多行可读报告（中文）。"""
    cfg_kw: dict[str, object] = {}
    if rest_base:
        cfg_kw["rest_base"] = rest_base
    if coin_m_prefix:
        cfg_kw["coin_m_prefix"] = coin_m_prefix
    client = BinanceCoinMClient(BinanceClientConfig(**cfg_kw))

    bundles = load_klines_three_tf(client, symbol, limit=int(limit))
    model = TrendModel(params)
    sig = model.evaluate(bundles)

    lines: list[str] = [
        f"symbol={symbol}",
        f"side={sig.side.value} mixed_score={sig.score:.4f}",
        f"per_tf: " + ", ".join(f"{k}={v:+.4f}" for k, v in sorted(sig.score_by_interval.items())),
        "",
        "--- explanations ---",
    ]
    lines.extend(sig.reasons)
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="离线趋势评分（公有 K 线，不下单）")
    p.add_argument("symbol", help="COIN-M symbol，例如 BTCUSD_PERP")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT_PER_TF, help="每周期最多 K 线条数（默认500）")
    p.add_argument(
        "--rest-base",
        type=str,
        default=None,
        help="REST 根域名，省略则沿用 BinanceCoinMClient 默认 Testnet",
    )
    p.add_argument(
        "--coin-m-prefix",
        type=str,
        default=None,
        help="COIN-M REST 前缀，默认 /dapi/v1",
    )
    return p


def offline_main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    text = run_public_offline_evaluation(
        args.symbol,
        rest_base=args.rest_base,
        coin_m_prefix=args.coin_m_prefix,
        limit=args.limit,
    )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(offline_main())

