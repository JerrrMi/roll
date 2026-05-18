"""Binance COIN-M Futures public REST client (default Testnet).

覆盖 GET /dapi/v1：ping、time、exchangeInfo、klines、ticker/price；
支持服务器时间偏移估算与 exchangeInfo 动态解析；候选资产筛选（精确 baseAsset 匹配）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_TESTNET_REST_BASE = "https://testnet.binancefuture.com"
DEFAULT_LIVE_REST_BASE = "https://dapi.binance.com"
DEFAULT_COIN_M_PREFIX = "/dapi/v1"


class BinanceHTTPError(RuntimeError):
    """HTTP 失败或 Binance 返回的业务错误载荷。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        body: str | None = None,
        code: int | None = None,
        msg: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body
        self.code = code
        self.msg = msg


class InsufficientMonitorableSymbolsError(RuntimeError):
    """可监测标的少于配置的最小数量。"""

    def __init__(self, message: str, *, report: SymbolAvailabilityReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class CoinMFuturesSymbol:
    """从 exchangeInfo.symbols[] 解析出的 COIN-M 合约视图（永续/交割共用结构）。"""

    symbol: str
    pair: str
    base_asset: str
    quote_asset: str
    contract_status: str
    contract_type: str
    margin_asset: str
    price_precision: int
    quantity_precision: int
    contract_size: float
    tick_size: str
    step_size: str
    min_qty: str
    market_min_qty: str
    market_step_size: str
    filters_raw: tuple[Mapping[str, Any], ...] = field(repr=False)


@dataclass(frozen=True)
class CandidateSymbolRow:
    candidate: str
    matched: CoinMFuturesSymbol | None
    reason: str | None


@dataclass(frozen=True)
class SymbolAvailabilityReport:
    candidates: tuple[str, ...]
    rows: tuple[CandidateSymbolRow, ...]
    matched: tuple[CoinMFuturesSymbol, ...]

    def format_human_readable(self) -> str:
        lines: list[str] = []
        for row in self.rows:
            if row.matched is not None:
                s = row.matched
                lines.append(
                    f"  {row.candidate}: OK -> {s.symbol} "
                    f"({s.contract_type}, {s.contract_status})"
                )
            else:
                lines.append(f"  {row.candidate}: unavailable - {row.reason}")
        lines.append("")
        lines.append(
            f"Matched {len(self.matched)} / {len(self.candidates)} candidates "
            f"(order preserved)."
        )
        return "\n".join(lines)


@dataclass
class BinanceClientConfig:
    rest_base: str = DEFAULT_TESTNET_REST_BASE
    coin_m_prefix: str = DEFAULT_COIN_M_PREFIX
    api_key: str | None = None
    api_secret: str | None = None
    recv_window_ms: int = 5000
    timeout_sec: float = 30.0


def _millis_now() -> int:
    return int(time.time() * 1000)


def _filter_dict(filters: list[Mapping[str, Any]], filter_type: str) -> Mapping[str, Any] | None:
    for f in filters:
        if f.get("filterType") == filter_type:
            return f
    return None


def parse_coin_m_specs_from_exchange_info(exchange_info: Mapping[str, Any]) -> list[CoinMFuturesSymbol]:
    """解析 GET /dapi/v1/exchangeInfo 响应中的 symbols 列表。"""
    symbols_raw = exchange_info.get("symbols")
    if not isinstance(symbols_raw, list):
        return []
    out: list[CoinMFuturesSymbol] = []
    for row in symbols_raw:
        if not isinstance(row, Mapping):
            continue
        filters_list = row.get("filters")
        filters: list[Mapping[str, Any]] = filters_list if isinstance(filters_list, list) else []

        pf = _filter_dict(filters, "PRICE_FILTER") or {}
        lot = _filter_dict(filters, "LOT_SIZE") or {}
        mlot = _filter_dict(filters, "MARKET_LOT_SIZE") or {}

        contract_size_raw = row.get("contractSize", 0)
        try:
            contract_size_f = float(contract_size_raw)
        except (TypeError, ValueError):
            contract_size_f = 0.0

        pp = row.get("pricePrecision")
        qp = row.get("quantityPrecision")
        try:
            price_prec = int(pp) if pp is not None else 0
        except (TypeError, ValueError):
            price_prec = 0
        try:
            qty_prec = int(qp) if qp is not None else 0
        except (TypeError, ValueError):
            qty_prec = 0

        out.append(
            CoinMFuturesSymbol(
                symbol=str(row.get("symbol", "")),
                pair=str(row.get("pair", "")),
                base_asset=str(row.get("baseAsset", "")),
                quote_asset=str(row.get("quoteAsset", "")),
                contract_status=str(row.get("contractStatus", "")),
                contract_type=str(row.get("contractType", "")),
                margin_asset=str(row.get("marginAsset", "")),
                price_precision=price_prec,
                quantity_precision=qty_prec,
                contract_size=contract_size_f,
                tick_size=str(pf.get("tickSize", "")),
                step_size=str(lot.get("stepSize", "")),
                min_qty=str(lot.get("minQty", "")),
                market_min_qty=str(mlot.get("minQty", "")),
                market_step_size=str(mlot.get("stepSize", "")),
                filters_raw=tuple(filters),
            )
        )
    return out


def select_monitorable_coin_m_symbols(
    specs: Sequence[CoinMFuturesSymbol],
    candidates: Sequence[str],
    *,
    min_count: int = 3,
    allowed_contract_types: frozenset[str] | None = None,
    required_contract_status: str = "TRADING",
) -> tuple[list[CoinMFuturesSymbol], SymbolAvailabilityReport]:
    """按候选资产（精确匹配 baseAsset）筛选可监测合约。

    不按名称猜测（例如 SHIB vs 1000SHIB）；交易所需用 exchange 返回的 baseAsset 作为配置项。
    """
    if allowed_contract_types is None:
        allowed_contract_types = frozenset({"PERPETUAL"})

    by_base: dict[str, CoinMFuturesSymbol] = {}
    for s in specs:
        if not s.base_asset:
            continue
        if s.base_asset not in by_base:
            by_base[s.base_asset] = s

    norm_candidates = tuple(str(c).strip().upper() for c in candidates if str(c).strip())
    rows: list[CandidateSymbolRow] = []
    matched: list[CoinMFuturesSymbol] = []

    for cand in norm_candidates:
        spec = by_base.get(cand)
        if spec is None:
            rows.append(
                CandidateSymbolRow(
                    candidate=cand,
                    matched=None,
                    reason=(
                        "no symbol with this baseAsset on exchangeInfo "
                        "(if the venue lists 1000SHIB etc., put that exact code in candidates)"
                    ),
                )
            )
            continue
        reasons: list[str] = []
        if spec.contract_status != required_contract_status:
            reasons.append(
                f"contractStatus={spec.contract_status!r} (need {required_contract_status!r})"
            )
        if spec.contract_type not in allowed_contract_types:
            reasons.append(
                f"contractType={spec.contract_type!r} "
                f"(allowed: {sorted(allowed_contract_types)})"
            )
        if reasons:
            rows.append(
                CandidateSymbolRow(
                    candidate=cand,
                    matched=None,
                    reason="; ".join(reasons),
                )
            )
            continue
        rows.append(CandidateSymbolRow(candidate=cand, matched=spec, reason=None))
        matched.append(spec)

    report = SymbolAvailabilityReport(
        candidates=norm_candidates,
        rows=tuple(rows),
        matched=tuple(matched),
    )
    if len(matched) < min_count:
        msg = (
            f"Need at least {min_count} monitorable COIN-M symbols for candidates "
            f"{list(norm_candidates)}, got {len(matched)}.\n"
            f"{report.format_human_readable()}"
        )
        raise InsufficientMonitorableSymbolsError(msg, report=report)
    return matched, report


class BinanceCoinMClient:
    """COIN-M Futures 公共 REST 封装（默认连 Testnet）。"""

    def __init__(self, config: BinanceClientConfig | None = None) -> None:
        self._config = config or BinanceClientConfig()
        self._server_offset_ms: int = 0

    @property
    def config(self) -> BinanceClientConfig:
        return self._config

    @property
    def server_offset_ms(self) -> int:
        """最近一次 sync_server_time() 估算的偏移：server_time ≈ local_ms + offset。"""
        return self._server_offset_ms

    def estimated_server_time_ms(self) -> int:
        """基于本地时钟与已缓存偏移估算当前服务器毫秒时间。"""
        return _millis_now() + self._server_offset_ms

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        base = self._config.rest_base.rstrip("/")
        prefix = self._config.coin_m_prefix.rstrip("/")
        full_path = f"{prefix}{path}" if prefix else path
        if not full_path.startswith("/"):
            full_path = "/" + full_path
        url = f"{base}{full_path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    def _request_json(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        url = self._url(path, params)
        req = Request(url, method=method.upper())
        # 公共接口可不带头；预留 api_key 便于后续扩展（当前不使用签名）
        if self._config.api_key:
            req.add_header("X-MBX-APIKEY", self._config.api_key)
        try:
            with urlopen(req, timeout=self._config.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            code = msg = None
            try:
                payload = json.loads(body)
                if isinstance(payload, Mapping):
                    c = payload.get("code")
                    m = payload.get("msg")
                    if isinstance(c, int):
                        code = c
                    if isinstance(m, str):
                        msg = m
            except json.JSONDecodeError:
                pass
            raise BinanceHTTPError(
                f"HTTP {e.code} on {url}: {body}",
                status_code=e.code,
                url=url,
                body=body,
                code=code,
                msg=msg,
            ) from e
        except URLError as e:
            raise BinanceHTTPError(f"network error on {url}: {e}", url=url) from e

        if raw == "":
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise BinanceHTTPError(
                f"invalid JSON from {url}: {raw[:500]}",
                url=url,
                body=raw,
            ) from e

    def ping(self) -> None:
        """GET /dapi/v1/ping"""
        self._request_json("GET", "/ping")

    def fetch_server_time_ms(self) -> int:
        """GET /dapi/v1/time — 返回 Binance serverTime（毫秒）。"""
        data = self._request_json("GET", "/time")
        if not isinstance(data, Mapping):
            raise BinanceHTTPError(f"unexpected /time payload: {data!r}")
        st = data.get("serverTime")
        if not isinstance(st, int):
            raise BinanceHTTPError(f"unexpected serverTime: {st!r}")
        return st

    def sync_server_time(self) -> int:
        """估算往返延迟中的点时间偏移并缓存。

        offset_ms 满足 serverTime ≈ local_midpoint_ms + offset_ms。
        """
        t0 = _millis_now()
        server_ms = self.fetch_server_time_ms()
        t1 = _millis_now()
        midpoint = (t0 + t1) // 2
        self._server_offset_ms = server_ms - midpoint
        return self._server_offset_ms

    def exchange_info(self) -> dict[str, Any]:
        """GET /dapi/v1/exchangeInfo"""
        data = self._request_json("GET", "/exchangeInfo")
        if not isinstance(data, Mapping):
            raise BinanceHTTPError(f"unexpected exchangeInfo payload: {type(data)}")
        return dict(data)

    def list_coin_m_specs(self) -> list[CoinMFuturesSymbol]:
        """exchangeInfo + 字段解析。"""
        return parse_coin_m_specs_from_exchange_info(self.exchange_info())

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        """GET /dapi/v1/klines"""
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        data = self._request_json("GET", "/klines", params)
        if not isinstance(data, list):
            raise BinanceHTTPError(f"unexpected klines payload: {type(data)}")
        return data

    def ticker_price(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET /dapi/v1/ticker/price

        symbol 省略时返回全部 ticker（字段映射：symbol / pair(ps) / price / time）。
        """
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = self._request_json("GET", "/ticker/price", params if params else None)
        if isinstance(data, Mapping):
            return [dict(data)]
        if isinstance(data, list):
            out: list[dict[str, Any]] = []
            for row in data:
                if isinstance(row, Mapping):
                    out.append(dict(row))
            return out
        raise BinanceHTTPError(f"unexpected ticker/price payload: {type(data)}")
