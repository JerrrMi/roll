"""USD-M 账户模式：逐仓/全仓 marginType 校验与可选设置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from roll.binance_client import BinanceCoinMSignedClient, BinanceHTTPError

MarginType = Literal["ISOLATED", "CROSSED"]
LoggerFn = Callable[[str], None]


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
    client: BinanceCoinMSignedClient,
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
    client: BinanceCoinMSignedClient,
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
