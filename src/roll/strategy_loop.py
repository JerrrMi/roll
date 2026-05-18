"""策略主循环（首期：仅公有 REST + dry-run，绝不触发下单）。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from roll.binance_client import (
    BinanceCoinMClient,
    BinanceCoinMSignedClient,
    CoinMFuturesSymbol,
    InsufficientMonitorableSymbolsError,
    select_monitorable_coin_m_symbols,
)
from roll.logger import get_logger
from roll.market_data import parse_candidate_assets
from roll.offline_trend import evaluate_symbol_offline_public
from roll.risk import OpenEvaluation, RiskEngine, RiskLimits, Side, fixed_stop_price
from roll.trend_model import SignalSide, TrendModelParams, TrendSignal

LoggerFn = Callable[[str], None]


@dataclass(frozen=True)
class StrategyLoopParams:
    """run-loop 使用的可调参数（可由 YAML `strategy` 覆盖）。

    public_rest_base：若设置，run-loop 仅用该公有 REST host 拉 exchangeInfo/K 线/ticker；
    Live 模式下必须与 binance.rest_base 一致或为 None。
    trail_stop_fraction：>0 时启用基于极值的追踪 STOP（仍可被初始止损底价约束）。
    """

    loop_interval_sec: float = 60.0
    klines_limit: int = 600
    dry_run_equity: float = 10_000.0
    initial_leverage: int = 25
    stop_adverse_fraction: float = 0.05
    kelly_p: float = 0.55
    kelly_b: float = 1.2
    min_monitor_symbols: int = 3
    public_rest_base: str | None = None
    trail_stop_fraction: float | None = None


def parse_strategy_loop_params(settings: Mapping[str, Any]) -> StrategyLoopParams:
    raw = settings.get("strategy")
    if not isinstance(raw, dict):
        return StrategyLoopParams()
    def _f(key: str, default: float) -> float:
        v = raw.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        return default

    iv = raw.get("loop_interval_sec")
    interval = float(iv) if isinstance(iv, (int, float)) else StrategyLoopParams.loop_interval_sec

    kl = raw.get("klines_limit")
    klines_lim = int(kl) if isinstance(kl, int) and not isinstance(kl, bool) else StrategyLoopParams.klines_limit

    ms = raw.get("min_monitor_symbols")
    min_syms = int(ms) if isinstance(ms, int) and not isinstance(ms, bool) else StrategyLoopParams.min_monitor_symbols

    lev = raw.get("initial_leverage")
    leverage = int(lev) if isinstance(lev, int) and not isinstance(lev, bool) else StrategyLoopParams.initial_leverage

    prb = raw.get("public_rest_base")
    pub_rest: str | None = None
    if isinstance(prb, str):
        s = prb.strip()
        if s:
            pub_rest = s

    tr = raw.get("trail_stop_fraction")
    trail_frac: float | None = None
    if isinstance(tr, (int, float)) and not isinstance(tr, bool):
        tf = float(tr)
        trail_frac = tf if tf > 0 else None

    return StrategyLoopParams(
        loop_interval_sec=max(interval, 1.0),
        klines_limit=max(klines_lim, 50),
        dry_run_equity=max(_f("dry_run_equity", StrategyLoopParams.dry_run_equity), 1.0),
        initial_leverage=max(leverage, 1),
        stop_adverse_fraction=max(_f("stop_adverse_fraction", StrategyLoopParams.stop_adverse_fraction), 1e-6),
        kelly_p=_f("kelly_p", StrategyLoopParams.kelly_p),
        kelly_b=max(_f("kelly_b", StrategyLoopParams.kelly_b), 1e-6),
        min_monitor_symbols=max(min_syms, 3),
        public_rest_base=pub_rest,
        trail_stop_fraction=trail_frac,
    )


def intervals_from_settings(settings: Mapping[str, Any]) -> tuple[str, ...]:
    raw = settings.get("timeframes")
    if isinstance(raw, list) and raw:
        return tuple(str(x) for x in raw)
    return ("15m", "1h", "4h")


def rank_directional_signals(
    assessed: Sequence[tuple[str, TrendSignal]],
) -> list[tuple[str, TrendSignal]]:
    """按 |mixed_score| 降序排列的可交易方向候选（long / short）。"""
    directional: list[tuple[str, TrendSignal]] = []
    for sym, sig in assessed:
        if sig.side in (SignalSide.LONG, SignalSide.SHORT):
            directional.append((sym, sig))
    directional.sort(key=lambda item: abs(item[1].score), reverse=True)
    return directional


def signal_side_to_risk_side(sig: TrendSignal) -> Side | None:
    if sig.side == SignalSide.LONG:
        return "long"
    if sig.side == SignalSide.SHORT:
        return "short"
    return None


def _float_step(step_raw: str) -> float:
    if not step_raw or not step_raw.strip():
        return 0.0
    return float(step_raw)


def _fetch_symbol_price(client: BinanceCoinMClient, symbol: str) -> float:
    rows = client.ticker_price(symbol)
    if not rows:
        raise ValueError(f"ticker_price empty for {symbol!r}")
    px = rows[0].get("price") or rows[0].get("ps")
    if px is None:
        raise ValueError(f"ticker row missing price: {rows[0]!r}")
    return float(px)


def evaluate_candidates_with_public_rest(
    client: BinanceCoinMClient,
    specs: Sequence[CoinMFuturesSymbol],
    *,
    params: StrategyLoopParams,
    intervals: tuple[str, ...],
    trend_params: TrendModelParams | None = None,
) -> list[tuple[str, TrendSignal]]:
    """对已通过 exchangeInfo 筛选的一组合约逐个拉 K 线并评估趋势。"""
    from roll.trend_model import TrendModel

    outcomes: list[tuple[str, TrendSignal]] = []
    rb = client.config.rest_base
    pref = client.config.coin_m_prefix
    model = TrendModel(trend_params) if trend_params is not None else TrendModel()

    for spec in specs:
        sym = spec.symbol.upper()
        sig = evaluate_symbol_offline_public(
            sym,
            rest_base=rb,
            coin_m_prefix=pref,
            model=model,
            klines_limit=params.klines_limit,
            intervals=intervals,
        )
        outcomes.append((sym, sig))
    return outcomes


def try_select_single_symbol(
    *,
    ranked: Sequence[tuple[str, TrendSignal]],
    specs_by_symbol: Mapping[str, CoinMFuturesSymbol],
    client: BinanceCoinMClient,
    risk_engine: RiskEngine,
    params: StrategyLoopParams,
    ts: float,
    equity: float,
    emit: LoggerFn,
) -> tuple[str | None, Side | None, OpenEvaluation | None]:
    """按趋势强度从高到低依次尝试风控；首个通过的即为当日最优可行标的。"""
    for rank_idx, (sym, sig) in enumerate(ranked, start=1):
        side = signal_side_to_risk_side(sig)
        if side is None:
            continue
        spec = specs_by_symbol.get(sym.upper())
        if spec is None:
            emit(f"[risk_try rank={rank_idx}] symbol={sym} skip_missing_spec")
            continue
        try:
            entry = _fetch_symbol_price(client, sym)
        except (ValueError, OSError) as e:
            emit(f"[risk_try rank={rank_idx}] symbol={sym} price_error={type(e).__name__}:{e}")
            continue

        stop_px = fixed_stop_price(side, entry, params.stop_adverse_fraction)
        step_sz = _float_step(spec.market_step_size or spec.step_size)
        min_q_raw = spec.market_min_qty or spec.min_qty
        try:
            min_q = float(min_q_raw) if min_q_raw else 0.0
        except (TypeError, ValueError):
            min_q = 0.0

        oe = risk_engine.evaluate_open(
            ts=ts,
            equity=equity,
            entry_price=entry,
            stop_price=stop_px,
            side=side,
            p=params.kelly_p,
            b=params.kelly_b,
            quantity_step=step_sz,
            min_quantity=min_q,
            contract_multiplier=max(spec.contract_size, 1e-12),
        )
        if oe.allow:
            emit(
                f"[risk_try rank={rank_idx}] symbol={sym} side={side} PASS qty={oe.quantity:.8g} "
                f"entry={entry:.8g} stop={stop_px:.8g}"
            )
            return sym, side, oe
        emit(
            f"[risk_try rank={rank_idx}] symbol={sym} side={side} REJECT reasons="
            f"{'; '.join(oe.reasons)}"
        )
    return None, None, None


def run_strategy_iteration(
    *,
    settings: Mapping[str, Any],
    client: BinanceCoinMClient,
    params: StrategyLoopParams | None = None,
    emit: LoggerFn | None = None,
    dry_run: bool = True,
    intervals: tuple[str, ...] | None = None,
    trend_params: TrendModelParams | None = None,
    signed_client: BinanceCoinMSignedClient | None = None,
    position_manager=None,
    state_store=None,
    clear_entry_pause: bool = False,
) -> None:
    """单次扫描：候选趋势 → dry-run 打印或 Testnet Signed 自动交易闭环。"""
    log = get_logger("strategy_loop")
    out: LoggerFn = emit or log.info
    p = params or parse_strategy_loop_params(settings)
    iv = intervals if intervals is not None else intervals_from_settings(settings)

    if not dry_run:
        from roll.coinm_auto_trade import run_live_strategy_iteration
        from roll.position_manager import PositionManager
        from roll.state_store import StateStore as _RtStore

        if signed_client is None or not isinstance(signed_client, BinanceCoinMSignedClient):
            raise ValueError("live run_strategy_iteration 需要 BinanceCoinMSignedClient signed_client")
        if position_manager is None or not isinstance(position_manager, PositionManager):
            raise ValueError("live run_strategy_iteration 需要 PositionManager(position_manager)")
        if state_store is None or not isinstance(state_store, _RtStore):
            raise ValueError("live run_strategy_iteration 需要 StateStore(state_store)")
        run_live_strategy_iteration(
            settings=settings,
            signed_client=signed_client,
            position_manager=position_manager,
            state_store=state_store,
            params=p,
            emit=out,
            intervals=iv,
            trend_params=trend_params,
            clear_entry_pause=clear_entry_pause,
        )
        return

    limits = RiskLimits()

    candidates = parse_candidate_assets(dict(settings))
    client.sync_server_time()

    specs_full = client.list_coin_m_specs()
    try:
        matched, report = select_monitorable_coin_m_symbols(
            specs_full,
            candidates,
            min_count=p.min_monitor_symbols,
        )
    except InsufficientMonitorableSymbolsError as e:
        out(f"[iteration_abort] insufficient_symbols:\n{e}")
        raise

    out("[symbols_monitor_pool]")
    out(report.format_human_readable())

    assessed = evaluate_candidates_with_public_rest(
        client,
        matched,
        params=p,
        intervals=iv,
        trend_params=trend_params,
    )

    out("[trend_scan]")
    for sym, sig in assessed:
        rreason = "; ".join(sig.rejection_reasons) if sig.rejection_reasons else ""
        parts = [f"{tf}={sig.score_by_interval.get(tf, float('nan')):+.4f}" for tf in iv]
        out(
            f"  symbol={sym} signal={sig.side.value} mixed_score={sig.score:+.4f} "
            f"scores[{', '.join(parts)}]"
            + (f" trend_reject={rreason}" if rreason else "")
        )
        if sig.reasons:
            for line in sig.reasons[:12]:
                out(f"    detail {line}")
            if len(sig.reasons) > 12:
                out(f"    ... ({len(sig.reasons)} trend detail lines total)")

    ranked = rank_directional_signals(assessed)
    specs_map = {s.symbol.upper(): s for s in matched}

    ts = time.time()
    equity = p.dry_run_equity
    risk_engine = RiskEngine(limits=limits)

    out("[selection]")
    if not ranked:
        out("[dry-run] NO_OPEN — no directional trend signal among monitored symbols.")
        return

    for i, (sym, sig) in enumerate(ranked, start=1):
        out(f"  rank={i} symbol={sym} side={sig.side.value} |score|={abs(sig.score):.4f}")

    sym_pick, side_pick, oe_pick = try_select_single_symbol(
        ranked=ranked,
        specs_by_symbol=specs_map,
        client=client,
        risk_engine=risk_engine,
        params=p,
        ts=ts,
        equity=equity,
        emit=out,
    )

    if sym_pick is None or side_pick is None or oe_pick is None:
        out("[dry-run] NO_OPEN — all directional candidates failed risk checks.")
        return

    try:
        entry_live = _fetch_symbol_price(client, sym_pick)
    except (ValueError, OSError):
        entry_live = float("nan")
    stop_px = fixed_stop_price(
        side_pick,
        entry_live if entry_live == entry_live else 1.0,
        p.stop_adverse_fraction,
    )

    out("[dry-run][planned_open]")
    out(f"  action=MARKET_OPEN side={side_pick} symbol={sym_pick}")
    out(f"  leverage(initial,target)={p.initial_leverage}x (dry-run: no leverage REST)")
    out(f"  reference_price≈{entry_live:.8g} stop_price≈{stop_px:.8g} adverse_frac={p.stop_adverse_fraction}")
    out(f"  equity(simulated)={equity:.4f} quantity_contracts≈{oe_pick.quantity:.8g}")
    out(f"  eff_kelly_fraction≈{oe_pick.effective_kelly_fraction:.6f}")
    out("[dry-run] order_executor bypass — zero signed/order REST calls.")


def run_strategy_forever(
    *,
    settings: Mapping[str, Any],
    client: BinanceCoinMClient,
    dry_run: bool = True,
    params: StrategyLoopParams | None = None,
    signed_client: BinanceCoinMSignedClient | None = None,
    position_manager=None,
    state_store=None,
    clear_entry_pause_once: bool = False,
) -> None:
    from roll.position_manager import PositionManager

    p = params or parse_strategy_loop_params(settings)
    log = get_logger("strategy_loop")
    pm_eff = position_manager if position_manager is not None else PositionManager()
    first_clear_done = False

    log.info(
        "strategy loop starting dry_run=%s interval_sec=%.1f min_symbols=%d",
        dry_run,
        p.loop_interval_sec,
        p.min_monitor_symbols,
    )
    while True:
        ce = False
        if clear_entry_pause_once and not first_clear_done:
            ce = True
            first_clear_done = True
        try:
            run_strategy_iteration(
                settings=settings,
                client=client,
                params=p,
                dry_run=dry_run,
                signed_client=signed_client,
                position_manager=pm_eff,
                state_store=state_store,
                clear_entry_pause=ce,
            )
        except InsufficientMonitorableSymbolsError:
            log.warning("iteration aborted for insufficient symbols; retry after interval")
        except Exception:
            log.exception("iteration failed — sleep and retry")
        time.sleep(p.loop_interval_sec)
