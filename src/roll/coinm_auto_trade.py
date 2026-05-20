"""历史别名：请使用 `roll.usdm_auto_trade`（USD-M /fapi 自动交易闭环）。"""
from __future__ import annotations

from roll.usdm_auto_trade import (  # noqa: F401
    cancel_protective_close_orders,
    desired_protective_stop_price,
    ensure_or_roll_protective_stop,
    estimate_available_margin_usdt,
    estimate_quote_equity_for_risk,
    estimate_usdt_equity_for_risk,
    fetch_ticker_px,
    infer_hold_side,
    load_monitorable_specs,
    log_peer_symbol_signals_while_in_position,
    merge_live_leaf,
    persist_autotrade_runtime,
    place_protective_stop_market_close,
    reconcile_and_restore_pm,
    resolve_open_quantity_raw,
    run_live_strategy_iteration,
    should_exit_from_trend,
    should_exit_max_hold,
    stop_order_side_for_protect,
)
