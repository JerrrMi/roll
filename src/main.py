"""应用入口：加载配置；支持 `run-loop`（dry-run 策略循环）、`trend-offline`、Signed 验收、`reconcile-state`。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from roll.state_store import StateStore


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
    joined = "".join(
        f" {tf}={sig.score_by_interval.get(tf, float('nan')):+.4f}" for tf in ("4h", "1h", "15m")
    )
    print(f"scores_by_tf:{joined}")
    for line in sig.reasons:
        print(line)
    return 0


def _cmd_coinm_signed_smoke(project_root: Path, argv: list[str]) -> int:
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


def _resolve_cfg_path(cfg: Path, project_root: Path) -> Path:
    return cfg if cfg.is_absolute() else project_root / cfg


def _load_settings_yaml(cfg_path: Path) -> dict:
    import yaml

    if not cfg_path.is_file():
        return {}
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _state_store_from_settings(settings: dict) -> StateStore:
    """根据 settings.state 构造 StateStore。sqlite backend 暂未实现时改写为并行 JSON path。"""

    from roll.state_store import StateStore

    raw = settings.get("state")
    blob = raw if isinstance(raw, dict) else {}
    backend = str(blob.get("backend", "memory")).lower()
    path_raw = blob.get("path")

    path: Path | None = Path(str(path_raw)) if path_raw else None
    resolved_backend = backend

    if backend == "sqlite" and path is not None:
        resolved_backend = "json"
        path = path.with_suffix(".json")

    if resolved_backend != "json":
        path = None

    return StateStore(backend=resolved_backend, path=path)


def _apply_logging_from_settings(settings: dict) -> None:
    import logging

    raw = settings.get("logging")
    if not isinstance(raw, dict):
        return
    name = str(raw.get("level", "INFO")).upper()
    level = getattr(logging, name, logging.INFO)
    logging.getLogger().setLevel(level)


def _cmd_run_loop(project_root: Path, argv: list[str]) -> int:
    """策略主循环：默认 dry-run，仅公有 REST + 打印计划动作，不下单。"""

    from roll.binance_client import BinanceClientConfig, BinanceCoinMClient, InsufficientMonitorableSymbolsError
    from roll.logger import get_logger
    from roll.strategy_loop import (
        parse_strategy_loop_params,
        run_strategy_forever,
        run_strategy_iteration,
    )

    ap = argparse.ArgumentParser(
        prog="python -m main run-loop",
        description=(
            "多候选扫描 → 趋势评分 → 风控择优 → 单标的 dry-run 输出。"
            "不进行签名请求与下单。"
        ),
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=project_root / "config/settings.yaml",
        help="读取 binance / candidates / strategy / logging",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮迭代后退出（验收用）",
    )
    ap.add_argument(
        "--interval-sec",
        type=float,
        default=None,
        help="覆盖配置 strategy.loop_interval_sec（仅循环模式）",
    )
    ap.add_argument(
        "--no-dry-run",
        action="store_true",
        help="首期不支持：若指定则立即退出（防止误触发真实交易）。",
    )
    args_loop = ap.parse_args(argv)

    if args_loop.no_dry_run:
        print(
            "[run-loop] 首期仅实现 dry-run；去掉 --no-dry-run 或使用默认 dry-run。",
            file=sys.stderr,
        )
        return 2

    cfg_path = _resolve_cfg_path(Path(args_loop.config), project_root)
    settings = _load_settings_yaml(cfg_path)
    if not settings:
        print(f"[run-loop] 配置不存在或为空: {cfg_path}", file=sys.stderr)
        return 2

    _apply_logging_from_settings(settings)
    log = get_logger("main.run_loop")

    params = parse_strategy_loop_params(settings)
    if args_loop.interval_sec is not None:
        from dataclasses import replace

        params = replace(params, loop_interval_sec=max(float(args_loop.interval_sec), 1.0))

    b = settings.get("binance", {}) if isinstance(settings.get("binance"), dict) else {}
    rest_base = str(b.get("rest_base", "https://testnet.binancefuture.com"))
    if params.public_rest_base:
        rest_base = params.public_rest_base
        log.info("strategy.public_rest_base overrides market REST host (read-only): %s", rest_base)
    coin_m_prefix = str(b.get("coin_m_prefix", "/dapi/v1"))
    recv_window_ms = int(b.get("recv_window_ms", 5000))

    client = BinanceCoinMClient(
        BinanceClientConfig(
            rest_base=rest_base,
            coin_m_prefix=coin_m_prefix,
            recv_window_ms=recv_window_ms,
        ),
    )

    log.info("run-loop start dry_run=True rest_base=%s once=%s", rest_base, args_loop.once)
    if args_loop.once:
        try:
            run_strategy_iteration(settings=settings, client=client, params=params, dry_run=True)
        except InsufficientMonitorableSymbolsError:
            log.warning(
                "run-loop stopped: fewer than min_monitor_symbols matched on this venue "
                "(try adding exact baseAsset codes from exchangeInfo or set strategy.public_rest_base)."
            )
            return 3
        return 0

    run_strategy_forever(settings=settings, client=client, dry_run=True, params=params)
    return 0


def _cmd_reconcile_state(project_root: Path, argv: list[str]) -> int:
    from roll.binance_client import BinanceClientConfig, BinanceCoinMSignedClient, is_binance_coin_m_testnet_url
    from roll.position_manager import bootstrap_position_manager_from_exchange_client
    from roll.state_store import RuntimeState

    ap = argparse.ArgumentParser(
        prog="python -m main reconcile-state",
        description=(
            "Binance COIN-M **Testnet** 启动对账：拉取全局 positionRisk + openOrders，"
            "以交易所快照恢复单标的交易锁；检测到多标的持仓/跨标的挂单即挂起自动交易。"
        ),
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=project_root / "config/settings.yaml",
        help="读取 binance/rest_base/state 等字段",
    )
    ap.add_argument(
        "--no-save",
        action="store_true",
        help="仅打印对账结果，不写本地 state 文件",
    )
    args_r = ap.parse_args(argv)

    cfg_path = _resolve_cfg_path(Path(args_r.config), project_root)
    settings = _load_settings_yaml(cfg_path)
    if not settings:
        print(f"[reconcile-state] 配置不存在或为空: {cfg_path}", file=sys.stderr)
        return 2

    b = settings.get("binance", {}) if isinstance(settings.get("binance"), dict) else {}
    rest_base = str(b.get("rest_base", "https://testnet.binancefuture.com"))
    coin_m_prefix = str(b.get("coin_m_prefix", "/dapi/v1"))
    recv_window_ms = int(b.get("recv_window_ms", 5000))

    if not is_binance_coin_m_testnet_url(rest_base):
        print(
            f"[reconcile-state] rest_base={rest_base!r} 非官方 Futures Testnet host；"
            "本命令仅允许 testnet.binancefuture.com（见 is_binance_coin_m_testnet_url）。",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    ck = api_key.strip() if isinstance(api_key, str) else None
    cs = api_secret.strip() if isinstance(api_secret, str) else None
    if not ck or not cs:
        print(
            "[reconcile-state] 需要环境变量 BINANCE_API_KEY / BINANCE_API_SECRET（禁止打印 Secret）。",
            file=sys.stderr,
        )
        return 2

    cfg = BinanceClientConfig(
        rest_base=rest_base,
        coin_m_prefix=coin_m_prefix,
        recv_window_ms=recv_window_ms,
        api_key=ck,
        api_secret=cs,
    )
    client = BinanceCoinMSignedClient(cfg)

    _, outcome, (sync_ms, n_pos, n_ord) = bootstrap_position_manager_from_exchange_client(client)

    print(f"exchange_rest_base={rest_base}")
    print(f"sync_server_offset_ms≈{sync_ms}")
    print(f"positionRisk_rows={n_pos} openOrders_rows={n_ord}")
    print(f"nonzero_position_symbols={sorted(outcome.position_symbols)}")
    print(f"symbols_with_open_orders={sorted(outcome.order_symbols)}")
    print(
        "reconcile="
        f"trade_lock_state={outcome.lock_state.value} "
        f"active_symbol={outcome.active_symbol!r} "
        f"halt={outcome.halt_automatic_trading}"
    )
    if outcome.halt_reason:
        print(f"halt_reason={outcome.halt_reason}")

    if args_r.no_save:
        return 1 if outcome.halt_automatic_trading else 0

    store = _state_store_from_settings(settings)
    persisted = RuntimeState(
        trade_lock_state=outcome.lock_state.value,
        active_symbol=outcome.active_symbol,
        halt_automatic_trading=outcome.halt_automatic_trading,
        halt_reason=outcome.halt_reason,
    )
    store.save(persisted)
    if getattr(store, "path", None) is not None:
        print(f"saved_state_json={store.path}")
    else:
        print("[reconcile-state] state.backend!=json — 会话锁仅保存在内存占位 store 内。")
    return 1 if outcome.halt_automatic_trading else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    project_root = Path(__file__).resolve().parent.parent

    if argv[:1] == ["trend-offline"]:
        return _cmd_trend_offline(project_root, argv[1:])

    if argv[:1] == ["coinm-signed-smoke"]:
        return _cmd_coinm_signed_smoke(project_root, argv[1:])

    if argv[:1] == ["reconcile-state"]:
        return _cmd_reconcile_state(project_root, argv[1:])

    if argv[:1] == ["run-loop"]:
        return _cmd_run_loop(project_root, argv[1:])

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
    cfg_path = _resolve_cfg_path(args.config, project_root)

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
    log.info(
        "自动交易前先执行（Testnet、`BINANCE_*` 已就绪时）"
        " `python -m main reconcile-state` 以对账持仓与挂单并恢复全局单标的锁。"
    )
    log.info(
        "策略 dry-run 主循环：`python -m main run-loop --once`（默认公有 REST，不下单）。"
    )
    log.info("运行 `python -m main trend-offline --symbol YOUR_PERP_SYMBOL` 可离线验收趋势模型。")
    log.info(
        "Testnet Signed 验收：设置 BINANCE_* 环境变量后执行 "
        "`python -m main coinm-signed-smoke --symbol YOUR_PERP`。",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())