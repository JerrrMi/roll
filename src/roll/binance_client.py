"""Binance USD-M Futures public REST client (default Testnet).

覆盖 GET /fapi/v1：ping、time、exchangeInfo、klines、ticker/price；
支持服务器时间偏移估算与 exchangeInfo 动态解析；候选资产筛选（精确 baseAsset 匹配）。

Signed API：`BinanceCoinMSignedClient`（类名历史保留），HMAC SHA256 + timestamp + recvWindow；
不得将 API Secret 写入日志或通过 __repr__ 暴露（见 BinanceClientConfig）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlunparse, urlparse
from urllib.request import Request, urlopen

DEFAULT_TESTNET_REST_BASE = "https://testnet.binancefuture.com"
DEFAULT_LIVE_REST_BASE = "https://fapi.binance.com"
DEFAULT_API_PREFIX = "/fapi/v1"
DEFAULT_PRODUCT = "usdm"

BINANCE_ALLOWED_TESTNET_HOSTS = frozenset({"testnet.binancefuture.com"})
BINANCE_ALLOWED_USDM_LIVE_HOSTS = frozenset({"fapi.binance.com"})


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


class BinanceSignerError(RuntimeError):
    """Signed 请求参数或凭证异常。"""


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


def redact_signed_query_url(url: str) -> str:
    """去除 query 中的 `signature`，避免写入日志或通过异常暴露。"""
    p = urlparse(url)
    if not p.query:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "signature"]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))


def is_binance_futures_testnet_url(rest_base_url: str) -> bool:
    """是否指向官方 Futures Testnet host（USD-M / COIN-M 共用该 host）。

    为降低明文传输风险：仅 **https** 且 hostname 精确匹配时返回 True。
    """
    parsed = urlparse(rest_base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        return False
    return host in BINANCE_ALLOWED_TESTNET_HOSTS


def is_binance_usdm_live_url(rest_base_url: str) -> bool:
    """是否指向官方 USD-M 实盘 REST host（https://fapi.binance.com）。

    为降低明文传输风险：仅 **https** 且 hostname 精确匹配时返回 True。
    """
    parsed = urlparse(rest_base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        return False
    return host in BINANCE_ALLOWED_USDM_LIVE_HOSTS


def is_binance_coin_m_testnet_url(rest_base_url: str) -> bool:
    """兼容别名：同 `is_binance_futures_testnet_url`。"""
    return is_binance_futures_testnet_url(rest_base_url)


def is_binance_coin_m_live_url(rest_base_url: str) -> bool:
    """已废弃：COIN-M live host；3.0 请使用 `is_binance_usdm_live_url`。"""
    return is_binance_usdm_live_url(rest_base_url)


@dataclass
class BinanceClientConfig:
    product: str = DEFAULT_PRODUCT
    rest_base: str = DEFAULT_TESTNET_REST_BASE
    api_prefix: str = DEFAULT_API_PREFIX
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    recv_window_ms: int = 5000
    timeout_sec: float = 30.0


def _canonical_signed_value(value: Any) -> str:
    """Signed 请求的 query string 取值编码（与普通 REST 取值一致）。

    Binance：`true` / `false` 小写；数值避免科学计数；由调用方传入已格式化的 DECIMAL 字符串更稳妥。
    """
    if value is None:
        raise BinanceSignerError("signed 参数不能使用 None（请省略该键）")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value
    raise BinanceSignerError(f"不支持的签名参数类型: {type(value).__name__}")


def build_hmac_sha256(secret: str, payload: str) -> str:
    """HMAC SHA256（hex lowercase），与 Binance SIGNED endpoints 约定一致。"""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_query_string(params: Mapping[str, Any], *, signing_secret: str) -> str:
    """生成带 `signature` 的查询串（不包含前导 ?）。

    *payload* 为除 `signature` 外全部键的字典，`signature` 由 HMAC(SHA256) 写入末尾。
    """
    if "signature" in params:
        raise BinanceSignerError('参数中不得包含键 "signature"（应由本函数追加）')

    tuples = sorted(((str(k), _canonical_signed_value(v)) for k, v in params.items()), key=lambda x: x[0])
    base_qs = urlencode(tuples)
    sig = build_hmac_sha256(signing_secret, base_qs)
    return f"{base_qs}&signature={sig}"


def format_floor_to_step_decimal_str(raw_decimal_str: str, step_str: str) -> str:
    """将数量按 LOT/MARKET_LOT_SIZE 步长向下取整，去掉多余尾随零（COIN-M quantity 常为 DECIMAL 字符串）。"""
    try:
        q = Decimal(str(raw_decimal_str))
        step = Decimal(str(step_str))
    except InvalidOperation as e:
        raise BinanceSignerError(f"无效的 decimal / step：{raw_decimal_str!r} / {step_str!r}") from e

    if step <= 0:
        raise BinanceSignerError(f"无效的 step_size: {step_str!r}")
    n_step = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
    s = format(n_step, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def format_price_to_tick_decimal_str(
    raw_price: str | float,
    tick_str: str,
    *,
    rounding: str = "down",
) -> str:
    """将价格对齐到 PRICE_FILTER.tickSize；止损价多头通常向下floor、空头向上ceil。"""
    try:
        q = Decimal(str(raw_price))
        tick = Decimal(str(tick_str))
    except InvalidOperation as e:
        raise BinanceSignerError(f"无效的价格 / tick：{raw_price!r} / {tick_str!r}") from e
    if tick <= 0:
        raise BinanceSignerError(f"无效的 tick_str: {tick_str!r}")
    mode = rounding.strip().lower()
    rnd = ROUND_DOWN if mode in {"down", "floor"} else ROUND_UP
    n = (q / tick).to_integral_value(rounding=rnd) * tick
    s = format(n, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


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
    """USD-M Futures 公共 REST 封装（默认连 Testnet；类名历史保留）。"""

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

    def _build_rest_url(self, path: str, query: str | None = None) -> str:
        base = self._config.rest_base.rstrip("/")
        prefix = self._config.api_prefix.rstrip("/")
        full_path = f"{prefix}{path}" if prefix else path
        if not full_path.startswith("/"):
            full_path = "/" + full_path
        url = f"{base}{full_path}"
        if query:
            url = f"{url}?{query}"
        return url

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        q = urlencode([(str(k), str(v)) for k, v in (params or {}).items()])
        return self._build_rest_url(path, q if q else None)

    def _http_json_request(
        self,
        method: str,
        url: str,
        *,
        send_api_key_if_configured: bool = True,
    ) -> Any:
        req = Request(url, method=method.upper())
        if send_api_key_if_configured and self._config.api_key:
            req.add_header("X-MBX-APIKEY", str(self._config.api_key))

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
                f"HTTP {e.code} on {redact_signed_query_url(url)}: {body}",
                status_code=e.code,
                url=redact_signed_query_url(url),
                body=body,
                code=code,
                msg=msg,
            ) from e
        except URLError as e:
            safe_u = redact_signed_query_url(url)
            raise BinanceHTTPError(f"network error on {safe_u}: {e}", url=safe_u) from e

        if raw == "":
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise BinanceHTTPError(
                f"invalid JSON from {redact_signed_query_url(url)}: {raw[:500]}",
                url=redact_signed_query_url(url),
                body=raw,
            ) from e

    def _request_json(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        url = self._url(path, params)
        return self._http_json_request(method, url, send_api_key_if_configured=True)

    def ping(self) -> None:
        """GET /fapi/v1/ping"""
        self._request_json("GET", "/ping")

    def fetch_server_time_ms(self) -> int:
        """GET /fapi/v1/time — 返回 Binance serverTime（毫秒）。"""
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
        """GET /fapi/v1/exchangeInfo"""
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
        """GET /fapi/v1/klines"""
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
        """GET /fapi/v1/ticker/price

        symbol 省略时返回全部 ticker（字段映射：symbol / price / time）。
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

    def get_coin_m_spec(self, symbol: str) -> CoinMFuturesSymbol | None:
        """exchangeInfo 中按 symbol（如 DOGEUSDT）精确查找。"""
        want = symbol.strip().upper()
        for spec in self.list_coin_m_specs():
            if spec.symbol.upper() == want:
                return spec
        return None

    def min_marketable_quantity_string(self, symbol: str, *, prefer_market: bool = True) -> str:
        """返回按 LOT / MARKET_LOT_SIZE step 格式化后的最小可交易数量（字符串 DECIMAL）。"""
        spec = self.get_coin_m_spec(symbol)
        if spec is None:
            raise BinanceHTTPError(f"exchangeInfo 中找不到 symbol={symbol!r}")

        min_raw = spec.market_min_qty if prefer_market else spec.min_qty
        step_raw = spec.market_step_size if prefer_market else spec.step_size
        if not min_raw or not step_raw:
            raise BinanceHTTPError(f"symbol={symbol!r} 缺少 LOT/MARKET_LOT_SIZE 过滤条件")
        return format_floor_to_step_decimal_str(min_raw, step_raw)


class BinanceCoinMSignedClient(BinanceCoinMClient):
    """Binance USD-M Futures Signed REST（`/fapi/v1`；类名历史保留）。

    - 必选配置：`BinanceClientConfig.api_key`、`api_secret`（不得写入日志）。
    - 每个请求附带 `timestamp`（基于 `estimated_server_time_ms()`）、`recvWindow` 与 `signature`（HMAC SHA256）。
    """

    def __init__(self, config: BinanceClientConfig | None = None) -> None:
        super().__init__(config)
        ck = self._config.api_key
        cs = self._config.api_secret
        if ck is None or not str(ck).strip():
            raise ValueError("Signed 客户端需要提供非空 api_key")
        if cs is None or not str(cs).strip():
            raise ValueError("Signed 客户端需要提供非空 api_secret（请勿记录到日志）")

    def __repr__(self) -> str:
        return (
            "<BinanceCoinMSignedClient"
            f" rest_base={self._config.rest_base!r}"
            f" product={self._config.product!r}"
            f" api_prefix={self._config.api_prefix!r}"
            " credentials=(redacted)>"
        )

    def _signed_payload(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """合并签名用参数：跳过 None，`timestamp` / `recvWindow` 由内层补上。"""
        merged: dict[str, Any] = {}
        for k, v in params.items():
            if v is None:
                continue
            merged[str(k)] = v
        merged["timestamp"] = self.estimated_server_time_ms()
        merged["recvWindow"] = int(self._config.recv_window_ms)
        return merged

    def _request_json_signed(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        secret = self._config.api_secret
        if secret is None:
            raise BinanceSignerError("api_secret missing")

        merged = self._signed_payload(dict(params or {}))
        qs = build_signed_query_string(merged, signing_secret=str(secret))
        url = self._build_rest_url(path, qs)
        return self._http_json_request(method, url, send_api_key_if_configured=True)

    def account(self) -> dict[str, Any]:
        """GET /dapi/v1/account"""
        data = self._request_json_signed("GET", "/account")
        if not isinstance(data, Mapping):
            raise BinanceHTTPError(f"unexpected account payload: {type(data)}")
        return dict(data)

    def position_risk(self, *, symbol: str | None = None, pair: str | None = None) -> list[dict[str, Any]]:
        """GET /dapi/v1/positionRisk"""
        p: dict[str, Any] = {}
        if symbol is not None:
            p["symbol"] = symbol
        if pair is not None:
            p["pair"] = pair
        data = self._request_json_signed("GET", "/positionRisk", p)
        if not isinstance(data, list):
            raise BinanceHTTPError(f"unexpected positionRisk payload: {type(data)}")
        rows: list[dict[str, Any]] = []
        for row in data:
            if isinstance(row, Mapping):
                rows.append(dict(row))
            else:
                raise BinanceHTTPError(f"unexpected positionRisk row: {type(row)}")
        return rows

    def open_orders(self, *, symbol: str | None = None, pair: str | None = None) -> list[dict[str, Any]]:
        """GET /dapi/v1/openOrders"""
        p: dict[str, Any] = {}
        if symbol is not None:
            p["symbol"] = symbol
        if pair is not None:
            p["pair"] = pair
        data = self._request_json_signed("GET", "/openOrders", p)
        if not isinstance(data, list):
            raise BinanceHTTPError(f"unexpected openOrders payload: {type(data)}")
        out: list[dict[str, Any]] = []
        for row in data:
            if isinstance(row, Mapping):
                out.append(dict(row))
            else:
                raise BinanceHTTPError(f"unexpected openOrders row: {type(row)}")
        return out

    def set_leverage(self, *, symbol: str, leverage: int) -> dict[str, Any]:
        """POST /dapi/v1/leverage"""
        data = self._request_json_signed(
            "POST",
            "/leverage",
            {"symbol": symbol, "leverage": leverage},
        )
        if not isinstance(data, Mapping):
            raise BinanceHTTPError(f"unexpected leverage payload: {type(data)}")
        return dict(data)

    def new_order_raw(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """POST /dapi/v1/order — 直接使用 Binance 参数字典（均已为 str / bool / int，数量等为 DECIMAL 字符串）。"""
        data = self._request_json_signed("POST", "/order", dict(params))
        if not isinstance(data, Mapping):
            raise BinanceHTTPError(f"unexpected new order payload: {type(data)}")
        return dict(data)

    def new_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        reduce_only: bool = False,
        position_side: str | None = None,
        client_order_id: str | None = None,
        new_order_resp_type: str | None = None,
    ) -> dict[str, Any]:
        """便捷封装：COIN-M MARKET（quantity 合约张数为 DECIMAL 字符串）。"""
        p: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": reduce_only,
        }
        if position_side:
            p["positionSide"] = position_side.upper()
        if client_order_id:
            p["newClientOrderId"] = client_order_id
        if new_order_resp_type:
            p["newOrderRespType"] = new_order_resp_type
        return self.new_order_raw(p)

    def new_stop_market_close_position(
        self,
        *,
        symbol: str,
        side: str,
        stop_price: str,
        position_side: str | None = None,
        working_type: str = "MARK_PRICE",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """STOP_MARKET + closePosition：全平价保护止损（不能与 quantity / reduceOnly 同发）。"""
        q: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "closePosition": True,
            "workingType": working_type,
        }
        if position_side:
            q["positionSide"] = position_side.upper()
        if client_order_id:
            q["newClientOrderId"] = client_order_id
        return self.new_order_raw(q)

    def get_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """GET /dapi/v1/order"""
        if order_id is None and orig_client_order_id is None:
            raise ValueError("get_order 需要 order_id 或 orig_client_order_id 之一")
        p: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = int(order_id)
        if orig_client_order_id is not None:
            p["origClientOrderId"] = orig_client_order_id
        data = self._request_json_signed("GET", "/order", p)
        if not isinstance(data, Mapping):
            raise BinanceHTTPError(f"unexpected get order payload: {type(data)}")
        return dict(data)

    def cancel_order(
        self,
        *,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """DELETE /dapi/v1/order"""
        if order_id is None and orig_client_order_id is None:
            raise ValueError("cancel_order 需要 order_id 或 orig_client_order_id 之一")
        p: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            p["orderId"] = int(order_id)
        if orig_client_order_id is not None:
            p["origClientOrderId"] = orig_client_order_id
        data = self._request_json_signed("DELETE", "/order", p)
        if not isinstance(data, Mapping):
            raise BinanceHTTPError(f"unexpected cancel payload: {type(data)}")
        return dict(data)

    def new_limit_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        position_side: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """LIMIT 挂单（可用于撤单接口验收）；价格需符合 PRICE_FILTER。"""
        p: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": time_in_force,
            "quantity": quantity,
            "price": price,
            "reduceOnly": reduce_only,
        }
        if position_side:
            p["positionSide"] = position_side.upper()
        if client_order_id:
            p["newClientOrderId"] = client_order_id
        return self.new_order_raw(p)

    def close_symbol_position_market(self, *, symbol: str) -> dict[str, Any]:
        """按当前持仓用 MARKET + reduceOnly 平仓（双向 Hedge / 单向 One-way BOTH 尽量兼容）。"""
        risks = self.position_risk(symbol=symbol)
        want = symbol.upper()

        chosen: Mapping[str, Any] | None = None
        for row in risks:
            if str(row.get("symbol", "")).upper() != want:
                continue
            amt_s = row.get("positionAmt")
            if not isinstance(amt_s, str):
                continue
            try:
                amt_chk = Decimal(amt_s)
            except InvalidOperation:
                raise BinanceHTTPError(f"invalid positionAmt: {amt_s!r}") from None
            if amt_chk != 0:
                chosen = row
                break

        if chosen is None:
            raise BinanceHTTPError(f"positionRisk 中无合约 {symbol!r} 的非零持仓")

        amt_s = chosen["positionAmt"]
        spec_local = self.get_coin_m_spec(symbol)
        step_s = spec_local.market_step_size if spec_local is not None and spec_local.market_step_size else "1"

        ps_raw = chosen.get("positionSide")
        ps = str(ps_raw).upper() if isinstance(ps_raw, str) else "BOTH"

        if ps in {"LONG", "SHORT"}:
            qty_s = format_floor_to_step_decimal_str(str(abs(Decimal(str(amt_s)))), step_s)
            if ps == "LONG":
                return self.new_market_order(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty_s,
                    reduce_only=True,
                    position_side="LONG",
                )
            return self.new_market_order(
                symbol=symbol,
                side="BUY",
                quantity=qty_s,
                reduce_only=True,
                position_side="SHORT",
            )

        amt = Decimal(str(amt_s))
        if amt == 0:
            raise BinanceHTTPError("当前无持仓，无法平仓")
        qty_both = format_floor_to_step_decimal_str(str(abs(amt)), step_s)
        close_side = "SELL" if amt > 0 else "BUY"
        return self.new_market_order(
            symbol=symbol,
            side=close_side,
            quantity=qty_both,
            reduce_only=True,
        )
