"""应用入口：加载配置；支持 `trend-offline` 子命令离线评估趋势（仅公共 K 线）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _cmd_trend_offline(project_root: Path, argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="python -m main trend-offline",
        description=(
            "从 Binance COIN-M 公共 REST 拉取历史 K 线，计算多周期趋势评分；"
            "不下单、不签名。"
        ),
    )
    p.add_argument(
        "--config",
        type=Path,
        default=project_root / "config/settings.yaml",
        help="用于读取 binance.rest_base / coin_m_prefix（默认同项目根路径）",
    )
    p.add_argument(
        "--symbol",
        required=True,
        help="合约 symbol，例如 DOGEUSD_PERP",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=600,
        help="klines limit（每周期）",
    )
    args_local = p.parse_args(argv)

    import yaml

    from roll.offline_trend import evaluate_symbol_offline_public, settings_to_offline_urls

    cfg_path = Path(args_local.config)
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        settings = raw if isinstance(raw, dict) else {}
    else:
        settings = {}
    rb, pref = settings_to_offline_urls(settings)

    sig = evaluate_symbol_offline_public(
        args_local.symbol.upper(),
        rest_base=rb,
        coin_m_prefix=pref,
        klines_limit=int(args_local.limit),
    )

    print(f"symbol={args_local.symbol.upper()} REST={rb}")
    print(f"signal={sig.side.value} mixed_score={sig.score:.4f}")
    joined = "".join(f" {tf}={sig.score_by_interval.get(tf, float('nan')):+.4f}" for tf in ("4h", "1h", "15m"))
    print(f"scores_by_tf:{joined}")
    for line in sig.reasons:
        print(line)
    return 0


def _cmd_coinm_signed_smoke(project_root: Path, argv: list[str]) -> int:
    import argparse
    import os

    import yaml

    from roll.coinm_signed_testnet import run_signed_testnet_acceptance

    ap = argparse.ArgumentParser(
        prog="python -m main coinm-signed-smoke",
        description=(
            "Binance COIN-M Futures **Testnet ONLY** Signed API 验收；"
            "API Key / Secret 从环境变量 BINANCE_API_KEY / BINANCE_API_SECRET 读取（禁止打印 Secret）。"
        ),
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=project_root / "config/settings.yaml",
        help="读取 binance.rest_base / recv_window_ms / coin_m_prefix",
    )
    ap.add_argument(
        "--symbol",
        required=True,
        help="合约例如 DOGEUSD_PERP（需与 exchangeInfo 一致）",
    )
    ap.add_argument(
        "--leverage",
        type=int,
        default=5,
        help="验收用杠杆倍数（写入 POST /leverage）",
    )
    args_sm = ap.parse_args(argv)

    cfg_path = args_sm.config
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        settings = raw if isinstance(raw, dict) else {}
    else:
        settings = {}

    b = settings.get("binance", {}) if isinstance(settings.get("binance"), dict) else {}
    rest_base = str(b.get("rest_base", "https://testnet.binancefuture.com"))
    coin_m_prefix = str(b.get("coin_m_prefix", "/dapi/v1"))
    recv_window_ms = int(b.get("recv_window_ms", 5000))

    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")

    outcome = run_signed_testnet_acceptance(
        rest_base=rest_base,
        coin_m_prefix=coin_m_prefix,
        recv_window_ms=recv_window_ms,
        symbol=args_sm.symbol,
        leverage_to_set=args_sm.leverage,
        api_key=api_key,
        api_secret=api_secret,
        report=print,
    )

    failures = [r for r in outcome if not r.ok]
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    project_root = Path(__file__).resolve().parent.parent

    if argv[:1] == ["trend-offline"]:
        return _cmd_trend_offline(project_root, argv[1:])

    if argv[:1] == ["coinm-signed-smoke"]:
        return _cmd_coinm_signed_smoke(project_root, argv[1:])

    parser = argparse.ArgumentParser(description="Binance COIN-M 滚仓系统入口")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/settings.yaml"),
        help="配置文件路径（默认可复制 settings.example.yaml）",
    )
    args = parser.parse_args(argv)

    import yaml

    from roll.logger import get_logger

    log = get_logger("main")
    cfg_path = args.config if args.config.is_absolute() else project_root / args.config

    if not cfg_path.is_file():
        log.warning("配置文件不存在: %s — 请复制 config/settings.example.yaml", cfg_path)
        return 0

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    settings = raw if isinstance(raw, dict) else {}
    log.info(
        "已加载配置 environment=%s rest_base=%s",
        settings.get("environment"),
        settings.get("binance", {}).get("rest_base"),
    )
    log.info("运行 `python -m main trend-offline --symbol YOUR_PERP_SYMBOL` 可离线验收趋势模型。")
    log.info(
        "Testnet Signed 验收：设置 BINANCE_* 环境变量后执行 "
        "`python -m main coinm-signed-smoke --symbol YOUR_PERP`。",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
