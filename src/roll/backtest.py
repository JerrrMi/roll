"""历史回测：Binance USD-M K 线、趋势模型、止损/追踪止损、手续费、滑点、单标的全局持仓。

纯公有 REST；默认可用实盘 `fapi.binance.com` 拉 K 线（Testnet 历史往往较短）。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from roll.binance_client import BinanceUsdmClient, UsdMFuturesSymbol
from roll.coinm_auto_trade import desired_protective_stop_price, should_exit_from_trend
from roll.position_roll import has_float_profit, trend_allows_add
from roll.risk import (
    KellyVariant,
    RiskEngine,
    RiskLimits,
    Side,
    USDM_LINEAR_CONTRACT_MULTIPLIER,
    fixed_stop_price,
    linear_pnl_usdt,
    parse_risk_limits_settings,
)
from roll.usdm_account import parse_min_notional_usdt, usdm_linear_contract_multiplier
from roll.strategy_loop import (
    rank_directional_signals,
    signal_side_to_risk_side,
    intervals_from_settings,
    StrategyLoopParams,
)
from roll.trend_model import (
    Candle,
    TrendModel,
    TrendModelParams,
    TrendSignal,
    parse_binance_klines,
    wilder_atr_last,
)
from roll.usdm_auto_trade import should_exit_max_hold

TF_MS: dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


def _close_time_ms(c: Candle, interval: str) -> int:
    return c.open_time_ms + TF_MS[interval]


def _end_index_closed(candles: Sequence[Candle], interval: str, t_close_ms: int) -> int:
    """Returns last index i such that candle i's close_time <= t_close_ms, or -1."""
    if not candles:
        return -1
    ms = TF_MS[interval]
    # Binary search on open_time_ms + ms
    lo, hi = 0, len(candles) - 1
    best = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        ct = candles[mid].open_time_ms + ms
        if ct <= t_close_ms:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def slice_closed(
    candles: Sequence[Candle],
    interval: str,
    t_close_ms: int,
) -> list[Candle]:
    j = _end_index_closed(candles, interval, t_close_ms)
    if j < 0:
        return []
    return list(candles[: j + 1])


def fetch_klines_range(
    client: BinanceUsdmClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = 1500,
) -> list[Candle]:
    """分页拉取 [start_ms, end_ms) 内全部 K 线；单次请求的 (end-start) 受交易所上限（约 200 日）约束。"""
    ms_day = 86400000
    max_span = int(199 * ms_day)
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    rows_accum: list[list[Any]] = []
    seen_open: set[int] = set()
    win_start = start_ms
    while win_start < end_ms:
        win_end = min(win_start + max_span, end_ms)
        cur = int(win_start)
        while cur < win_end:
            rows = client.klines(
                symbol,
                interval,
                start_time_ms=cur,
                end_time_ms=win_end,
                limit=limit,
            )
            if not rows:
                break
            for r in rows:
                ot = int(r[0])
                if ot not in seen_open:
                    seen_open.add(ot)
                    rows_accum.append(r)
            last_open = int(rows[-1][0])
            step = TF_MS.get(interval, 60_000)
            nxt = last_open + step
            if nxt <= cur:
                break
            cur = nxt
            if len(rows) < limit:
                break
        win_start = win_end
    rows_accum.sort(key=lambda r: int(r[0]))
    return parse_binance_klines(rows_accum)


@dataclass
class BacktestTrade:
    symbol: str
    side: Side
    entry_ts: float
    exit_ts: float
    entry_px: float
    exit_px: float
    qty: float
    contract_multiplier: float
    pnl_gross: float
    fees: float
    pnl_net: float
    exit_reason: str


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    equity_curve: list[tuple[float, float]]
    """(ts_sec, equity_quote)。"""
    symbols: tuple[str, ...]
    by_symbol: dict[str, dict[str, float]]
    aggregate: dict[str, float]
    kelly_estimate: dict[str, float]
    params_used: dict[str, Any] = field(default_factory=dict)


def _apply_slippage_entry(side: Side, px: float, slip_bps: float) -> float:
    s = slip_bps / 10_000.0
    if side == "long":
        return px * (1.0 + s)
    return px * (1.0 - s)


def _apply_slippage_exit(side: Side, px: float, slip_bps: float) -> float:
    s = slip_bps / 10_000.0
    if side == "long":
        return px * (1.0 - s)
    return px * (1.0 + s)


def _float_step(step_raw: str) -> float:
    if not step_raw or not step_raw.strip():
        return 0.0
    return float(step_raw)


def _pnl_quote(
    side: Side,
    entry: float,
    exit_: float,
    qty: float,
    cm: float,
) -> float:
    """USDT 线性永续 PnL；cm 保留参数以兼容历史字段，USD-M 应为 1.0。"""
    _ = cm
    return linear_pnl_usdt(side, qty, entry, exit_)


def _annualized_return(equity_start: float, equity_end: float, t_start: float, t_end: float) -> float:
    if equity_start <= 0 or t_end <= t_start:
        return float("nan")
    years = (t_end - t_start) / (365.25 * 24 * 3600)
    if years <= 0:
        return float("nan")
    return (equity_end / equity_start) ** (1.0 / years) - 1.0


def _max_drawdown(equity: Sequence[float]) -> float:
    peak = -float("inf")
    max_dd = 0.0
    for x in equity:
        peak = max(peak, x)
        if peak > 0:
            max_dd = max(max_dd, (peak - x) / peak)
    return max_dd


def _sharpe_daily(equity_ts: Sequence[tuple[float, float]], *, rf: float = 0.0) -> float:
    """基于权益曲线重采样到日末的简单 Sharpe（年化 sqrt(365)）。"""
    if len(equity_ts) < 3:
        return float("nan")
    # bucket by UTC day
    from datetime import datetime, timezone

    last_day: dict[str, float] = {}
    for ts, eq in equity_ts:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        last_day[d] = eq
    days_sorted = sorted(last_day.items(), key=lambda x: x[0])
    rets: list[float] = []
    prev = None
    for _, eq in days_sorted:
        if prev is not None and prev > 0:
            rets.append((eq - prev) / prev - rf / 365.25)
        prev = eq
    if len(rets) < 2:
        return float("nan")
    m = sum(rets) / len(rets)
    v = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    sd = math.sqrt(max(v, 0.0))
    if sd < 1e-18:
        return float("nan")
    return (m / sd) * math.sqrt(365.25)


def _estimate_kelly_pb(trades: Sequence[BacktestTrade]) -> dict[str, float]:
    wins = [t.pnl_net for t in trades if t.pnl_net > 0]
    losses = [t.pnl_net for t in trades if t.pnl_net < 0]
    n = len(trades)
    if n == 0:
        return {"p": float("nan"), "b": float("nan"), "n_trades": 0.0}
    p = len(wins) / n
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0  # negative
    b = (aw / abs(al)) if al < 0 else float("inf")
    return {"p": p, "b": b, "n_trades": float(n), "avg_win": aw, "avg_loss": al}


def _trade_stats_core(trades: Sequence[BacktestTrade]) -> dict[str, float]:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0.0,
            "win_rate": float("nan"),
            "profit_factor": float("nan"),
            "avg_win_loss_ratio": float("nan"),
            "total_pnl_net": 0.0,
            "wins": 0.0,
            "losses": 0.0,
        }
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net < 0]
    win_rate = len(wins) / n
    gp = sum(t.pnl_net for t in wins)
    gl = sum(abs(t.pnl_net) for t in losses)
    profit_factor = gp / gl if gl > 1e-18 else float("inf")
    aw = gp / len(wins) if wins else 0.0
    al = gl / len(losses) if losses else 0.0
    avg_wl = aw / al if al > 1e-18 else float("inf")
    return {
        "trades": float(n),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win_loss_ratio": avg_wl,
        "total_pnl_net": sum(t.pnl_net for t in trades),
        "wins": float(len(wins)),
        "losses": float(len(losses)),
    }


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    fee_rate: float = 0.0005
    """每边 taker 费率；开仓+平仓共 2 次。"""
    slippage_bps: float = 2.0
    warmup_extra_days: float = 140.0
    risk_limits: RiskLimits | None = None
    """若 None 则使用默认 RiskLimits()。"""
    cooldown_bars_after_exit: int = 0
    """按 15m bar 计的简单冷却（非账户连续亏损熔断）。"""


def parse_trend_model_params(settings: Mapping[str, Any]) -> TrendModelParams:
    raw = settings.get("trend_model")
    if not isinstance(raw, dict):
        return TrendModelParams()
    base = TrendModelParams()
    patch: dict[str, Any] = {}
    for k in (
        "long_threshold",
        "short_threshold",
        "exit_threshold",
        "min_adx",
        "min_regression_r2",
        "chop_reject_above",
        "weight_15m",
        "weight_1h",
        "weight_4h",
        "w_slope",
        "w_adx_bundle",
        "w_ema",
        "w_breakout",
        "w_volume",
        "w_chop_bundle",
        "volume_confirm_mult",
        "slope_norm_divisor",
    ):
        if k in raw and isinstance(raw[k], (int, float)) and not isinstance(raw[k], bool):
            patch[k] = float(raw[k])
    for k in (
        "regression_bars",
        "chop_period",
        "adx_period",
        "ema_fast",
        "ema_mid",
        "ema_slow",
        "donchian_period",
        "volume_ma_period",
        "max_ema_crosses",
        "min_bars_soft",
    ):
        if k in raw and isinstance(raw[k], int) and not isinstance(raw[k], bool):
            patch[k] = int(raw[k])
    if not patch:
        return base
    return replace(base, **patch)


def run_backtest(
    *,
    settings: Mapping[str, Any],
    client: BinanceUsdmClient,
    matched: Sequence[UsdMFuturesSymbol],
    data: Mapping[str, Mapping[str, list[Candle]]],
    base_timeline: list[Candle],
    strat_params: StrategyLoopParams,
    trend_params: TrendModelParams,
    config: BacktestConfig | None = None,
    intervals: tuple[str, ...] | None = None,
    trail_stop_fraction: float | None = None,
    emit: Callable[[str], None] | None = None,
) -> BacktestResult:
    """在已对齐的 15m 时间轴上推进；`data[symbol][interval]` 为按 open_time 升序。"""
    _ = client
    cfg = config or BacktestConfig()
    lim = cfg.risk_limits or RiskLimits()
    iv = intervals if intervals is not None else intervals_from_settings(dict(settings))
    need = tuple(trend_params.intervals_required)
    for s in need:
        if s not in iv:
            raise ValueError(f"trend requires interval {s!r} present in {iv!r}")

    model = TrendModel(trend_params)
    engine = RiskEngine(limits=lim)
    specs_map = {sp.symbol.upper(): sp for sp in matched}
    symbols = tuple(sorted(specs_map.keys()))

    out_log = emit or (lambda _m: None)

    trail_frac = trail_stop_fraction
    if trail_frac is not None and trail_frac <= 0:
        trail_frac = None

    cash = float(cfg.initial_equity)
    curve: list[tuple[float, float]] = []
    trades: list[BacktestTrade] = []

    # position state
    pos_sym: str | None = None
    pos_side: Side | None = None
    qty: float = 0.0
    cm: float = USDM_LINEAR_CONTRACT_MULTIPLIER
    entry_ref: float = 0.0
    extreme: float = 0.0
    add_count: int = 0
    cooldown_until_i: int = -1

    def total_equity(mid_px: float | None) -> float:
        if pos_sym is None or pos_side is None or mid_px is None or qty <= 0:
            return cash
        u = _pnl_quote(pos_side, entry_ref, mid_px, qty, cm)
        return cash + u

    def close_position(
        sym: str,
        side_p: Side,
        px_exit_raw: float,
        ts_sec: float,
        reason: str,
    ) -> None:
        nonlocal cash, pos_sym, pos_side, qty, cm, entry_ref, extreme, add_count
        ex = _apply_slippage_exit(side_p, px_exit_raw, cfg.slippage_bps)
        g = _pnl_quote(side_p, entry_ref, ex, qty, cm)
        fee_close = abs(qty) * ex * cfg.fee_rate
        fee_open = abs(qty) * entry_ref * cfg.fee_rate
        fe_total = fee_open + fee_close
        pnl_net = g - fe_total
        cash += g - fee_close
        engine.monitor.record_realized_pnl(ts_sec, pnl_net)
        trades.append(
            BacktestTrade(
                symbol=sym,
                side=side_p,
                entry_ts=entry_ts,
                exit_ts=ts_sec,
                entry_px=entry_ref,
                exit_px=ex,
                qty=qty,
                contract_multiplier=cm,
                pnl_gross=g,
                fees=fe_total,
                pnl_net=g - fe_total,
                exit_reason=reason,
            )
        )
        pos_sym = None
        pos_side = None
        qty = 0.0
        cm = USDM_LINEAR_CONTRACT_MULTIPLIER
        entry_ref = 0.0
        extreme = 0.0
        add_count = 0

    entry_ts = 0.0
    min_need = int(trend_params.min_bars_soft) + 50

    for i, bar in enumerate(base_timeline):
        t_close_ms = _close_time_ms(bar, "15m")
        ts_sec = t_close_ms / 1000.0

        if pos_sym and pos_side and qty > 0:
            hold_rows = data[pos_sym]["15m"]
            j_hold = _end_index_closed(hold_rows, "15m", t_close_ms)
            if j_hold < 0:
                continue
            cbar = hold_rows[j_hold]
            px_close = float(cbar.close)
            hi = float(cbar.high)
            lo = float(cbar.low)
            mark_eq = px_close
        else:
            px_close = float(bar.close)
            hi = float(bar.high)
            lo = float(bar.low)
            mark_eq = None

        eq_now = total_equity(mark_eq)
        curve.append((ts_sec, eq_now))

        if pos_sym is not None and pos_side is not None and qty > 0:
            sym = pos_sym
            side_p = pos_side
            if side_p == "long":
                extreme = max(extreme, hi)
            else:
                extreme = min(extreme, lo)

            atr_val: float | None = None
            if strat_params.atr_stop_k is not None and strat_params.atr_stop_k > 0:
                c15_hold = data[sym]["15m"]
                j_atr = _end_index_closed(c15_hold, "15m", t_close_ms)
                if j_atr >= 0:
                    atr_slice = list(c15_hold[: j_atr + 1])
                    a = wilder_atr_last(atr_slice, period=strat_params.atr_period)
                    if math.isfinite(a) and a > 0:
                        atr_val = float(a)
            stop_px = desired_protective_stop_price(
                side_p,
                float(entry_ref),
                float(extreme),
                float(strat_params.stop_adverse_fraction),
                trail_frac,
                atr=atr_val,
                atr_stop_k=strat_params.atr_stop_k,
            )

            stopped = False
            if side_p == "long" and lo <= stop_px:
                close_position(sym, side_p, stop_px, ts_sec, "stop")
                stopped = True
                cooldown_until_i = i + cfg.cooldown_bars_after_exit
            elif side_p == "short" and hi >= stop_px:
                close_position(sym, side_p, stop_px, ts_sec, "stop")
                stopped = True
                cooldown_until_i = i + cfg.cooldown_bars_after_exit

            if not stopped:
                c15 = data[sym]["15m"]
                slice15 = slice_closed(c15, "15m", t_close_ms)
                slice1h = slice_closed(data[sym]["1h"], "1h", t_close_ms)
                slice4h = slice_closed(data[sym]["4h"], "4h", t_close_ms)
                if (
                    len(slice15) >= min_need
                    and len(slice1h) >= min_need
                    and len(slice4h) >= min_need
                ):
                    sig = model.evaluate(
                        {
                            "15m": slice15,
                            "1h": slice1h,
                            "4h": slice4h,
                        }
                    )
                    if should_exit_max_hold(
                        opened_unix_ms=entry_ts,
                        max_hold_hours=strat_params.max_hold_hours,
                        now_unix=ts_sec,
                    ):
                        close_position(sym, side_p, px_close, ts_sec, "max_hold")
                        cooldown_until_i = i + cfg.cooldown_bars_after_exit
                    elif should_exit_from_trend(holding=side_p, sig=sig, tparams=trend_params):
                        close_position(sym, side_p, px_close, ts_sec, "trend_exit")
                        cooldown_until_i = i + cfg.cooldown_bars_after_exit
                    elif (
                        add_count < strat_params.max_add_per_round
                        and has_float_profit(
                            side=side_p,
                            quantity=qty,
                            avg_entry=entry_ref,
                            mark=px_close,
                            min_return_fraction=strat_params.min_unrealized_profit_fraction,
                        )
                        and trend_allows_add(holding=side_p, sig=sig, tparams=trend_params)
                    ):
                        stop_add = fixed_stop_price(
                            side_p, px_close, strat_params.stop_adverse_fraction
                        )
                        sp_add = specs_map.get(sym.upper())
                        if sp_add is not None:
                            step_sz = _float_step(sp_add.market_step_size or sp_add.step_size)
                            try:
                                min_q = (
                                    float(sp_add.market_min_qty or sp_add.min_qty)
                                    if (sp_add.market_min_qty or sp_add.min_qty)
                                    else 0.0
                                )
                            except (TypeError, ValueError):
                                min_q = 0.0
                            cm_u = usdm_linear_contract_multiplier(sp_add)
                            min_ntl = parse_min_notional_usdt(sp_add)
                            equity_add = total_equity(px_close)
                            ae = engine.evaluate_add(
                                ts=ts_sec,
                                equity=equity_add,
                                entry_price=px_close,
                                stop_price=stop_add,
                                side=side_p,
                                p=strat_params.kelly_p,
                                b=strat_params.kelly_b,
                                existing_quantity=qty,
                                existing_avg_entry=entry_ref,
                                quantity_step=step_sz,
                                min_quantity=min_q,
                                contract_multiplier=cm_u,
                                min_notional_usdt=min_ntl,
                                initial_leverage=strat_params.initial_leverage,
                            )
                            if ae.allow and ae.incremental_quantity > 0.0:
                                inc = float(ae.incremental_quantity)
                                add_px = _apply_slippage_entry(side_p, px_close, cfg.slippage_bps)
                                fee_add = abs(inc) * add_px * cfg.fee_rate
                                cash -= fee_add
                                prev_q = qty
                                qty += inc
                                entry_ref = (
                                    (prev_q * entry_ref + inc * add_px) / qty
                                    if prev_q > 0.0
                                    else add_px
                                )
                                add_count += 1
                                extreme = hi if side_p == "long" else lo
                                out_log(
                                    f"[bt add] i={i} sym={sym} side={side_p} add#{add_count} "
                                    f"inc_qty={inc:.8g} avg_entry={entry_ref:.8g} equity≈{equity_add:.4f}"
                                )
            continue

        # flat — optional cooldown
        if i < cooldown_until_i:
            continue

        assessed: list[tuple[str, TrendSignal]] = []
        for sym in symbols:
            c15 = data[sym]["15m"]
            slice15 = slice_closed(c15, "15m", t_close_ms)
            slice1h = slice_closed(data[sym]["1h"], "1h", t_close_ms)
            slice4h = slice_closed(data[sym]["4h"], "4h", t_close_ms)
            if (
                len(slice15) < min_need
                or len(slice1h) < min_need
                or len(slice4h) < min_need
            ):
                continue
            sig = model.evaluate({"15m": slice15, "1h": slice1h, "4h": slice4h})
            assessed.append((sym, sig))

        ranked = rank_directional_signals(assessed)
        ts_eval = ts_sec
        equity_eval = cash
        picked = False
        for rank_idx, (sym_r, sig_r) in enumerate(ranked, start=1):
            sk = signal_side_to_risk_side(sig_r)
            if sk is None:
                continue
            sp = specs_map.get(sym_r.upper())
            if sp is None:
                continue
            j15 = _end_index_closed(data[sym_r]["15m"], "15m", t_close_ms)
            if j15 < 0:
                continue
            obar = data[sym_r]["15m"][j15]
            mark = float(obar.close)
            ohi = float(obar.high)
            olo = float(obar.low)
            stop_v = fixed_stop_price(sk, mark, strat_params.stop_adverse_fraction)
            step_sz = _float_step(sp.market_step_size or sp.step_size)
            try:
                min_q = float(sp.market_min_qty or sp.min_qty) if (sp.market_min_qty or sp.min_qty) else 0.0
            except (TypeError, ValueError):
                min_q = 0.0
            cm_u = usdm_linear_contract_multiplier(sp)
            min_ntl = parse_min_notional_usdt(sp)
            oe = engine.evaluate_open(
                ts=ts_eval,
                equity=equity_eval,
                entry_price=mark,
                stop_price=stop_v,
                side=sk,
                p=strat_params.kelly_p,
                b=strat_params.kelly_b,
                quantity_step=step_sz,
                min_quantity=min_q,
                contract_multiplier=cm_u,
                min_notional_usdt=min_ntl,
                initial_leverage=strat_params.initial_leverage,
            )
            if oe.allow and oe.position is not None:
                q = float(oe.position.quantity)
                entry_eff = _apply_slippage_entry(sk, mark, cfg.slippage_bps)
                fee_open = abs(q) * entry_eff * cfg.fee_rate
                cash -= fee_open
                pos_sym = sym_r.upper()
                pos_side = sk
                qty = q
                cm = cm_u
                entry_ref = entry_eff
                extreme = ohi if sk == "long" else olo
                add_count = 0
                entry_ts = ts_sec
                out_log(
                    f"[bt open] i={i} sym={sym_r} side={sk} qty={q:.8g} entry={entry_eff:.8g} "
                    f"fee_open={fee_open:.4f} score={sig_r.score:+.4f}"
                )
                picked = True
                break
            out_log(
                f"[bt skip] rank={rank_idx} sym={sym_r} side={sk} reasons={'; '.join(oe.reasons)}"
            )
        if not picked and ranked:
            pass

    eq_series = [c[1] for c in curve]
    t0 = curve[0][0] if curve else 0.0
    t1 = curve[-1][0] if curve else 0.0
    init = cfg.initial_equity
    end_eq = eq_series[-1] if eq_series else init

    trade_core = _trade_stats_core(trades)
    aggregate = {
        **trade_core,
        "max_drawdown": _max_drawdown(eq_series) if eq_series else 0.0,
        "sharpe": _sharpe_daily(curve),
        "cagr": _annualized_return(init, end_eq, t0, t1),
        "equity_end": end_eq,
    }

    by_symbol: dict[str, dict[str, float]] = {}
    for sym in symbols:
        sub = [t for t in trades if t.symbol.upper() == sym]
        core = _trade_stats_core(sub)
        by_symbol[sym] = {
            **core,
            "max_drawdown": float("nan"),
            "sharpe": float("nan"),
            "cagr": float("nan"),
        }

    kelly_est = _estimate_kelly_pb(trades)

    return BacktestResult(
        trades=trades,
        equity_curve=curve,
        symbols=symbols,
        by_symbol=by_symbol,
        aggregate=aggregate,
        kelly_estimate=kelly_est,
        params_used={
            "stop_adverse_fraction": strat_params.stop_adverse_fraction,
            "kelly_p": strat_params.kelly_p,
            "kelly_b": strat_params.kelly_b,
            "trail_stop_fraction": trail_frac,
            "long_threshold": trend_params.long_threshold,
            "short_threshold": trend_params.short_threshold,
            "min_adx": trend_params.min_adx,
            "max_position_fraction": lim.max_position_fraction,
            "kelly_extra_multiplier": lim.kelly_extra_multiplier,
            "max_add_per_round": strat_params.max_add_per_round,
            "min_unrealized_profit_fraction": strat_params.min_unrealized_profit_fraction,
            "fee_rate": cfg.fee_rate,
            "slippage_bps": cfg.slippage_bps,
        },
    )


def build_aligned_timeline(
    data: Mapping[str, Mapping[str, list[Candle]]],
    symbols: Sequence[str],
) -> tuple[list[Candle], int]:
    """各标的 15m 的 open_time 交集排序；时间轴仅用于推进 t_close。"""
    syms = [s.upper() for s in symbols]
    if not syms:
        return [], 0
    tsets: list[set[int]] = []
    for s in syms:
        tsets.append({c.open_time_ms for c in data[s]["15m"]})
    common = set.intersection(*tsets) if tsets else set()
    master = min(syms, key=lambda x: len(data[x]["15m"]))
    axis = [c for c in data[master]["15m"] if c.open_time_ms in common]
    axis.sort(key=lambda c: c.open_time_ms)
    return axis, 0


def load_backtest_data(
    client: BinanceUsdmClient,
    matched: Sequence[UsdMFuturesSymbol],
    *,
    start_ms: int,
    end_ms: int,
    warmup_extra_days: float = 140.0,
) -> tuple[dict[str, dict[str, list[Candle]]], list[Candle]]:
    ms_day = 86400000
    warm_ms = int(float(warmup_extra_days) * ms_day)
    fetch_start = max(0, int(start_ms) - warm_ms)
    end_ms = int(end_ms)
    data: dict[str, dict[str, list[Candle]]] = {}
    for sp in matched:
        sym = sp.symbol.upper()
        data[sym] = {
            "15m": fetch_klines_range(client, sym, "15m", fetch_start, end_ms),
            "1h": fetch_klines_range(client, sym, "1h", fetch_start, end_ms),
            "4h": fetch_klines_range(client, sym, "4h", fetch_start, end_ms),
        }
    axis_full, _ = build_aligned_timeline(data, [sp.symbol for sp in matched])
    axis_live = [c for c in axis_full if int(start_ms) <= _close_time_ms(c, "15m") <= end_ms]
    return data, axis_live


DEFAULT_SENSITIVITY_GRID: dict[str, tuple[float, ...]] = {
    "trend_threshold": (0.55, 0.65, 0.70, 0.75, 0.85),
    "min_adx": (18.0, 22.0, 25.0, 28.0, 32.0),
    "stop_adverse_fraction": (0.03, 0.04, 0.05, 0.06, 0.08),
    "kelly_extra_multiplier": (0.25, 0.5, 0.75, 1.0, 1.25),
    "max_position_fraction": (0.08, 0.10, 0.12, 0.15, 0.20),
}


def run_parameter_sensitivity(
    *,
    settings: Mapping[str, Any],
    client: BinanceUsdmClient,
    matched: Sequence[UsdMFuturesSymbol],
    data: Mapping[str, Mapping[str, list[Candle]]],
    base_timeline: list[Candle],
    strat_params: StrategyLoopParams,
    trend_params: TrendModelParams,
    config: BacktestConfig,
    trail_stop_fraction: float | None,
    grid: Mapping[str, Sequence[float]] | None = None,
) -> list[dict[str, Any]]:
    """单参数扫描：每次只变一个维度，其它固定为传入的基准。"""
    g = dict(grid or DEFAULT_SENSITIVITY_GRID)
    rows: list[dict[str, Any]] = []
    baseline = run_backtest(
        settings=settings,
        client=client,
        matched=matched,
        data=data,
        base_timeline=base_timeline,
        strat_params=strat_params,
        trend_params=trend_params,
        config=config,
        trail_stop_fraction=trail_stop_fraction,
    )
    b_cagr = float(baseline.aggregate.get("cagr", float("nan")))
    b_dd = float(baseline.aggregate.get("max_drawdown", float("nan")))
    b_sh = float(baseline.aggregate.get("sharpe", float("nan")))

    def _row(param: str, value: float, res: BacktestResult) -> dict[str, Any]:
        return {
            "param": param,
            "value": value,
            "cagr": res.aggregate.get("cagr", float("nan")),
            "max_drawdown": res.aggregate.get("max_drawdown", float("nan")),
            "sharpe": res.aggregate.get("sharpe", float("nan")),
            "win_rate": res.aggregate.get("win_rate", float("nan")),
            "profit_factor": res.aggregate.get("profit_factor", float("nan")),
            "avg_win_loss_ratio": res.aggregate.get("avg_win_loss_ratio", float("nan")),
            "trades": res.aggregate.get("trades", 0.0),
            "total_pnl_net": res.aggregate.get("total_pnl_net", 0.0),
            "delta_cagr_vs_baseline": float(res.aggregate.get("cagr", float("nan"))) - b_cagr,
            "delta_max_dd_vs_baseline": float(res.aggregate.get("max_drawdown", float("nan"))) - b_dd,
        }

    for thr in g.get("trend_threshold", ()):
        th = float(thr)
        tp = replace(trend_params, long_threshold=th, short_threshold=th)
        r = run_backtest(
            settings=settings,
            client=client,
            matched=matched,
            data=data,
            base_timeline=base_timeline,
            strat_params=strat_params,
            trend_params=tp,
            config=config,
            trail_stop_fraction=trail_stop_fraction,
        )
        rows.append(_row("trend_threshold", th, r))

    for adx in g.get("min_adx", ()):
        ad = float(adx)
        tp = replace(trend_params, min_adx=ad)
        r = run_backtest(
            settings=settings,
            client=client,
            matched=matched,
            data=data,
            base_timeline=base_timeline,
            strat_params=strat_params,
            trend_params=tp,
            config=config,
            trail_stop_fraction=trail_stop_fraction,
        )
        rows.append(_row("min_adx", ad, r))

    for sf in g.get("stop_adverse_fraction", ()):
        sff = float(sf)
        sp2 = replace(strat_params, stop_adverse_fraction=sff)
        r = run_backtest(
            settings=settings,
            client=client,
            matched=matched,
            data=data,
            base_timeline=base_timeline,
            strat_params=sp2,
            trend_params=trend_params,
            config=config,
            trail_stop_fraction=trail_stop_fraction,
        )
        rows.append(_row("stop_adverse_fraction", sff, r))

    lim0 = config.risk_limits or RiskLimits()
    for km in g.get("kelly_extra_multiplier", ()):
        lim_n = replace(lim0, kelly_extra_multiplier=float(km))
        cfg_n = replace(config, risk_limits=lim_n)
        r = run_backtest(
            settings=settings,
            client=client,
            matched=matched,
            data=data,
            base_timeline=base_timeline,
            strat_params=strat_params,
            trend_params=trend_params,
            config=cfg_n,
            trail_stop_fraction=trail_stop_fraction,
        )
        rows.append(_row("kelly_extra_multiplier", float(km), r))

    for mp in g.get("max_position_fraction", ()):
        lim_n = replace(lim0, max_position_fraction=float(mp))
        cfg_n = replace(config, risk_limits=lim_n)
        r = run_backtest(
            settings=settings,
            client=client,
            matched=matched,
            data=data,
            base_timeline=base_timeline,
            strat_params=strat_params,
            trend_params=trend_params,
            config=cfg_n,
            trail_stop_fraction=trail_stop_fraction,
        )
        rows.append(_row("max_position_fraction", float(mp), r))

    base_summary = {
        "param": "BASELINE",
        "value": float("nan"),
        "cagr": b_cagr,
        "max_drawdown": b_dd,
        "sharpe": b_sh,
        "win_rate": baseline.aggregate.get("win_rate", float("nan")),
        "profit_factor": baseline.aggregate.get("profit_factor", float("nan")),
        "avg_win_loss_ratio": baseline.aggregate.get("avg_win_loss_ratio", float("nan")),
        "trades": baseline.aggregate.get("trades", 0.0),
        "total_pnl_net": baseline.aggregate.get("total_pnl_net", 0.0),
        "delta_cagr_vs_baseline": 0.0,
        "delta_max_dd_vs_baseline": 0.0,
    }
    return [base_summary, *rows]


def summarize_sensitivity(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """粗略度量各参数对 CAGR 的敏感度：range(max-min) of CAGR per param name。"""
    from collections import defaultdict

    by_p: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        p = str(r.get("param", ""))
        if p == "BASELINE":
            continue
        c = r.get("cagr")
        if isinstance(c, (int, float)) and math.isfinite(float(c)):
            by_p[p].append(float(c))
    out: dict[str, dict[str, float]] = {}
    for p, vals in by_p.items():
        if len(vals) < 2:
            out[p] = {"cagr_range": 0.0, "cagr_std": 0.0}
            continue
        rng = max(vals) - min(vals)
        m = sum(vals) / len(vals)
        var = sum((x - m) ** 2 for x in vals) / max(len(vals) - 1, 1)
        out[p] = {"cagr_range": rng, "cagr_std": math.sqrt(max(var, 0.0))}
    ranked = sorted(out.items(), key=lambda kv: kv[1].get("cagr_range", 0.0), reverse=True)
    return {k: v for k, v in ranked}


def print_backtest_report(res: BacktestResult, *, emit: Callable[[str], None] = print) -> None:
    emit("=== 回测总体 ===")
    ag = res.aggregate
    emit(
        f"symbols={list(res.symbols)} trades={int(ag.get('trades', 0))} "
        f"win_rate={ag.get('win_rate', float('nan')):.4f} "
        f"profit_factor={ag.get('profit_factor', float('nan')):.4f} "
        f"avg_WL={ag.get('avg_win_loss_ratio', float('nan')):.4f}"
    )
    emit(
        f"max_drawdown={ag.get('max_drawdown', float('nan')):.4f} "
        f"Sharpe(日收益)≈{ag.get('sharpe', float('nan')):.4f} "
        f"CAGR≈{ag.get('cagr', float('nan')):.4%} "
        f"equity_end={ag.get('equity_end', float('nan')):.4f}"
    )
    ke = res.kelly_estimate
    emit(
        f"Kelly 估计: p≈{ke.get('p', float('nan')):.4f} b≈{ke.get('b', float('nan')):.4f} "
        f"n={int(ke.get('n_trades', 0))}"
    )
    emit("=== 分标的 ===")
    for sym, st in sorted(res.by_symbol.items()):
        if st.get("trades", 0) <= 0:
            emit(f"  {sym}: (no trades)")
            continue
        emit(
            f"  {sym}: trades={int(st.get('trades', 0))} win_rate={st.get('win_rate', float('nan')):.4f} "
            f"PF={st.get('profit_factor', float('nan')):.4f} pnl_net={st.get('total_pnl_net', 0):.4f}"
        )


def print_sensitivity_report(
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Mapping[str, float]],
    *,
    emit: Callable[[str], None] = print,
) -> None:
    emit("=== 敏感性扫描（相对 BASELINE 单列增量见 delta_*） ===")
    emit("param\tvalue\tcagr\tmax_dd\tsharpe\ttrades\tdelta_cagr")
    for r in rows:
        emit(
            f"{r.get('param')}\t{r.get('value')}\t{r.get('cagr', float('nan')):.6g}\t"
            f"{r.get('max_drawdown', float('nan')):.6g}\t{r.get('sharpe', float('nan')):.6g}\t"
            f"{int(r.get('trades', 0))}\t{r.get('delta_cagr_vs_baseline', float('nan')):.6g}"
        )
    emit("=== 参数敏感度粗排（CAGR 极差） ===")
    for p, m in summary.items():
        emit(f"  {p}: cagr_range={m.get('cagr_range', 0):.6g} std={m.get('cagr_std', 0):.6g}")
