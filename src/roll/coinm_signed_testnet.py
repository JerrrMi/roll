"""COIN-M Signed REST — Binance Futures **Testnet** 验收闭环。

不向日志写入 API Secret；异常与输出中的 URL 已通过 `redact_signed_query_url` 去除 signature。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Callable

from roll.binance_client import (
    BinanceClientConfig,
    BinanceCoinMClient,
    BinanceCoinMSignedClient,
    BinanceHTTPError,
    CoinMFuturesSymbol,
    is_binance_coin_m_testnet_url,
    redact_signed_query_url,
)


@dataclass(frozen=True)
class SignedAcceptanceRow:
    name: str
    ok: bool
    detail: str


Reporter = Callable[[str], None]


def _dust_limit_buy_price(pub: BinanceCoinMClient, spec: CoinMFuturesSymbol) -> tuple[str, str]:
    """返回 (tick_size, 远低于市价的买一限价字符串)。"""
    tick = spec.tick_size
    mn_price = ""
    mx_price = ""
    for f in spec.filters_raw:
        if f.get("filterType") == "PRICE_FILTER":
            mn_price = str(f.get("minPrice", ""))
            mx_price = str(f.get("maxPrice", ""))
            tick = str(f.get("tickSize", tick))
            break
    if not tick:
        raise ValueError(f"{spec.symbol}: 缺少 PRICE_FILTER.tickSize")

    ticker = pub.ticker_price(spec.symbol)
    if not ticker or "price" not in ticker[0]:
        raise BinanceHTTPError(f"ticker missing price for {spec.symbol!r}")
    ref = Decimal(str(ticker[0]["price"]))
    divisor = Decimal(50)
    cand = ref / divisor
    if mn_price:
        cand = cand.max(Decimal(mn_price))
    if mx_price:
        cand = cand.min(Decimal(mx_price))

    tk = Decimal(tick)
    floored = (cand / tk).to_integral_value(rounding=ROUND_DOWN) * tk
    ps = format(floored, "f")
    if "." in ps:
        ps = ps.rstrip("0").rstrip(".")
    return tick, ps


def _explain_http(exc: BinanceHTTPError, *, endpoint: str, symbol: str) -> str:
    return (
        f"endpoint={endpoint} symbol={symbol} http_status={exc.status_code} "
        f"code={exc.code} msg={exc.msg} safe_url={exc.url}"
    )


def _nonzero_positions(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    want = symbol.upper()
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("symbol", "")).upper() != want:
            continue
        am = r.get("positionAmt")
        if not isinstance(am, str):
            continue
        try:
            if Decimal(am) != 0:
                out.append(r)
        except InvalidOperation:
            continue
    return out


def run_signed_testnet_acceptance(
    *,
    rest_base: str,
    api_prefix: str,
    recv_window_ms: int,
    symbol: str,
    leverage_to_set: int,
    api_key: str | None,
    api_secret: str | None,
    report: Reporter,
) -> list[SignedAcceptanceRow]:
    """顺序执行_signed 能力与「开仓→查持仓→平仓→无持仓」闭环（Testnet ONLY）。"""
    rows: list[SignedAcceptanceRow] = []

    def ok(name: str, detail: str) -> None:
        rows.append(SignedAcceptanceRow(name=name, ok=True, detail=detail))

    def bad(name: str, detail: str) -> SignedAcceptanceRow:
        row = SignedAcceptanceRow(name=name, ok=False, detail=detail)
        rows.append(row)
        return row

    def summarize() -> None:
        report("--- acceptance rows ---")
        for r in rows:
            report(f"[{'OK' if r.ok else 'FAIL'}] {r.name}: {r.detail}")
        report(
            f"(diagnostic) scrubbed_signed_url="
            f"{redact_signed_query_url('https://x/y?a=1&signature=abcd')}"
        )

    sy = symbol.strip().upper()
    pub_cfg = BinanceClientConfig(
        rest_base=rest_base,
        api_prefix=api_prefix,
        recv_window_ms=recv_window_ms,
    )
    pub = BinanceCoinMClient(pub_cfg)

    report(f"USD-M Signed Testnet 验收 symbol={sy} REST={rest_base} api_prefix={api_prefix}")

    if not is_binance_coin_m_testnet_url(rest_base):
        bad(
            "guard_testnet_rest_base",
            f"禁止在非 Testnet REST 调用 signed 下单：rest_base={rest_base!r} "
            f"(只允许 host in testnet.binancefuture.com)",
        )
        summarize()
        return rows

    ck = api_key.strip() if isinstance(api_key, str) else None
    cs = api_secret.strip() if isinstance(api_secret, str) else None
    if not ck or not cs:
        bad(
            "guard_api_credentials",
            "缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET（--secrets-file / secrets.file / 环境变量；禁止打印 Secret）。",
        )
        summarize()
        return rows

    signed_cfg = BinanceClientConfig(
        rest_base=rest_base,
        api_prefix=api_prefix,
        recv_window_ms=recv_window_ms,
        api_key=ck,
        api_secret=cs,
    )

    signed: BinanceCoinMSignedClient | None = None
    try:
        signed = BinanceCoinMSignedClient(signed_cfg)
    except ValueError as e:
        bad("client_construct", repr(e))
        summarize()
        return rows

    assert signed is not None

    # --- bootstrap & read-only ---
    try:
        off = signed.sync_server_time()
        ok("sync_server_time", f"offset_ms={off}")
    except BinanceHTTPError as e:
        bad("sync_server_time", _explain_http(e, endpoint="GET /time", symbol="-"))
        summarize()
        return rows
    except Exception as e:  # noqa: BLE001
        bad("sync_server_time", f"unexpected: {e!r}")
        summarize()
        return rows

    try:
        acct = signed.account()
        assets = acct.get("assets") if isinstance(acct.get("assets"), list) else []
        poses = acct.get("positions") if isinstance(acct.get("positions"), list) else []
        ok(
            "GET /account",
            f"assets={len(assets)} positions_preview={len(poses)} feeTier={acct.get('feeTier')}",
        )
    except BinanceHTTPError as e:
        bad("GET /account", _explain_http(e, endpoint="GET /fapi/v1/account", symbol=sy))

    try:
        pr_before = signed.position_risk(symbol=sy)
        ok("GET /positionRisk(open)", f"rows={len(pr_before)} nonzero={len(_nonzero_positions(pr_before, sy))}")
    except BinanceHTTPError as e:
        bad("GET /positionRisk(open)", _explain_http(e, endpoint="GET /fapi/v1/positionRisk", symbol=sy))

    try:
        oo0 = signed.open_orders(symbol=sy)
        ok("GET /openOrders(empty)", f"count={len(oo0)}")
    except BinanceHTTPError as e:
        bad("GET /openOrders(empty)", _explain_http(e, endpoint="GET /fapi/v1/openOrders", symbol=sy))

    try:
        lev = signed.set_leverage(symbol=sy, leverage=int(leverage_to_set))
        ok("POST /leverage", f"leverage_hint={lev.get('leverage', lev)} symbol={sy}")
    except BinanceHTTPError as e:
        bad("POST /leverage", _explain_http(e, endpoint="POST /fapi/v1/leverage", symbol=sy))

    spec = pub.get_coin_m_spec(sy)
    if spec is None:
        bad("exchangeInfo/spec", f"找不到 {sy!r} 的合约规则")
        summarize()
        return rows

    try:
        min_q = signed.min_marketable_quantity_string(sy, prefer_market=True)
        ok("exchangeInfo/min_qty(hint)", f"min_qty={min_q}")
    except Exception as e:  # noqa: BLE001
        bad("exchangeInfo/min_qty(hint)", f"{type(e).__name__}: {e}")

    oid_open: int | None = None
    open_ok = False
    mid_pos_ok = False
    close_ok = False
    flat_ok = False
    try:
        mo = signed.new_market_order(symbol=sy, side="BUY", quantity=min_q, reduce_only=False)
        oid_raw = mo.get("orderId")
        oid_open = int(oid_raw) if oid_raw is not None else None
        open_ok = True
        ok(
            "POST /order(market_buy_open)",
            f"orderId={oid_open} status={mo.get('status')} avgPx={mo.get('avgPrice', mo.get('price'))}",
        )
    except BinanceHTTPError as e:
        bad("POST /order(market_buy_open)", _explain_http(e, endpoint="POST /fapi/v1/order", symbol=sy))

    if oid_open is not None:
        try:
            qo = signed.get_order(symbol=sy, order_id=oid_open)
            ok("GET /order(filled/open)", f"status={qo.get('status')} executedQty={qo.get('executedQty')}")
        except BinanceHTTPError as e:
            bad("GET /order(filled/open)", _explain_http(e, endpoint="GET /fapi/v1/order", symbol=sy))

    held = False
    try:
        pr_mid = signed.position_risk(symbol=sy)
        nn = _nonzero_positions(pr_mid, sy)
        held = len(nn) > 0
        mid_pos_ok = held
        ok("GET /positionRisk(after_buy)", f"nonzero_positions={len(nn)} held={held}")
    except BinanceHTTPError as e:
        bad("GET /positionRisk(after_buy)", _explain_http(e, endpoint="GET /fapi/v1/positionRisk", symbol=sy))

    try:
        cl = signed.close_symbol_position_market(symbol=sy)
        close_ok = True
        ok(
            "POST /order(market_close_reduce_only)",
            f"orderId={cl.get('orderId')} status={cl.get('status')}",
        )
    except BinanceHTTPError as e:
        bad("POST /order(market_close_reduce_only)", _explain_http(e, endpoint="POST /fapi/v1/order(close)", symbol=sy))

    try:
        pr_after = signed.position_risk(symbol=sy)
        flat_ok = len(_nonzero_positions(pr_after, sy)) == 0
        ok("GET /positionRisk(after_close)", f"flat={flat_ok} rows={len(pr_after)}")
    except BinanceHTTPError as e:
        bad("GET /positionRisk(after_close)", _explain_http(e, endpoint="GET /fapi/v1/positionRisk", symbol=sy))

    loop_ok = open_ok and mid_pos_ok and close_ok and flat_ok
    if loop_ok:
        ok("闭环(smoke_summary)", "open → nonzero positionRisk → reduceOnly close → flat")
    else:
        bad(
            "闭环(smoke_summary)",
            f"open_ok={open_ok} mid_nonempty={mid_pos_ok} close_ok={close_ok} flat_ok={flat_ok}",
        )

    # --- DELETE /order 闭环：挂单后立即撤单 ---
    lid: int | None = None
    try:
        tick, dust_px_s = _dust_limit_buy_price(pub, spec)
        lo = signed.new_limit_order(symbol=sy, side="BUY", quantity=min_q, price=dust_px_s)
        oid_l = lo.get("orderId")
        lid = int(oid_l) if oid_l is not None else None
        oo1 = signed.open_orders(symbol=sy)
        ok(
            "GET /openOrders(with_limit)",
            f"open_count={len(oo1)} limit_orderId={lid} dust_price≈{dust_px_s}(tick={tick})",
        )
    except BinanceHTTPError as e:
        bad(
            "GET /openOrders(with_limit)",
            _explain_http(e, endpoint="composite limit+openOrders", symbol=sy),
        )
    except (InvalidOperation, ValueError) as e:
        bad("limit_for_cancel_prep", repr(e))

    if lid is not None:
        try:
            cx = signed.cancel_order(symbol=sy, order_id=lid)
            ok("DELETE /order", f"canceled_orderId={cx.get('orderId')} status={cx.get('status')}")
        except BinanceHTTPError as e:
            bad("DELETE /order", _explain_http(e, endpoint="DELETE /fapi/v1/order", symbol=sy))

    summarize()
    return rows
