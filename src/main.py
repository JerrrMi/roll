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


def _cmd_backtest(project_root: Path, argv: list[str]) -> int:
    import argparse
    import time

    from roll.backtest import (
        BacktestConfig,
        load_backtest_data,
        parse_risk_limits_settings,
        parse_trend_model_params,
        print_backtest_report,
        print_sensitivity_report,
        run_backtest,
        run_parameter_sensitivity,
        summarize_sensitivity,
    )
    from roll.binance_client import (
        BinanceClientConfig,
        BinanceCoinMClient,
        InsufficientMonitorableSymbolsError,
        select_monitorable_coin_m_symbols,
    )
    from roll.market_data import parse_candidate_assets
    from roll.risk import RiskLimits
    from roll.strategy_loop import intervals_from_settings, parse_strategy_loop_params

    ap = argparse.ArgumentParser(
        prog="python -m main backtest",
        description=(
            "历史回测：公有 K 线 + 趋势模型 + 止损/追踪止损 + 手续费/滑点；"
            "可选 --sensitivity 做参数敏感性扫描。默认使用实盘公共 REST 以拉长样本区间。"
        ),
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=project_root / "config/settings.yaml",
        help="读取 candidates / strategy / trend_model / risk / binance.coin_m_prefix",
    )
    ap.add_argument("--days", type=float, default=180.0, help="回测区间长度（天，自当前 UTC 往前推）")
    ap.add_argument(
        "--public-rest",
        type=str,
        default="https://dapi.binance.com",
        help="公共 REST base（默认实盘 dapi；可用 settings.strategy.public_rest_base 覆盖）",
    )
    ap.add_argument("--fee-bps", type=float, default=5.0, help="每边 taker 手续费（基点），往返自动 ×2 计入")
    ap.add_argument("--slippage-bps", type=float, default=2.0, help="买卖滑点（基点，不利方向）")
    ap.add_argument("--initial-equity", type=float, default=10_000.0)
    ap.add_argument(
        "--sensitivity",
        action="store_true",
        help="对 trend_threshold、min_adx、stop、kelly_extra_multiplier、max_position_fraction 做单参数扫描",
    )
    ap.add_argument("--min-symbols", type=int, default=None, help="覆盖 strategy.min_monitor_symbols（默认用配置）")
    args_bt = ap.parse_args(argv)

    cfg_path = _resolve_cfg_path(Path(args_bt.config), project_root)
    settings = _load_settings_yaml(cfg_path)
    if not settings:
        print(f"[backtest] 配置不存在或为空: {cfg_path}", file=sys.stderr)
        return 2

    strat = parse_strategy_loop_params(settings)
    intervals = intervals_from_settings(settings)
    trend_params = parse_trend_model_params(settings)
    risk_overlay = parse_risk_limits_settings(settings)
    limits_eff = risk_overlay or RiskLimits()

    min_sy = args_bt.min_symbols if args_bt.min_symbols is not None else strat.min_monitor_symbols

    pub = str(args_bt.public_rest).strip()
    prb = strat.public_rest_base
    if isinstance(prb, str) and prb.strip():
        pub = prb.strip()

    b = settings.get("binance", {}) if isinstance(settings.get("binance"), dict) else {}
    coin_m_prefix = str(b.get("coin_m_prefix", "/dapi/v1"))

    client = BinanceCoinMClient(
        BinanceClientConfig(rest_base=pub.rstrip("/"), coin_m_prefix=coin_m_prefix),
    )
    client.sync_server_time()

    candidates = parse_candidate_assets(dict(settings))
    specs_full = client.list_coin_m_specs()
    try:
        matched, report = select_monitorable_coin_m_symbols(specs_full, candidates, min_count=min_sy)
    except InsufficientMonitorableSymbolsError as e:
        print(f"[backtest] 可监测标的不足:\n{e.report.format_human_readable()}", file=sys.stderr)
        return 3

    print(f"[backtest] public_rest={pub} matched={[s.symbol for s in matched]}")
    print(report.format_human_readable())

    end_ms = int(time.time() * 1000)
    ms_per_day = 86400000
    start_ms = end_ms - int(float(args_bt.days) * ms_per_day)

    bt_cfg = BacktestConfig(
        initial_equity=float(args_bt.initial_equity),
        fee_rate=float(args_bt.fee_bps) / 10_000.0,
        slippage_bps=float(args_bt.slippage_bps),
        risk_limits=limits_eff,
    )

    data, axis = load_backtest_data(
        client,
        matched,
        start_ms=start_ms,
        end_ms=end_ms,
        warmup_extra_days=bt_cfg.warmup_extra_days,
    )
    if len(axis) < 50:
        print(
            f"[backtest] 公共时间轴过短 bars={len(axis)}；"
            "可能部分候选无重叠历史，可减少 candidates 或缩短区间。",
            file=sys.stderr,
        )
        return 4

    trail = strat.trail_stop_fraction
    res = run_backtest(
        settings=settings,
        client=client,
        matched=matched,
        data=data,
        base_timeline=axis,
        strat_params=strat,
        trend_params=trend_params,
        config=bt_cfg,
        intervals=intervals,
        trail_stop_fraction=trail,
    )
    print_backtest_report(res)
    if args_bt.sensitivity:
        rows = run_parameter_sensitivity(
            settings=settings,
            client=client,
            matched=matched,
            data=data,
            base_timeline=axis,
            strat_params=strat,
            trend_params=trend_params,
            config=bt_cfg,
            trail_stop_fraction=trail,
        )
        summ = summarize_sensitivity(rows)
        print_sensitivity_report(rows, summ)
    return 0


def _cmd_run_loop(project_root: Path, argv: list[str]) -> int:
    """策略主循环：默认 dry-run；`--no-dry-run` 且 binance.rest_base 为 Futures Testnet 时 Signed 自动交易闭环。"""

    from roll.binance_client import BinanceClientConfig, BinanceCoinMClient, InsufficientMonitorableSymbolsError
    from roll.logger import get_logger
    from roll.strategy_loop import (
        parse_strategy_loop_params,
        run_strategy_forever,
        run_strategy_iteration,
    )
    from roll.state_store import RuntimeState

    ap = argparse.ArgumentParser(
        prog="python -m main run-loop",
        description=(
            "多候选扫描 → 趋势评分 → 风控择优 → 默认 dry-run；"
            "加 --no-dry-run（仅官方 Futures Testnet + BINANCE_*）则 Signed 闭环自动交易。"
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
        help="COIN-M Futures **Testnet** 自动下单（REST 必须 testnet.binancefuture.com）；需 BINANCE_API_KEY / BINANCE_API_SECRET。",
    )
    ap.add_argument(
        "--clear-entry-pause",
        action="store_true",
        help="下一轮迭代开始前解除 persisted 开仓暂停标记（不写 API；仅清除本地 pause_new_positions）。",
    )
    args_loop = ap.parse_args(argv)
    cfg_path = _resolve_cfg_path(Path(args_loop.config), project_root)
    settings = _load_settings_yaml(cfg_path)
    if not settings:
        print(f"[run-loop] 配置不存在或为空: {cfg_path}", file=sys.stderr)
        return 2

    env_raw = str(settings.get("environment", "testnet") or "").strip().lower()
    if env_raw not in {"", "testnet"} and args_loop.no_dry_run:
        print("[run-loop] --no-dry-run 仅在 environment=testnet（或未设置）时使用。", file=sys.stderr)
        return 2

    _apply_logging_from_settings(settings)
    log = get_logger("main.run_loop")

    params = parse_strategy_loop_params(settings)
    if args_loop.interval_sec is not None:
        from dataclasses import replace

        params = replace(params, loop_interval_sec=max(float(args_loop.interval_sec), 1.0))

    b = settings.get("binance", {}) if isinstance(settings.get("binance"), dict) else {}
    rest_base_signed = str(b.get("rest_base", "https://testnet.binancefuture.com"))
    coin_m_prefix = str(b.get("coin_m_prefix", "/dapi/v1"))
    recv_window_ms = int(b.get("recv_window_ms", 5000))

    rest_public = rest_base_signed
    if params.public_rest_base:
        rest_public = str(params.public_rest_base).strip()
        log.info("strategy.public_rest_base overrides market REST (read/K 线/ticker): %s", rest_public)

    api_key_env = os.environ.get("BINANCE_API_KEY")
    api_secret_env = os.environ.get("BINANCE_API_SECRET")
    api_key_ck = api_key_env.strip() if isinstance(api_key_env, str) else ""
    api_secret_cs = api_secret_env.strip() if isinstance(api_secret_env, str) else ""

    dry_run = not args_loop.no_dry_run
    signed_client_live = None
    pm_for_live = None
    store_live = None

    if args_loop.no_dry_run:
        from roll.binance_client import BinanceCoinMSignedClient, is_binance_coin_m_testnet_url
        from roll.position_manager import PositionManager
        from roll.position_manager import reconcile_coin_m_account

        if not is_binance_coin_m_testnet_url(rest_base_signed):
            print(f"[run-loop] --no-dry-run 被拒绝：binance.rest_base={rest_base_signed!r} 非 Futures Testnet host。", file=sys.stderr)
            return 2
        if not api_key_ck or not api_secret_cs:
            print("[run-loop] --no-dry-run 需要环境变量 BINANCE_API_KEY / BINANCE_API_SECRET。", file=sys.stderr)
            return 2
        if params.public_rest_base:
            pu = rest_public.rstrip("/").lower()
            su = rest_base_signed.rstrip("/").lower()
            if pu != su:
                print("[run-loop] 自动交易中 strategy.public_rest_base 必须与 binance.rest_base 相同或删除该字段。", file=sys.stderr)
                return 2

        cfg_s = BinanceClientConfig(
            rest_base=rest_base_signed,
            coin_m_prefix=coin_m_prefix,
            recv_window_ms=recv_window_ms,
            api_key=api_key_ck,
            api_secret=api_secret_cs,
        )
        signed_client_live = BinanceCoinMSignedClient(cfg_s)

        pm_for_live = PositionManager()
        store_live = _state_store_from_settings(settings)
        persisted_early = store_live.load()

        if args_loop.clear_entry_pause:
            persisted_early.pause_new_positions = False
            persisted_early.pause_new_positions_reason = None
            store_live.save(persisted_early)

        elif persisted_early.pause_new_positions:
            rs = persisted_early.pause_new_positions_reason or "persisted"
            pm_for_live.pause_opening_entries(str(rs))

        signed_client_live.sync_server_time()
        reconcile_outcome = reconcile_coin_m_account(signed_client_live.position_risk(), signed_client_live.open_orders())
        pm_for_live.restore_from_exchange(reconcile_outcome)
        snap_pm = pm_for_live.snapshot_dict()
        stored_after_reconcile = store_live.load()
        store_live.save(
            RuntimeState(
                trade_lock_state=reconcile_outcome.lock_state.value,
                active_symbol=reconcile_outcome.active_symbol,
                halt_automatic_trading=reconcile_outcome.halt_automatic_trading,
                halt_reason=reconcile_outcome.halt_reason,
                pause_new_positions=bool(snap_pm.get("pause_new_positions")),
                pause_new_positions_reason=(
                    str(snap_pm["pause_new_reason"]) if isinstance(snap_pm.get("pause_new_reason"), str) else None
                ),
                cooldown_until_unix_ms=stored_after_reconcile.cooldown_until_unix_ms,
                last_signal=stored_after_reconcile.last_signal if isinstance(stored_after_reconcile.last_signal, dict) else {},
            )
        )

    client = BinanceCoinMClient(
        BinanceClientConfig(
            rest_base=rest_public,
            coin_m_prefix=coin_m_prefix,
            recv_window_ms=recv_window_ms,
        ),
    )

    log.info(
        "run-loop start dry_run=%s signed_rest_base=%s public_rest_base=%s once=%s",
        dry_run,
        rest_base_signed,
        rest_public,
        args_loop.once,
    )

    exec_client_for_loop = client if dry_run else signed_client_live

    if args_loop.once:
        try:
            run_strategy_iteration(
                settings=settings,
                client=exec_client_for_loop,
                params=params,
                dry_run=dry_run,
                signed_client=signed_client_live,
                position_manager=pm_for_live,
                state_store=store_live,
                clear_entry_pause=bool(args_loop.clear_entry_pause and args_loop.no_dry_run),
            )
        except InsufficientMonitorableSymbolsError:
            log.warning(
                "run-loop stopped: fewer than min_monitor_symbols matched on this venue "
                "(try adding exact baseAsset codes from exchangeInfo or set strategy.public_rest_base)."
            )
            return 3
        return 0

    run_strategy_forever(
        settings=settings,
        client=exec_client_for_loop,
        dry_run=dry_run,
        params=params,
        signed_client=signed_client_live,
        position_manager=pm_for_live,
        state_store=store_live,
        clear_entry_pause_once=bool(args_loop.clear_entry_pause and args_loop.no_dry_run),
    )
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
        pause_new_positions=False,
        pause_new_positions_reason=None,
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

    if argv[:1] == ["backtest"]:
        return _cmd_backtest(project_root, argv[1:])

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
    log.info("历史回测与参数校准：`conda activate roll-env` 后 `python -m main backtest --days 180`")
    log.info("运行 `python -m main trend-offline --symbol YOUR_PERP_SYMBOL` 可离线验收趋势模型。")
    log.info(
        "Testnet Signed 验收：设置 BINANCE_* 环境变量后执行 "
        "`python -m main coinm-signed-smoke --symbol YOUR_PERP`。",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())