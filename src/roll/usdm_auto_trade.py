"""Binance USD-M Futures signed 自动交易闭环（Testnet / live）：对账→信号→杠杆→开仓→STOP→持仓管理→平仓。

仅通过 `/fapi` REST；Testnet 须 `strategy.testnet_signed_orders_enabled=true` 且官方 Testnet host。
异常时 `pause_opening_entries` 暂停新开仓；已有持仓仍维护保护单并按信号平仓。"""
from __future__ import annotations

import math
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from roll.binance_client import (
    BinanceCoinMSignedClient,
    BinanceHTTPError,
    UsdMFuturesSymbol,
    format_price_to_tick_decimal_str,
    select_monitorable_usdm_symbols,
)
from roll.logger import get_logger
from roll.market_data import parse_candidate_assets
from roll.offline_trend import evaluate_symbol_offline_public
from roll.position_manager import PositionManager, TradeLockState, reconcile_usdm_account
from roll.position_roll import (
    ensure_profit_tier_leverage,
    has_float_profit,
    merge_roll_live_state,
    parse_add_count,
    trend_allows_add,
)
from roll.risk import (
    AccountRiskMonitor,
    RiskEngine,
    RiskLimits,
    Side,
    account_risk_state_from_mapping,
    account_risk_state_to_mapping,
    atr_stop_price,
    fixed_stop_price as _fixed_stop_price,
    linear_pnl_usdt,
    notional_usdt,
    parse_risk_limits_settings,
    trailing_stop_price,
)
from roll.state_store import RuntimeState, StateStore
from roll.strategy_loop import (
    InsufficientMonitorableSymbolsError,
    StrategyLoopParams,
    _float_step,
    evaluate_candidates_with_public_rest,
    intervals_from_settings,
    parse_strategy_loop_params,
    rank_directional_signals,
    signal_side_to_risk_side,
)
from roll.trend_model import (
    Candle,
    SignalSide,
    TrendModel,
    TrendModelParams,
    TrendSignal,
    parse_binance_klines,
    wilder_atr_last,
)
from roll.usdm_account import (
    format_market_quantity_str,
    parse_min_notional_usdt,
    parse_usdt_account_snapshot,
    precheck_usdm_market_open,
    usdm_linear_contract_multiplier,
)

LoggerFn = Callable[[str], None]


def reconcile_and_restore_pm(pm: PositionManager, client: BinanceCoinMSignedClient) -> None:
    client.sync_server_time()
    outcome = reconcile_usdm_account(client.position_risk(), client.open_orders())
    pm.restore_from_exchange(outcome)


def estimate_usdt_equity_for_risk(*, account: Mapping[str, Any]) -> float:
    """USD-M 风控权益：USDT wallet balance + unrealized profit（不乘标记价）。"""
    snap = parse_usdt_account_snapshot(account)
    return snap.equity_usdt if snap.equity_usdt > 0.0 else 0.0


def estimate_available_margin_usdt(*, account: Mapping[str, Any]) -> float:
    snap = parse_usdt_account_snapshot(account)
    return max(snap.available_margin_usdt, 0.0)


def estimate_quote_equity_for_risk(
    *,
    account: Mapping[str, Any],
    spec: UsdMFuturesSymbol | None = None,
    mark_price_quote: float | None = None,
) -> float:
    _ = spec, mark_price_quote
    return estimate_usdt_equity_for_risk(account=account)


def _lots(spec: UsdMFuturesSymbol, *, market: bool) -> tuple[str, str, str]:
    return spec.tick_size, spec.market_min_qty if market else spec.min_qty, spec.market_step_size if market else spec.step_size


def _min_market_quantity(spec: UsdMFuturesSymbol) -> float:
    raw = spec.market_min_qty or spec.min_qty
    if not raw:
        return 0.0
    try:
        return max(float(raw), 0.0)
    except (TypeError, ValueError):
        return 0.0


def resolve_open_quantity_raw(*, spec: UsdMFuturesSymbol, risk_quantity: float, mode: str) -> float:
    """`min_market`：验收/极小资金时使用交易所 MARKET 最小可交易量。"""
    m = (mode or "risk").strip().lower()
    if m == "min_market":
        mq = _min_market_quantity(spec)
        return mq if mq > 0.0 else risk_quantity
    return risk_quantity


def load_monitorable_specs(
    signed_client: BinanceCoinMSignedClient,
    settings: Mapping[str, Any],
    *,
    min_count: int,
) -> tuple[list[UsdMFuturesSymbol], Any]:
    specs_full = signed_client.list_usdm_specs()
    candidates = parse_candidate_assets(dict(settings))
    return select_monitorable_usdm_symbols(specs_full, candidates, min_count=min_count)


def log_peer_symbol_signals_while_in_position(
    *,
    signed_client: BinanceCoinMSignedClient,
    active_symbol: str,
    matched: Sequence[UsdMFuturesSymbol],
    params: StrategyLoopParams,
    intervals: tuple[str, ...],
    trend_params: TrendModelParams | None,
    emit: LoggerFn,
) -> None:
    """持仓期间：其它 symbol 仅记录趋势信号，绝不下单。"""
    active = active_symbol.strip().upper()
    tpl = TrendModel(trend_params) if trend_params is not None else TrendModel()
    rb = signed_client.config.rest_base.rstrip("/")
    pref = signed_client.config.api_prefix
    emit(f"[live][signal_only] active={active} — scanning peer symbols (no orders)")
    for spec in matched:
        sym = spec.symbol.upper()
        if sym == active:
            continue
        sig = evaluate_symbol_offline_public(
            sym,
            rest_base=rb,
            api_prefix=pref,
            model=tpl,
            klines_limit=params.klines_limit,
            intervals=intervals,
        )
        rr = "; ".join(sig.rejection_reasons) if sig.rejection_reasons else ""
        emit(
            f"[live][signal_only] symbol={sym} signal={sig.side.value} mixed_score={sig.score:+.4f}"
            + (f" trend_reject={rr}" if rr else "")
            + f" — no_order (trade_lock IN_POSITION on {active})"
        )


def _parse_close_position_flag(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        x = raw.strip().lower()
        if x == "true":
            return True
        if x == "false":
            return False
    return None


def _is_protective_close_order(row: Mapping[str, Any]) -> bool:
    ot = str(row.get("type", "")).upper()
    cp = _parse_close_position_flag(row.get("closePosition"))
    if cp is not True:
        return False
    return ot in {"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}


def cancel_protective_close_orders(client: BinanceCoinMSignedClient, symbol: str, *, emit: LoggerFn) -> None:
    sym = symbol.upper()
    for row in client.open_orders(symbol=sym):
        if str(row.get("symbol", "")).upper() != sym or not _is_protective_close_order(dict(row)):
            continue
        oid_raw = row.get("orderId")
        try:
            oid = int(oid_raw) if oid_raw is not None else None
        except (TypeError, ValueError):
            oid = None
        if oid is None:
            continue
        try:
            client.cancel_order(symbol=sym, order_id=oid)
            emit(f"[live] canceled_protective orderId={oid} type={row.get('type')}")
        except BinanceHTTPError as e:
            emit(f"[live.warn] cancel_protective_failed code={e.code} msg={e.msg}")


def desired_protective_stop_price(
    side: Side,
    entry_px: float,
    extreme_px: float,
    adverse_frac: float,
    trail_frac: float | None,
    *,
    atr: float | None = None,
    atr_stop_k: float | None = None,
) -> float:
    """固定 / 追踪 / ATR 止损取最保守价（多头取最高止损，空头取最低）。"""
    candidates = [_fixed_stop_price(side, entry_px, adverse_frac)]
    if trail_frac is not None and trail_frac > 0:
        candidates.append(trailing_stop_price(side, extreme_px, trail_fraction=float(trail_frac)))
    if (
        atr is not None
        and atr_stop_k is not None
        and atr > 0.0
        and atr_stop_k > 0.0
        and math.isfinite(atr)
    ):
        candidates.append(atr_stop_price(side, entry_px, float(atr), float(atr_stop_k)))
    if side == "long":
        return max(candidates)
    return min(candidates)


def resolve_atr_for_symbol(
    client: BinanceCoinMSignedClient,
    symbol: str,
    *,
    interval: str,
    period: int,
    limit: int,
) -> float | None:
    """拉 K 线计算 Wilder ATR；失败或数据不足返回 None。"""
    try:
        rows = client.klines(symbol.upper(), interval, limit=max(limit, period + 2))
        candles: list[Candle] = parse_binance_klines(rows)
        atr = wilder_atr_last(candles, period=period)
        if math.isfinite(atr) and atr > 0.0:
            return float(atr)
    except (ValueError, OSError, BinanceHTTPError):
        pass
    return None


def should_exit_max_hold(
    *,
    opened_unix_ms: int | float | None,
    max_hold_hours: float | None,
    now_unix: float | None = None,
) -> bool:
    """``opened_unix_ms`` 可为毫秒时间戳或秒级 Unix 时间。"""
    if max_hold_hours is None or max_hold_hours <= 0:
        return False
    if opened_unix_ms is None:
        return False
    now = time.time() if now_unix is None else float(now_unix)
    opened = float(opened_unix_ms)
    if opened >= 1e12:
        opened /= 1000.0
    return (now - opened) >= float(max_hold_hours) * 3600.0


def _position_opened_unix_ms(live_leaf: Mapping[str, Any]) -> int | None:
    raw = live_leaf.get("position_opened_unix_ms")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    return None


def _fmt_stop_px(side: Side, raw_px: float, tick: str) -> str:
    rnd = "down" if side == "long" else "up"
    return format_price_to_tick_decimal_str(raw_px, tick, rounding=rnd)


def _net_position_any(rows: list[dict[str, Any]], symbol: str) -> tuple[float | None, str | None]:
    want = symbol.upper()
    lh: list[tuple[str, Decimal]] = []
    for row in rows:
        if str(row.get("symbol", "")).upper() != want:
            continue
        ps_u = row.get("positionSide")
        ps = ps_u.upper() if isinstance(ps_u, str) else "BOTH"
        amt_raw = row.get("positionAmt")
        if not isinstance(amt_raw, str):
            continue
        try:
            d = Decimal(amt_raw)
        except InvalidOperation:
            continue
        if ps in {"LONG", "SHORT"}:
            lh.append((ps, d))
        else:
            return float(d), "BOTH"
    lg = Decimal(0)
    sh = Decimal(0)
    for nm, dv in lh:
        if nm == "LONG":
            lg += dv
        else:
            sh += dv
    if lg != 0 and sh != 0:
        return float(lg + sh), "HEDGE"
    if lg != 0:
        return float(lg), "LONG"
    if sh != 0:
        return float(sh), "SHORT"
    return None, None


def _position_entry_avg(rows: list[dict[str, Any]], symbol: str) -> float | None:
    """从 positionRisk 读取交易所持仓均价（entryPrice）。"""
    want = symbol.upper()
    for row in rows:
        if str(row.get("symbol", "")).upper() != want:
            continue
        ep = row.get("entryPrice")
        if ep is None:
            continue
        try:
            px = float(ep)
        except (TypeError, ValueError):
            continue
        if px > 0.0:
            return px
    return None


def infer_hold_side(am: float | None, hedge_h: str | None) -> Side | None:
    if am is None or hedge_h == "HEDGE":
        return None
    if am > 0:
        return "long"
    if am < 0:
        return "short"
    return None


def stop_order_side_for_protect(*, hold_side: Side) -> str:
    return "SELL" if hold_side == "long" else "BUY"


def should_exit_from_trend(*, holding: Side, sig: TrendSignal, tparams: TrendModelParams) -> bool:
    if holding == "long":
        if sig.side is SignalSide.SHORT and (-sig.score) >= tparams.short_threshold:
            return True
        if sig.side is SignalSide.LONG and sig.score < tparams.exit_threshold:
            return True
        return False
    if holding == "short":
        if sig.side is SignalSide.LONG and sig.score >= tparams.long_threshold:
            return True
        if sig.side is SignalSide.SHORT and (-sig.score) < tparams.exit_threshold:
            return True
        return False
    return False


def _cooldown_active(cooldown_until_unix_ms: int | None) -> bool:
    if cooldown_until_unix_ms is None:
        return False
    return int(time.time() * 1000) < int(cooldown_until_unix_ms)


def _load_live_risk_engine(*, settings: Mapping[str, Any], persisted: RuntimeState) -> tuple[RiskLimits, RiskEngine]:
    limits = parse_risk_limits_settings(settings) or RiskLimits()
    monitor_state = account_risk_state_from_mapping(persisted.account_risk)
    return limits, RiskEngine(limits, monitor=AccountRiskMonitor(limits, monitor_state))


def _apply_account_circuit_pause(
    pm: PositionManager,
    rk: RiskEngine,
    *,
    circ_reasons: Sequence[str],
    emit: LoggerFn,
) -> None:
    st = rk.monitor.state
    if st.halted_max_drawdown:
        pm.pause_opening_entries("circuit:max_drawdown")
        emit("[live][circuit] max_drawdown latched — pause new opens; existing position still managed")
    elif st.halted_daily_loss:
        pm.pause_opening_entries("circuit:daily_loss")
        emit("[live][circuit] daily_loss latched — pause new opens; existing position still managed")
    for r in circ_reasons:
        if r.startswith("max_drawdown:") or r.startswith("daily_loss:"):
            emit(f"[live][circuit] {r}")


def _merge_cooldown_until_ms(
    *,
    persisted_ms: int | None,
    monitor_cooldown_ts: float | None,
    post_exit_ts: float | None,
    cooldown_seconds: float,
) -> int | None:
    candidates: list[int] = []
    if persisted_ms is not None and _cooldown_active(persisted_ms):
        candidates.append(int(persisted_ms))
    if monitor_cooldown_ts is not None and time.time() < monitor_cooldown_ts:
        candidates.append(int(monitor_cooldown_ts * 1000))
    if post_exit_ts is not None:
        candidates.append(int((post_exit_ts + cooldown_seconds) * 1000))
    return max(candidates) if candidates else None


def merge_live_leaf(
    prev_signal: Mapping[str, Any],
    live: Mapping[str, Any],
    *,
    clear_live: bool = False,
) -> dict[str, Any]:
    base = dict(prev_signal) if isinstance(prev_signal, dict) else {}
    if clear_live:
        base.pop("live", None)
        return base
    old_live = base.get("live") if isinstance(base.get("live"), dict) else {}
    merged_live = dict(old_live)
    merged_live.update(dict(live))
    base["live"] = merged_live
    return base


def persist_autotrade_runtime(
    store: StateStore,
    *,
    pm: PositionManager,
    live_leaf: Mapping[str, Any],
    clear_live: bool = False,
    rk: RiskEngine | None = None,
    cooldown_until_unix_ms: int | None = None,
) -> None:
    snap = pm.snapshot_dict()
    prev = store.load()
    merged = merge_live_leaf(prev.last_signal if isinstance(prev.last_signal, dict) else {}, live_leaf, clear_live=clear_live)
    account_risk = account_risk_state_to_mapping(rk.monitor.state) if rk is not None else prev.account_risk
    cd_out = cooldown_until_unix_ms if cooldown_until_unix_ms is not None else prev.cooldown_until_unix_ms
    store.save(
        RuntimeState(
            trade_lock_state=str(snap["trade_lock_state"]),
            active_symbol=(
                str(snap["active_symbol"]).strip().upper()
                if isinstance(snap.get("active_symbol"), str) and snap.get("active_symbol")
                else None
            ),
            halt_automatic_trading=bool(snap.get("halt_automatic_trading")),
            halt_reason=str(snap["halt_reason"]) if isinstance(snap.get("halt_reason"), str) else None,
            pause_new_positions=bool(snap.get("pause_new_positions")),
            pause_new_positions_reason=(
                str(snap["pause_new_reason"]) if isinstance(snap.get("pause_new_reason"), str) else None
            ),
            cooldown_until_unix_ms=cd_out,
            account_risk=account_risk,
            last_signal=merged,
        ),
    )


def fetch_ticker_px(client: BinanceCoinMSignedClient, symbol: str) -> float:
    rows = client.ticker_price(symbol)
    if not rows:
        raise ValueError(f"ticker empty {symbol}")
    px = rows[0].get("price") or rows[0].get("ps")
    if px is None:
        raise ValueError(f"ticker bad row {rows[0]!r}")
    return float(px)


def place_protective_stop_market_close(
    client: BinanceCoinMSignedClient,
    *,
    symbol: str,
    hold_side: Side,
    stop_px_s: str,
    emit: LoggerFn,
) -> None:
    s = stop_order_side_for_protect(hold_side=hold_side)
    cid = f"stp_{uuid.uuid4().hex[:28]}"
    r = client.new_stop_market_close_position(symbol=symbol, side=s, stop_price=stop_px_s, client_order_id=cid[:36])
    emit(f"[live] STOP_MARKET closePosition placed side={s} stop={stop_px_s} oid={r.get('orderId')}")


def ensure_or_roll_protective_stop(
    *,
    client: BinanceCoinMSignedClient,
    symbol: str,
    spec: UsdMFuturesSymbol,
    hold_side: Side,
    entry_ref: float,
    extreme_px: float,
    adverse_frac: float,
    trail_frac: float | None,
    emit: LoggerFn,
    atr: float | None = None,
    atr_stop_k: float | None = None,
) -> tuple[float, str]:
    tick, _mn, _st = _lots(spec, market=True)
    want_raw = desired_protective_stop_price(
        hold_side,
        entry_ref,
        extreme_px,
        adverse_frac,
        trail_frac,
        atr=atr,
        atr_stop_k=atr_stop_k,
    )
    want_s = _fmt_stop_px(hold_side, want_raw, tick)
    protectors = [
        dict(r) for r in client.open_orders(symbol=symbol.upper()) if _is_protective_close_order(dict(r))
    ]
    need_new = True
    if protectors:
        sp0 = protectors[0].get("stopPrice")
        need_new = True
        if isinstance(sp0, str) and sp0:
            d0 = Decimal(sp0)
            d1 = Decimal(want_s)
            tk = Decimal(tick)
            if tk > 0:
                delta_ticks = abs(d0 - d1) / tk
                if delta_ticks < 1:
                    need_new = False
    if need_new and protectors:
        cancel_protective_close_orders(client, symbol, emit=emit)
    if need_new:
        place_protective_stop_market_close(client, symbol=symbol, hold_side=hold_side, stop_px_s=want_s, emit=emit)
    return extreme_px, want_s


def _protective_atr_kwargs(
    client: BinanceCoinMSignedClient,
    symbol: str,
    pcfg: StrategyLoopParams,
) -> dict[str, float | None]:
    atr: float | None = None
    if pcfg.atr_stop_k is not None and pcfg.atr_stop_k > 0:
        atr = resolve_atr_for_symbol(
            client,
            symbol,
            interval=pcfg.atr_interval,
            period=pcfg.atr_period,
            limit=pcfg.klines_limit,
        )
    return {"atr": atr, "atr_stop_k": pcfg.atr_stop_k}


def _execute_open_flow(
    *,
    signed_client: BinanceCoinMSignedClient,
    pm: PositionManager,
    symbol: str,
    spec: UsdMFuturesSymbol,
    side_pick: Side,
    qty_pick: float,
    params: StrategyLoopParams,
    mark: float,
    emit: LoggerFn,
    live_leaf: dict[str, Any],
    trail_frac: float | None,
    atr: float | None = None,
    atr_stop_k: float | None = None,
) -> None:
    pm.begin_enter(symbol)
    tick, _mn, step = _lots(spec, market=True)
    if not tick or not step:
        pm.rollback_enter_to_idle()
        raise ValueError("合约缺少 tick_size / MARKET_LOT_SIZE step")
    try:
        signed_client.set_leverage(symbol=symbol, leverage=params.initial_leverage)
        qty_s = format_market_quantity_str(spec, qty_pick)
        emit(
            f"[live] MARKET_OPEN symbol={symbol} side={side_pick} qty={qty_s} "
            f"leverage={params.initial_leverage}x mode={params.open_quantity_mode}"
        )
        ot = signed_client.new_market_order(
            symbol=symbol,
            side=("BUY" if side_pick == "long" else "SELL"),
            quantity=qty_s,
            reduce_only=False,
        )
        emit(f"[live] open_order orderId={ot.get('orderId')} status={ot.get('status')}")
        pr = signed_client.position_risk(symbol=symbol)
        am, hh = _net_position_any(pr, symbol)
        hs = infer_hold_side(am, hh)
        if hs != side_pick:
            pm.rollback_enter_to_idle()
            raise RuntimeError(f"开仓后未见预期持仓 inferred={hs!r} amt={am!r}")
        pm.confirm_in_position(symbol)
        entry_ref = float(mark)
        ext = entry_ref if side_pick == "long" else entry_ref
        ext = max(ext, mark) if side_pick == "long" else min(ext, mark)
        live_leaf.update(
            {
                "entry_reference": entry_ref,
                "extreme": ext,
                "side": side_pick,
                "qty": qty_s,
                "add_count": 0,
                "position_opened_unix_ms": int(time.time() * 1000),
            }
        )
        _ext2, stops = ensure_or_roll_protective_stop(
            client=signed_client,
            symbol=symbol,
            spec=spec,
            hold_side=side_pick,
            entry_ref=entry_ref,
            extreme_px=float(ext),
            adverse_frac=params.stop_adverse_fraction,
            trail_frac=trail_frac,
            emit=emit,
            atr=atr,
            atr_stop_k=atr_stop_k,
        )
        live_leaf.update({"stop_live": stops, "symbol": symbol.upper(), "extreme": float(_ext2), "mark_px": mark})
    except Exception:
        pm.rollback_enter_to_idle()
        raise


def _execute_add_flow(
    *,
    signed_client: BinanceCoinMSignedClient,
    symbol: str,
    spec: UsdMFuturesSymbol,
    hold_side: Side,
    qty_add: float,
    mark: float,
    emit: LoggerFn,
    live_leaf: dict[str, Any],
    trail_frac: float | None,
    adverse_frac: float,
    atr: float | None = None,
    atr_stop_k: float | None = None,
) -> None:
    """持仓中 MARKET 加仓（不经过 ENTERING 状态机）。"""
    qty_s = format_market_quantity_str(spec, qty_add)
    emit(f"[live] MARKET_ADD symbol={symbol} side={hold_side} qty={qty_s} mark≈{mark:.8g}")
    ot = signed_client.new_market_order(
        symbol=symbol,
        side=("BUY" if hold_side == "long" else "SELL"),
        quantity=qty_s,
        reduce_only=False,
    )
    emit(f"[live] add_order orderId={ot.get('orderId')} status={ot.get('status')}")
    pr = signed_client.position_risk(symbol=symbol)
    am, hh = _net_position_any(pr, symbol)
    hs = infer_hold_side(am, hh)
    if hs != hold_side:
        raise RuntimeError(f"加仓后持仓方向异常 inferred={hs!r} amt={am!r}")
    entry_ex = _position_entry_avg(pr, symbol)
    entry_ref = float(entry_ex) if entry_ex is not None else float(mark)
    qty_f = abs(float(am)) if am is not None else float(qty_s)
    ex0 = live_leaf.get("extreme") if isinstance(live_leaf.get("extreme"), (int, float)) else entry_ref
    ex = float(ex0)
    ex = max(ex, mark) if hold_side == "long" else min(ex, mark)
    add_n = parse_add_count(live_leaf) + 1
    live_leaf.update(
        merge_roll_live_state(
            live_leaf,
            add_count=add_n,
            entry_reference=entry_ref,
            extreme=ex,
            total_qty=qty_f,
        )
    )
    live_leaf["side"] = hold_side
    _ext2, stops = ensure_or_roll_protective_stop(
        client=signed_client,
        symbol=symbol,
        spec=spec,
        hold_side=hold_side,
        entry_ref=entry_ref,
        extreme_px=ex,
        adverse_frac=adverse_frac,
        trail_frac=trail_frac,
        emit=emit,
        atr=atr,
        atr_stop_k=atr_stop_k,
    )
    live_leaf.update({"stop_live": stops, "symbol": symbol.upper(), "mark_px": mark})


def _try_position_roll_add(
    *,
    signed_client: BinanceCoinMSignedClient,
    symbol: str,
    spec: UsdMFuturesSymbol,
    hold: Side,
    amt: float | None,
    mark: float,
    sig: TrendSignal,
    tparams: TrendModelParams,
    pcfg: StrategyLoopParams,
    rk: RiskEngine,
    limits: RiskLimits,
    account: Mapping[str, Any],
    live_leaf: dict[str, Any],
    trail_frac: float | None,
    effective_leverage: int,
    emit: LoggerFn,
    atr: float | None = None,
    atr_stop_k: float | None = None,
) -> None:
    """浮盈滚仓：趋势仍强 + 浮盈 + 次数未满 → 风控增量 sizing → MARKET 加仓。"""
    add_count = parse_add_count(live_leaf)
    if add_count >= pcfg.max_add_per_round:
        emit(
            f"[live][roll_skip] add_count={add_count} >= max_add_per_round={pcfg.max_add_per_round}"
        )
        return

    er = live_leaf.get("entry_reference")
    entry_ref = float(er) if isinstance(er, (int, float)) else float(mark)
    qty_existing = abs(float(amt)) if amt is not None else 0.0
    if qty_existing <= 0.0:
        emit("[live][roll_skip] no_position_qty")
        return

    if not has_float_profit(
        side=hold,
        quantity=qty_existing,
        avg_entry=entry_ref,
        mark=mark,
        min_return_fraction=pcfg.min_unrealized_profit_fraction,
    ):
        emit("[live][roll_skip] no_unrealized_profit")
        return

    if not trend_allows_add(holding=hold, sig=sig, tparams=tparams):
        emit(
            f"[live][roll_skip] trend_not_strong signal={sig.side.value} score={sig.score:+.4f}"
        )
        return

    stop_px = _fixed_stop_price(hold, mark, pcfg.stop_adverse_fraction)
    equity_lv = estimate_usdt_equity_for_risk(account=account)
    avail_margin = estimate_available_margin_usdt(account=account)
    step_sz = _float_step(spec.market_step_size or spec.step_size)
    min_raw = spec.market_min_qty or spec.min_qty
    try:
        min_q_v = float(min_raw) if min_raw else 0.0
    except (TypeError, ValueError):
        min_q_v = 0.0
    cm = usdm_linear_contract_multiplier(spec)
    min_ntl = parse_min_notional_usdt(spec)

    ae = rk.evaluate_add(
        ts=time.time(),
        equity=equity_lv,
        entry_price=mark,
        stop_price=stop_px,
        side=hold,
        p=pcfg.kelly_p,
        b=pcfg.kelly_b,
        existing_quantity=qty_existing,
        existing_avg_entry=entry_ref,
        quantity_step=step_sz,
        min_quantity=min_q_v,
        contract_multiplier=cm,
        min_notional_usdt=min_ntl,
        available_margin_usdt=avail_margin,
        initial_leverage=max(int(effective_leverage), 1),
    )
    if not ae.allow:
        emit(f"[live][roll_reject] {'; '.join(ae.reasons)}")
        return

    existing_ntl = notional_usdt(qty_existing, mark) * cm
    pre = precheck_usdm_market_open(
        spec=spec,
        quantity_raw=float(ae.incremental_quantity),
        entry_price=mark,
        equity_usdt=equity_lv,
        available_margin_usdt=avail_margin,
        limits_max_position_fraction=limits.max_position_fraction,
        limits_max_single_loss_fraction=limits.max_single_loss_fraction,
        implied_loss_at_stop_fraction=ae.implied_loss_at_stop_fraction_of_equity,
        initial_leverage=max(int(effective_leverage), 1),
        existing_position_notional_usdt=existing_ntl,
    )
    if not pre.ok:
        emit(f"[live][roll_reject] precheck: {'; '.join(pre.reasons)}")
        return

    emit(
        f"[live][roll_pass] add #{add_count + 1} inc_qty={pre.quantity:.8g} "
        f"total≈{ae.total_quantity_after:.8g} avg_entry≈{ae.avg_entry_after:.8g} "
        f"equity≈{equity_lv:.4f} implied_loss_frac≈{ae.implied_loss_at_stop_fraction_of_equity:.4f}"
    )
    _execute_add_flow(
        signed_client=signed_client,
        symbol=symbol,
        spec=spec,
        hold_side=hold,
        qty_add=pre.quantity,
        mark=mark,
        emit=emit,
        live_leaf=live_leaf,
        trail_frac=trail_frac,
        adverse_frac=float(pcfg.stop_adverse_fraction),
        atr=atr,
        atr_stop_k=atr_stop_k,
    )


def run_live_strategy_iteration(
    *,
    settings: Mapping[str, Any],
    signed_client: BinanceCoinMSignedClient,
    position_manager: PositionManager,
    state_store: StateStore,
    params: StrategyLoopParams | None = None,
    emit: LoggerFn | None = None,
    intervals: tuple[str, ...] | None = None,
    trend_params: TrendModelParams | None = None,
    clear_entry_pause: bool = False,
) -> None:
    log = get_logger("usdm_auto_trade")
    out: LoggerFn = emit or log.info
    rb = intervals if intervals is not None else intervals_from_settings(settings)
    pcfg = params or parse_strategy_loop_params(settings)

    from roll.signed_guard import assert_signed_trading_allowed

    assert_signed_trading_allowed(
        environment=str(settings.get("environment", "testnet")),
        rest_base=signed_client.config.rest_base,
        api_prefix=signed_client.config.api_prefix,
        product=signed_client.config.product,
        testnet_signed_orders_enabled=pcfg.testnet_signed_orders_enabled,
        live_trading_enabled=pcfg.live_trading_enabled,
        command_label="strategy_loop",
    )
    if pcfg.public_rest_base:
        pu = str(pcfg.public_rest_base).rstrip("/").lower()
        su = str(signed_client.config.rest_base).rstrip("/").lower()
        if pu != su:
            raise RuntimeError("自动交易下 strategy.public_rest_base 必须与 binance.rest_base（signed）完全一致或留空")

    from roll.account_modes import ensure_symbol_margin_type, parse_margin_mode_settings

    tpl = TrendModel(trend_params) if trend_params is not None else TrendModel()
    tpar = tpl.params
    margin_cfg = parse_margin_mode_settings(settings)
    trail_frac: float | None = None
    if pcfg.trail_stop_fraction is not None and pcfg.trail_stop_fraction > 0:
        trail_frac = float(pcfg.trail_stop_fraction)

    live_leaf: dict[str, Any] = {}
    clear_live_snap = False
    cooldown_until_ms: int | None = None
    limits, rk = RiskLimits(), RiskEngine(RiskLimits())
    post_exit_ts: float | None = None

    try:
        if clear_entry_pause:
            position_manager.resume_opening_entries()
            out("[live] 已解除暂停开仓（persisted pause cleared）")

        reconcile_and_restore_pm(position_manager, signed_client)

        persisted = state_store.load()
        pl = persisted.last_signal.get("live") if isinstance(persisted.last_signal, dict) else {}
        live_leaf.update(pl if isinstance(pl, dict) else {})

        limits, rk = _load_live_risk_engine(settings=settings, persisted=persisted)
        cooldown_until_ms = persisted.cooldown_until_unix_ms

        ts_now = time.time()
        account = signed_client.account()
        equity_now = estimate_usdt_equity_for_risk(account=account)
        circ = rk.monitor.update_equity(ts_now, equity_now)
        rk.monitor.clear_cooldown_if_expired(ts_now)
        _apply_account_circuit_pause(position_manager, rk, circ_reasons=circ.block_reasons, emit=out)
        cooldown_until_ms = _merge_cooldown_until_ms(
            persisted_ms=cooldown_until_ms,
            monitor_cooldown_ts=rk.monitor.state.cooldown_until_ts,
            post_exit_ts=None,
            cooldown_seconds=limits.cooldown_seconds,
        )

        if position_manager.halt_automatic_trading:
            out(f"[live][halted] reason={position_manager.halt_reason!r}")

        if position_manager.lock_state is TradeLockState.ENTERING and position_manager.active_symbol:
            symx = position_manager.active_symbol.upper()
            out(f"[live] ENTERING 占用 {symx} —— 等待未完成入场委托；本轮不新开仓")

        if position_manager.lock_state is TradeLockState.IN_POSITION and position_manager.active_symbol:
            sym = position_manager.active_symbol.upper()
            spec = signed_client.get_usdm_spec(sym)
            if spec is None:
                raise RuntimeError(f"unknown symbol {sym!r}")

            mrk = fetch_ticker_px(signed_client, sym)
            prrows = signed_client.position_risk(symbol=sym)
            amt, hh = _net_position_any(prrows, sym)
            hold = infer_hold_side(amt, hh)

            sig = evaluate_symbol_offline_public(
                sym,
                rest_base=signed_client.config.rest_base.rstrip("/"),
                api_prefix=signed_client.config.api_prefix,
                model=tpl,
                klines_limit=pcfg.klines_limit,
                intervals=rb,
            )
            out(f"[live][trend_manage] symbol={sym} sig={sig.side.value} score={sig.score:+.4f} mark≈{mrk}")

            try:
                matched_pool, _pool_report = load_monitorable_specs(
                    signed_client, settings, min_count=pcfg.min_monitor_symbols
                )
                log_peer_symbol_signals_while_in_position(
                    signed_client=signed_client,
                    active_symbol=sym,
                    matched=matched_pool,
                    params=pcfg,
                    intervals=rb,
                    trend_params=trend_params,
                    emit=out,
                )
            except InsufficientMonitorableSymbolsError as pool_exc:
                out(f"[live][signal_only] peer_scan_skipped: {pool_exc}")

            if hold is None:
                hedge_reason = f"hedge_or_ambiguous_position symbol={sym} positionSide={hh!r}"
                out(
                    f"[live.alert] {hedge_reason} — halting automatic trading; "
                    "manual review required (cannot manage hedge in one-way mode)"
                )
                position_manager.set_halt_for_manual_review(hedge_reason)
                live_leaf["manage_error"] = "hedge_or_ambiguous_position"

            else:
                if _position_opened_unix_ms(live_leaf) is None:
                    live_leaf["position_opened_unix_ms"] = int(time.time() * 1000)

                exit_reason: str | None = None
                if should_exit_max_hold(
                    opened_unix_ms=_position_opened_unix_ms(live_leaf),
                    max_hold_hours=pcfg.max_hold_hours,
                ):
                    opened_s = float(_position_opened_unix_ms(live_leaf) or 0) / 1000.0
                    held_h = (time.time() - opened_s) / 3600.0
                    exit_reason = f"max_hold:{held_h:.2f}h>={pcfg.max_hold_hours}h"
                    out(f"[live][exit] reason={exit_reason}")
                elif should_exit_from_trend(holding=hold, sig=sig, tparams=tpar):
                    exit_reason = "trend_exit"

                if exit_reason is not None:
                    entry_ex = _position_entry_avg(prrows, sym)
                    er = live_leaf.get("entry_reference")
                    if entry_ex is not None:
                        entry_for_pnl = float(entry_ex)
                    elif isinstance(er, (int, float)):
                        entry_for_pnl = float(er)
                    else:
                        entry_for_pnl = float(mrk)
                    qty_exit = abs(float(amt)) if amt is not None else 0.0

                    cancel_protective_close_orders(signed_client, sym, emit=out)
                    position_manager.begin_exit(sym)
                    try:
                        cx = signed_client.close_symbol_position_market(symbol=sym)
                        out(f"[live] exit MARKET oid={cx.get('orderId')} reason={exit_reason}")
                        ts_exit = time.time()
                        if qty_exit > 0.0:
                            realized = linear_pnl_usdt(hold, qty_exit, entry_for_pnl, mrk)
                            rk.monitor.record_realized_pnl(ts_exit, realized)
                            out(f"[live][pnl] realized≈{realized:.4f} USDT (est. mark≈{mrk})")
                        position_manager.mark_exit_finished_to_cooldown(sym)
                        post_exit_ts = ts_exit
                        cooldown_until_ms = _merge_cooldown_until_ms(
                            persisted_ms=cooldown_until_ms,
                            monitor_cooldown_ts=rk.monitor.state.cooldown_until_ts,
                            post_exit_ts=post_exit_ts,
                            cooldown_seconds=limits.cooldown_seconds,
                        )
                        out(f"[live][cooldown] post-exit until_unix_ms={cooldown_until_ms}")
                    except Exception:
                        position_manager.mark_exit_abort_in_position(sym)
                        raise
                    live_leaf = {}
                    clear_live_snap = True

                else:
                    ensure_symbol_margin_type(signed_client, sym, margin_cfg, emit=out)
                    prot_atr = _protective_atr_kwargs(signed_client, sym, pcfg)
                    if prot_atr.get("atr") is not None:
                        out(
                            f"[live][atr_stop] atr≈{prot_atr['atr']:.8g} "
                            f"k={prot_atr.get('atr_stop_k')} interval={pcfg.atr_interval}"
                        )
                    entry_ex = _position_entry_avg(prrows, sym)
                    er = live_leaf.get("entry_reference")
                    if entry_ex is not None:
                        entry_ref_f = float(entry_ex)
                    elif isinstance(er, (int, float)):
                        entry_ref_f = float(er)
                    else:
                        entry_ref_f = float(mrk)
                    ex0 = (
                        live_leaf.get("extreme")
                        if isinstance(live_leaf.get("extreme"), (int, float))
                        else entry_ref_f
                    )
                    ex = float(ex0)
                    ex = max(ex, mrk) if hold == "long" else min(ex, mrk)
                    live_leaf = merge_roll_live_state(
                        live_leaf,
                        entry_reference=entry_ref_f,
                        extreme=ex,
                        total_qty=abs(float(amt)) if amt is not None else None,
                    )
                    live_leaf.setdefault("add_count", parse_add_count(live_leaf))
                    live_leaf["side"] = hold

                    effective_lev = ensure_profit_tier_leverage(
                        set_leverage_fn=lambda s, lev: signed_client.set_leverage(symbol=s, leverage=lev),
                        symbol=sym,
                        side=hold,
                        avg_entry=entry_ref_f,
                        mark=mrk,
                        initial_leverage=pcfg.initial_leverage,
                        live_leaf=live_leaf,
                        emit=out,
                    )
                    _try_position_roll_add(
                        signed_client=signed_client,
                        symbol=sym,
                        spec=spec,
                        hold=hold,
                        amt=amt,
                        mark=mrk,
                        sig=sig,
                        tparams=tpar,
                        pcfg=pcfg,
                        rk=rk,
                        limits=limits,
                        account=account,
                        live_leaf=live_leaf,
                        trail_frac=trail_frac,
                        effective_leverage=effective_lev,
                        emit=out,
                        atr=prot_atr.get("atr"),
                        atr_stop_k=prot_atr.get("atr_stop_k"),
                    )

                    entry_ref_f = float(live_leaf.get("entry_reference", entry_ref_f))
                    ex = float(live_leaf.get("extreme", ex))
                    _, stp = ensure_or_roll_protective_stop(
                        client=signed_client,
                        symbol=sym,
                        spec=spec,
                        hold_side=hold,
                        entry_ref=float(entry_ref_f),
                        extreme_px=ex,
                        adverse_frac=float(pcfg.stop_adverse_fraction),
                        trail_frac=trail_frac,
                        emit=out,
                        atr=prot_atr.get("atr"),
                        atr_stop_k=prot_atr.get("atr_stop_k"),
                    )
                    live_leaf.update(
                        {"symbol": sym, "side": hold, "extreme": ex, "stop_live": stp, "mark_px": mrk}
                    )

        if _cooldown_active(cooldown_until_ms):
            remaining_s = max(0.0, (int(cooldown_until_ms) - int(time.time() * 1000)) / 1000.0)
            out(f"[live][cooldown] active remaining≈{remaining_s:.0f}s — no candidate scan")
        elif cooldown_until_ms is not None:
            cooldown_until_ms = None
            if position_manager.lock_state is TradeLockState.COOLDOWN:
                try:
                    position_manager.finish_cooldown_to_idle()
                except Exception:
                    pass
            out("[live][cooldown] expired — candidate scan allowed")

        if position_manager.allow_scan_candidates() and not _cooldown_active(cooldown_until_ms):
            signed_client.sync_server_time()
            matched, report = load_monitorable_specs(
                signed_client, settings, min_count=pcfg.min_monitor_symbols
            )
            out("[symbols_monitor_pool]")
            out(report.format_human_readable())

            assessed = evaluate_candidates_with_public_rest(
                signed_client, matched, params=pcfg, intervals=rb, trend_params=trend_params
            )
            ranked = rank_directional_signals(assessed)
            specs_map = {s.symbol.upper(): s for s in matched}

            ts = time.time()
            account = signed_client.account()
            acct_snap = parse_usdt_account_snapshot(account)
            avail_margin = acct_snap.available_margin_usdt
            equity_lv = estimate_usdt_equity_for_risk(account=account)
            rk.monitor.update_equity(ts, equity_lv)
            rk.monitor.clear_cooldown_if_expired(ts)
            out(
                f"[account_usdt] equity≈{acct_snap.equity_usdt:.4f} "
                f"available≈{avail_margin:.4f} wallet≈{acct_snap.wallet_balance_usdt:.4f} "
                f"upnl≈{acct_snap.unrealized_profit_usdt:.4f}"
            )
            out("[trend_scan]")
            for sym_e, sg in assessed:
                rr = "; ".join(sg.rejection_reasons) if sg.rejection_reasons else ""
                out(f"  {sym_e} signal={sg.side.value} mixed={sg.score:+.4f}" + (f" reject={rr}" if rr else ""))

            if not ranked:
                out("[live] NO_OPEN — 无多空方向候选")
            else:
                sym_pick_o: str | None = None
                side_pick_o = None
                qty_for_open = 0.0

                for rank_idx, (spx_v, cand_sig_v) in enumerate(ranked, start=1):
                    sk_side = signal_side_to_risk_side(cand_sig_v)
                    if sk_side is None:
                        continue
                    sp_v = specs_map.get(spx_v.upper())
                    if sp_v is None:
                        continue
                    mark_c_v = fetch_ticker_px(signed_client, spx_v)
                    equity_lv = estimate_usdt_equity_for_risk(account=account)
                    stop_v = _fixed_stop_price(sk_side, mark_c_v, pcfg.stop_adverse_fraction)
                    step_sz = _float_step(sp_v.market_step_size or sp_v.step_size)
                    min_raw = sp_v.market_min_qty or sp_v.min_qty
                    try:
                        min_q_v = float(min_raw) if min_raw else 0.0
                    except (TypeError, ValueError):
                        min_q_v = 0.0
                    cm = usdm_linear_contract_multiplier(sp_v)
                    min_ntl = parse_min_notional_usdt(sp_v)

                    if pcfg.open_quantity_mode == "min_market":
                        qty_raw = resolve_open_quantity_raw(
                            spec=sp_v, risk_quantity=0.0, mode="min_market"
                        )
                        if qty_raw <= 0.0:
                            out(
                                f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} REJECT "
                                "min_market: no market min qty"
                            )
                            continue
                        pre = precheck_usdm_market_open(
                            spec=sp_v,
                            quantity_raw=qty_raw,
                            entry_price=mark_c_v,
                            equity_usdt=equity_lv,
                            available_margin_usdt=avail_margin,
                            limits_max_position_fraction=limits.max_position_fraction,
                            limits_max_single_loss_fraction=limits.max_single_loss_fraction,
                            implied_loss_at_stop_fraction=0.0,
                            initial_leverage=pcfg.initial_leverage,
                        )
                        if not pre.ok:
                            out(
                                f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} REJECT "
                                f"min_market precheck: {'; '.join(pre.reasons)}"
                            )
                            continue
                        out(
                            f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} PASS "
                            f"qty={pre.quantity:.8g} (min_market) notional≈{pre.notional_usdt:.4f} USDT"
                        )
                        sym_pick_o, side_pick_o, qty_for_open = spx_v, sk_side, pre.quantity
                        break

                    oe_try = rk.evaluate_open(
                        ts=ts,
                        equity=equity_lv,
                        entry_price=mark_c_v,
                        stop_price=stop_v,
                        side=sk_side,
                        p=pcfg.kelly_p,
                        b=pcfg.kelly_b,
                        quantity_step=step_sz,
                        min_quantity=min_q_v,
                        contract_multiplier=cm,
                        min_notional_usdt=min_ntl,
                        available_margin_usdt=avail_margin,
                        initial_leverage=pcfg.initial_leverage,
                    )
                    qty_raw = resolve_open_quantity_raw(
                        spec=sp_v,
                        risk_quantity=float(oe_try.quantity) if oe_try.allow else 0.0,
                        mode=pcfg.open_quantity_mode,
                    )

                    if oe_try.allow and oe_try.position is not None:
                        pre = precheck_usdm_market_open(
                            spec=sp_v,
                            quantity_raw=qty_raw,
                            entry_price=mark_c_v,
                            equity_usdt=equity_lv,
                            available_margin_usdt=avail_margin,
                            limits_max_position_fraction=limits.max_position_fraction,
                            limits_max_single_loss_fraction=limits.max_single_loss_fraction,
                            implied_loss_at_stop_fraction=oe_try.position.implied_loss_at_stop_fraction_of_equity,
                            initial_leverage=pcfg.initial_leverage,
                        )
                        if not pre.ok:
                            out(
                                f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} REJECT "
                                f"precheck: {'; '.join(pre.reasons)}"
                            )
                            continue
                        out(
                            f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} PASS "
                            f"qty={pre.quantity:.8g} notional≈{pre.notional_usdt:.4f} USDT "
                            f"entry≈{mark_c_v:.8g} equity≈{equity_lv:.4f}"
                        )
                        sym_pick_o, side_pick_o, qty_for_open = spx_v, sk_side, pre.quantity
                        break
                    reasons_join = "; ".join(oe_try.reasons)
                    out(f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} REJECT {reasons_join}")

                if sym_pick_o and side_pick_o and qty_for_open > 0:
                    ensure_symbol_margin_type(signed_client, sym_pick_o, margin_cfg, emit=out)
                    px_open = fetch_ticker_px(signed_client, sym_pick_o)
                    prot_open = _protective_atr_kwargs(signed_client, sym_pick_o, pcfg)
                    _execute_open_flow(
                        signed_client=signed_client,
                        pm=position_manager,
                        symbol=sym_pick_o,
                        spec=specs_map[sym_pick_o.upper()],
                        side_pick=side_pick_o,
                        qty_pick=qty_for_open,
                        params=pcfg,
                        mark=px_open,
                        emit=out,
                        live_leaf=live_leaf,
                        trail_frac=trail_frac,
                        atr=prot_open.get("atr"),
                        atr_stop_k=prot_open.get("atr_stop_k"),
                    )

    except InsufficientMonitorableSymbolsError:
        raise
    except Exception as e:
        position_manager.pause_opening_entries(f"{type(e).__name__}: {e}")
        log.exception("[live.error] iteration failed — paused new openings; protects existing manages next ticks")

    finally:
        persist_autotrade_runtime(
            state_store,
            pm=position_manager,
            live_leaf=live_leaf,
            clear_live=clear_live_snap,
            rk=rk,
            cooldown_until_unix_ms=cooldown_until_ms,
        )
