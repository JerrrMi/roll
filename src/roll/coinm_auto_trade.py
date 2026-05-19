"""COIN-M Futures signed 自动交易闭环（Testnet / live 共用）：对账→信号→杠杆→开仓→STOP 保护→追踪止损→反转平仓。

须通过 `signed_guard.assert_signed_trading_allowed` 放行；live 另须单进程锁（见 `process_lock`）。
异常开仓路径：`PositionManager.pause_opening_entries()` 暂停新开仓；已有持仓仍可维护保护单并按信号平仓。"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from roll.binance_client import (
    BinanceCoinMSignedClient,
    BinanceHTTPError,
    CoinMFuturesSymbol,
    format_floor_to_step_decimal_str,
    format_price_to_tick_decimal_str,
    select_monitorable_coin_m_symbols,
)
from roll.logger import get_logger
from roll.market_data import parse_candidate_assets
from roll.offline_trend import evaluate_symbol_offline_public
from roll.position_manager import PositionManager, TradeLockState, reconcile_coin_m_account
from roll.risk import RiskEngine, RiskLimits, Side, fixed_stop_price as _fixed_stop_price, trailing_stop_price
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
from roll.trend_model import SignalSide, TrendModel, TrendModelParams, TrendSignal

LoggerFn = Callable[[str], None]


def reconcile_and_restore_pm(pm: PositionManager, client: BinanceCoinMSignedClient) -> None:
    client.sync_server_time()
    outcome = reconcile_coin_m_account(client.position_risk(), client.open_orders())
    pm.restore_from_exchange(outcome)


def _marginal_wallet_balance_coin(account: Mapping[str, Any], margin_asset: str) -> Decimal:
    rows = account.get("assets")
    lst = rows if isinstance(rows, list) else []
    ma = margin_asset.strip().upper()
    for row in lst:
        if not isinstance(row, Mapping):
            continue
        ast = row.get("asset")
        if str(ast).upper() != ma:
            continue
        wb = Decimal(str(row.get("walletBalance", "0")))
        pr = Decimal(str(row.get("unrealizedProfit", "0")))
        return wb + pr
    return Decimal(0)


def estimate_quote_equity_for_risk(
    *,
    account: Mapping[str, Any],
    spec: CoinMFuturesSymbol,
    mark_price_quote: float,
) -> float:
    bal = _marginal_wallet_balance_coin(account, spec.margin_asset)
    px = Decimal(str(mark_price_quote))
    if bal <= 0 or px <= 0:
        return 0.0
    return float(bal * px)


def _lots(spec: CoinMFuturesSymbol, *, market: bool) -> tuple[str, str, str]:
    return spec.tick_size, spec.market_min_qty if market else spec.min_qty, spec.market_step_size if market else spec.step_size


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
) -> float:
    base = _fixed_stop_price(side, entry_px, adverse_frac)
    if trail_frac is None or trail_frac <= 0:
        return base
    trail_px = trailing_stop_price(side, extreme_px, trail_fraction=float(trail_frac))
    return max(base, trail_px) if side == "long" else min(base, trail_px)


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
        return sig.side is SignalSide.SHORT and (-sig.score) >= tparams.short_threshold
    if holding == "short":
        return sig.side is SignalSide.LONG and sig.score >= tparams.long_threshold
    return False


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
) -> None:
    snap = pm.snapshot_dict()
    prev = store.load()
    merged = merge_live_leaf(prev.last_signal if isinstance(prev.last_signal, dict) else {}, live_leaf, clear_live=clear_live)
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
            cooldown_until_unix_ms=prev.cooldown_until_unix_ms,
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
    spec: CoinMFuturesSymbol,
    hold_side: Side,
    entry_ref: float,
    extreme_px: float,
    adverse_frac: float,
    trail_frac: float | None,
    emit: LoggerFn,
) -> tuple[float, str]:
    tick, _mn, _st = _lots(spec, market=True)
    want_raw = desired_protective_stop_price(hold_side, entry_ref, extreme_px, adverse_frac, trail_frac)
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


def _execute_open_flow(
    *,
    signed_client: BinanceCoinMSignedClient,
    pm: PositionManager,
    symbol: str,
    spec: CoinMFuturesSymbol,
    side_pick: Side,
    qty_pick: float,
    params: StrategyLoopParams,
    mark: float,
    emit: LoggerFn,
    live_leaf: dict[str, Any],
    trail_frac: float | None,
) -> None:
    pm.begin_enter(symbol)
    tick, _mn, step = _lots(spec, market=True)
    if not tick or not step:
        pm.rollback_enter_to_idle()
        raise ValueError("合约缺少 tick_size / MARKETLOT step")
    try:
        signed_client.set_leverage(symbol=symbol, leverage=params.initial_leverage)
        qty_s = format_floor_to_step_decimal_str(str(qty_pick), step)
        emit(f"[live] MARKET_OPEN symbol={symbol} side={side_pick} qty={qty_s}")
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
        live_leaf.update({"entry_reference": entry_ref, "extreme": ext, "side": side_pick, "qty": qty_s})
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
        )
        live_leaf.update({"stop_live": stops, "symbol": symbol.upper(), "extreme": float(_ext2), "mark_px": mark})
    except Exception:
        pm.rollback_enter_to_idle()
        raise


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
    log = get_logger("strategy_loop.live")
    out: LoggerFn = emit or log.info
    rb = intervals if intervals is not None else intervals_from_settings(settings)
    pcfg = params or parse_strategy_loop_params(settings)

    from roll.signed_guard import assert_signed_trading_allowed

    assert_signed_trading_allowed(
        environment=str(settings.get("environment", "testnet")),
        rest_base=signed_client.config.rest_base,
        testnet_signed_orders_enabled=pcfg.testnet_signed_orders_enabled,
        live_trading_enabled=pcfg.live_trading_enabled,
        command_label="strategy_loop",
    )
    if pcfg.public_rest_base:
        pu = str(pcfg.public_rest_base).rstrip("/").lower()
        su = str(signed_client.config.rest_base).rstrip("/").lower()
        if pu != su:
            raise RuntimeError("自动交易下 strategy.public_rest_base 必须与 binance.rest_base（signed）完全一致或留空")

    tpl = TrendModel(trend_params) if trend_params is not None else TrendModel()
    tpar = tpl.params
    trail_frac: float | None = None
    if pcfg.trail_stop_fraction is not None and pcfg.trail_stop_fraction > 0:
        trail_frac = float(pcfg.trail_stop_fraction)

    live_leaf: dict[str, Any] = {}
    clear_live_snap = False

    try:
        if clear_entry_pause:
            position_manager.resume_opening_entries()
            out("[live] 已解除暂停开仓（persisted pause cleared）")

        reconcile_and_restore_pm(position_manager, signed_client)

        persisted = state_store.load()
        pl = persisted.last_signal.get("live") if isinstance(persisted.last_signal, dict) else {}
        live_leaf.update(pl if isinstance(pl, dict) else {})

        if position_manager.halt_automatic_trading:
            out(f"[live][halted] reason={position_manager.halt_reason!r}")

        elif position_manager.lock_state is TradeLockState.ENTERING and position_manager.active_symbol:
            symx = position_manager.active_symbol.upper()
            out(f"[live] ENTERING 占用 {symx} —— 等待未完成入场委托；本轮不新开仓")

        elif position_manager.lock_state is TradeLockState.IN_POSITION and position_manager.active_symbol:
            sym = position_manager.active_symbol.upper()
            spec = signed_client.get_coin_m_spec(sym)
            if spec is None:
                raise RuntimeError(f"unknown symbol {sym!r}")

            mrk = fetch_ticker_px(signed_client, sym)
            prrows = signed_client.position_risk(symbol=sym)
            amt, hh = _net_position_any(prrows, sym)
            hold = infer_hold_side(amt, hh)

            sig = evaluate_symbol_offline_public(
                sym,
                rest_base=signed_client.config.rest_base.rstrip("/"),
                coin_m_prefix=signed_client.config.coin_m_prefix,
                model=tpl,
                klines_limit=pcfg.klines_limit,
                intervals=rb,
            )
            out(f"[live][trend_manage] sig={sig.side.value} score={sig.score:+.4f} mark≈{mrk}")

            if hold is None:
                out("[live.warn] 持仓腿无法归入单向模式；跳过自动管理直至人工处理对冲")
                live_leaf["manage_error"] = "hedge_or_ambiguous_position"

            elif should_exit_from_trend(holding=hold, sig=sig, tparams=tpar):
                cancel_protective_close_orders(signed_client, sym, emit=out)
                position_manager.begin_exit(sym)
                try:
                    cx = signed_client.close_symbol_position_market(symbol=sym)
                    out(f"[live] exit MARKET oid={cx.get('orderId')}")
                    position_manager.mark_exit_finished_to_cooldown(sym)
                    position_manager.finish_cooldown_to_idle()
                except Exception:
                    position_manager.mark_exit_abort_in_position(sym)
                    raise
                live_leaf = {}
                clear_live_snap = True

            else:
                er = live_leaf.get("entry_reference") if isinstance(live_leaf.get("entry_reference"), (int, float)) else mrk
                entry_ref_f = float(er)
                ex0 = (
                    live_leaf.get("extreme")
                    if isinstance(live_leaf.get("extreme"), (int, float))
                    else entry_ref_f
                )
                ex = float(ex0)
                ex = max(ex, mrk) if hold == "long" else min(ex, mrk)
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
                )
                live_leaf.update({"symbol": sym, "side": hold, "extreme": ex, "stop_live": stp, "mark_px": mrk})

        elif position_manager.allow_scan_candidates():
            signed_client.sync_server_time()
            specs_full = signed_client.list_coin_m_specs()
            candidates = parse_candidate_assets(dict(settings))

            matched, report = select_monitorable_coin_m_symbols(
                specs_full,
                candidates,
                min_count=pcfg.min_monitor_symbols,
            )
            out("[symbols_monitor_pool]")
            out(report.format_human_readable())

            assessed = evaluate_candidates_with_public_rest(
                signed_client, matched, params=pcfg, intervals=rb, trend_params=trend_params
            )
            ranked = rank_directional_signals(assessed)
            specs_map = {s.symbol.upper(): s for s in matched}
            limits = RiskLimits()
            rk = RiskEngine(limits)

            ts = time.time()
            account = signed_client.account()
            out("[trend_scan]")
            for sym_e, sg in assessed:
                rr = "; ".join(sg.rejection_reasons) if sg.rejection_reasons else ""
                out(f"  {sym_e} signal={sg.side.value} mixed={sg.score:+.4f}" + (f" reject={rr}" if rr else ""))

            if not ranked:
                out("[live] NO_OPEN — 无多空方向候选")
            else:
                sym_pick_o: str | None = None
                side_pick_o = None
                oe_pick_o = None

                for rank_idx, (spx_v, cand_sig_v) in enumerate(ranked, start=1):
                    sk_side = signal_side_to_risk_side(cand_sig_v)
                    if sk_side is None:
                        continue
                    sp_v = specs_map.get(spx_v.upper())
                    if sp_v is None:
                        continue
                    mark_c_v = fetch_ticker_px(signed_client, spx_v)
                    equity_lv = estimate_quote_equity_for_risk(
                        account=account, spec=sp_v, mark_price_quote=mark_c_v
                    )
                    stop_v = _fixed_stop_price(sk_side, mark_c_v, pcfg.stop_adverse_fraction)
                    step_sz = _float_step(sp_v.market_step_size or sp_v.step_size)
                    min_raw = sp_v.market_min_qty or sp_v.min_qty
                    try:
                        min_q_v = float(min_raw) if min_raw else 0.0
                    except (TypeError, ValueError):
                        min_q_v = 0.0

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
                        contract_multiplier=max(sp_v.contract_size, 1e-12),
                    )
                    if oe_try.allow:
                        out(
                            f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} PASS "
                            f"qty={oe_try.quantity:.8g} entry≈{mark_c_v:.8g} equity≈{equity_lv:.4f}"
                        )
                        sym_pick_o, side_pick_o, oe_pick_o = spx_v, sk_side, oe_try
                        break
                    reasons_join = "; ".join(oe_try.reasons)
                    out(f"[risk_try rank={rank_idx}] symbol={spx_v} side={sk_side} REJECT {reasons_join}")

                if sym_pick_o and side_pick_o and oe_pick_o:
                    px_open = fetch_ticker_px(signed_client, sym_pick_o)
                    _execute_open_flow(
                        signed_client=signed_client,
                        pm=position_manager,
                        symbol=sym_pick_o,
                        spec=specs_map[sym_pick_o.upper()],
                        side_pick=side_pick_o,
                        qty_pick=float(oe_pick_o.quantity),
                        params=pcfg,
                        mark=px_open,
                        emit=out,
                        live_leaf=live_leaf,
                        trail_frac=trail_frac,
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
        )



