"""USD-M 账户模式：逐仓/全仓 marginType 校验与可选设置；单向/双向持仓模式检查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from roll.binance_client import BinanceUsdmSignedClient, BinanceHTTPError

MarginType = Literal["ISOLATED", "CROSSED"]
PositionMode = Literal["one_way", "hedge"]
LoggerFn = Callable[[str], None]


class HedgePositionModeError(RuntimeError):
    """账户处于 Hedge Mode（双向持仓），系统要求 One-way（单向净持仓）。"""


@dataclass(frozen=True)
class MarginModeSettings:
    """`settings.binance` 中的保证金模式配置。"""

    margin_type: MarginType | None = None
    apply_margin_type: bool = False


def parse_margin_mode_settings(settings: Mapping[str, Any]) -> MarginModeSettings:
    raw = settings.get("binance")
    if not isinstance(raw, dict):
        return MarginModeSettings()
    mt_raw = raw.get("margin_type")
    margin_type: MarginType | None = None
    if isinstance(mt_raw, str):
        u = mt_raw.strip().upper()
        if u in ("ISOLATED", "CROSSED"):
            margin_type = u  # type: ignore[assignment]
    apply = bool(raw.get("apply_margin_type", False))
    return MarginModeSettings(margin_type=margin_type, apply_margin_type=apply)


def normalize_margin_type_from_exchange(raw: Any) -> MarginType | None:
    if not isinstance(raw, str):
        return None
    u = raw.strip().upper()
    if u in ("ISOLATED", "ISOLATE"):
        return "ISOLATED"
    if u in ("CROSSED", "CROSS"):
        return "CROSSED"
    return None


def read_symbol_margin_type(
    client: BinanceUsdmSignedClient,
    symbol: str,
) -> MarginType | None:
    """从 positionRisk 读取 symbol 当前 marginType。"""
    sym = symbol.upper()
    for row in client.position_risk(symbol=sym):
        if str(row.get("symbol", "")).upper() != sym:
            continue
        mt = normalize_margin_type_from_exchange(row.get("marginType"))
        if mt is not None:
            return mt
    return None


def ensure_symbol_margin_type(
    client: BinanceUsdmSignedClient,
    symbol: str,
    cfg: MarginModeSettings,
    *,
    emit: LoggerFn | None = None,
) -> None:
    """校验（可选设置）symbol 的 marginType；未配置 expected 时跳过。"""
    expected = cfg.margin_type
    if expected is None:
        return
    log = emit or (lambda _m: None)
    sym = symbol.upper()
    current = read_symbol_margin_type(client, sym)
    if current is None and not cfg.apply_margin_type:
        log(
            f"[live][margin] symbol={sym} expected={expected} — "
            "无法读取 marginType；请在交易所确认或启用 apply_margin_type"
        )
        return
    if current == expected:
        log(f"[live][margin] symbol={sym} margin_type={expected} OK")
        return
    if not cfg.apply_margin_type:
        raise RuntimeError(
            f"symbol {sym} marginType={current!r} 与配置 binance.margin_type={expected!r} 不一致；"
            "请手动调整或设置 apply_margin_type: true（有持仓时交易所可能拒绝变更）"
        )
    try:
        client.change_margin_type(symbol=sym, margin_type=expected)
        log(f"[live][margin] symbol={sym} set margin_type={expected}")
    except BinanceHTTPError as e:
        if current is not None and current != expected:
            raise RuntimeError(
                f"symbol {sym} marginType={current!r} != expected {expected!r}; "
                f"change_margin_type failed: {e.msg}"
            ) from e
        log(f"[live][margin.warn] change_margin_type {sym} code={e.code} msg={e.msg}")


def parse_dual_side_position_from_account(account: Mapping[str, Any]) -> bool | None:
    """从 account 响应解析 dualSidePosition；True=hedge，False=one-way。"""
    raw = account.get("dualSidePosition")
    if raw is None:
        raw = account.get("dual_side_position")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
    return None


def read_dual_side_position(client: BinanceUsdmSignedClient) -> bool | None:
    """读取账户是否为 Hedge Mode；优先 REST `/positionSide/dual`，回退 account 字段。"""
    try:
        return client.dual_side_position()
    except BinanceHTTPError:
        pass
    try:
        return parse_dual_side_position_from_account(client.account())
    except BinanceHTTPError:
        return None


def position_mode_label(dual_side: bool | None) -> PositionMode | None:
    if dual_side is None:
        return None
    return "hedge" if dual_side else "one_way"


def ensure_one_way_position_mode_before_open(
    client: BinanceUsdmSignedClient,
    *,
    emit: LoggerFn | None = None,
) -> None:
    """开仓前校验：Hedge Mode 必须拒绝并交由人工切换为 One-way。"""
    log = emit or (lambda _m: None)
    dual = read_dual_side_position(client)
    mode = position_mode_label(dual)
    if mode == "hedge":
        raise HedgePositionModeError(
            "account position mode is hedge (dualSidePosition=true); "
            "switch Binance USD-M to one-way (单向持仓) before automatic opening"
        )
    if mode == "one_way":
        log("[live][position_mode] one-way (dualSidePosition=false) OK for open")
        return
    log(
        "[live][position_mode.warn] cannot read dualSidePosition — "
        "skipping hedge guard; confirm one-way mode on exchange"
    )