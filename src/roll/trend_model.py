"""多时间框架趋势评分（15m / 1h / 4h）。

离线可对历史 OHLCV 计算，输出 long / short / no_trade，
并给出各因子加权分数与拒绝理由（不包含下单逻辑）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class SignalSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    NO_TRADE = "no_trade"


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FactorContribution:
    """单因子在当前周期上的加权贡献与可读说明。"""

    name: str
    normalized: float
    """方向性因子通常为 [-1,1]（或约在此区间）；量级类为 [0,1]。"""
    weight: float
    weighted: float
    detail_zh: str


@dataclass(frozen=True)
class TimeframeAssessment:
    interval: str
    factor_contribs: tuple[FactorContribution, ...]
    score_tf: float
    diagnostics: Mapping[str, float]
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True)
class TrendSignal:
    """趋势输出：方向、混合分、结构化分解与可读原因文本。"""

    side: SignalSide
    score: float
    score_by_interval: Mapping[str, float]
    timeframe_assessments: tuple[TimeframeAssessment, ...]
    reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]


@dataclass
class TrendModelParams:
    """模型参数默认值（与 plan 对齐，可调参回测）。

    blended_score = 0.50*score_4h + 0.35*score_1h + 0.15*score_15m
    单周期分项（方向一致时各子分亦取与趋势同号）详见 evaluate 实现说明。
    """

    intervals_required: tuple[str, ...] = ("15m", "1h", "4h")
    weight_15m: float = 0.15
    weight_1h: float = 0.35
    weight_4h: float = 0.50

    # 单周期分项权重（在归一化子分数之上）
    w_slope: float = 0.25
    w_adx_bundle: float = 0.20
    w_ema: float = 0.20
    w_breakout: float = 0.15
    w_volume: float = 0.12
    w_chop_bundle: float = 0.08

    long_threshold: float = 0.70
    short_threshold: float = 0.70

    regression_bars: int = 48
    min_adx: float = 25.0
    min_regression_r2: float = 0.35
    # Choppiness Index：高于该阈值视为宽幅震荡（经典上沿约 61.8）；plan 写 max_chop=55 时可改严
    chop_reject_above: float = 61.8
    chop_period: int = 14
    adx_period: int = 14
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 100
    donchian_period: int = 20
    volume_ma_period: int = 20
    volume_confirm_mult: float = 1.2
    slope_norm_divisor: float = 2.0
    """slope_score 近似 beta/std(log-return)，再除以该值后用 tanh 压到 [-1,1]。"""
    max_ema_crosses: int = 10
    """近 regression_bars 根内允许的 EMA(中周期)穿越次数上限，过高视为震荡。"""
    min_bars_soft: int = 120
    """软下限：过少 K 线仍尝试计算；若不足 regression_bars + 5 则该周期作废。"""

    def blended_weights_ok(self) -> bool:
        s = self.weight_15m + self.weight_1h + self.weight_4h
        return abs(s - 1.0) < 1e-6


def parse_binance_klines(rows: Sequence[Sequence[Any]]) -> list[Candle]:
    """将 GET /dapi/v1/klines 单行数组转为 Candle。"""
    out: list[Candle] = []
    for r in rows:
        if len(r) < 6:
            continue
        out.append(
            Candle(
                open_time_ms=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            )
        )
    return out


# --- indicators ---


def _sma(vals: Sequence[float], period: int) -> list[float]:
    out = [math.nan] * len(vals)
    if period <= 0 or len(vals) < period:
        return out
    win = vals[:period]
    s = sum(win)
    out[period - 1] = s / period
    for i in range(period, len(vals)):
        s += vals[i] - vals[i - period]
        out[i] = s / period
    return out


def _ema_series(closes: Sequence[float], period: int) -> list[float]:
    n = len(closes)
    out = [math.nan] * n
    if period <= 1 or n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    sma_p = sum(closes[:period]) / period
    out[period - 1] = sma_p
    prev = sma_p
    for i in range(period, n):
        prev = alpha * closes[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr: list[float] = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return tr


def _adx_di_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> tuple[list[float], list[float], list[float]]:
    """返回 (ADX, plus_DI, minus_DI)，前若干值为 nan。"""
    n = len(closes)
    adx = [math.nan] * n
    pdi = [math.nan] * n
    mdi = [math.nan] * n
    if n < period + 2:
        return adx, pdi, mdi

    tr_s = _true_ranges(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move

    atr = math.nan
    sp_dm = math.nan
    sm_dm = math.nan

    atr = sum(tr_s[1 : period + 1])
    sp_dm = sum(plus_dm[1 : period + 1])
    sm_dm = sum(minus_dm[1 : period + 1])

    def _clamp_di(num: float, den: float) -> float:
        if den <= 1e-12:
            return 0.0
        return 100.0 * (num / den)

    prev_atr = atr
    prev_sp = sp_dm
    prev_sm = sm_dm

    dx_list: list[float] = []
    for i in range(period + 1, n):
        prev_atr = prev_atr - (prev_atr / period) + tr_s[i]
        prev_sp = prev_sp - (prev_sp / period) + plus_dm[i]
        prev_sm = prev_sm - (prev_sm / period) + minus_dm[i]

        pdi_here = _clamp_di(prev_sp, prev_atr)
        mdi_here = _clamp_di(prev_sm, prev_atr)
        pdi[i] = pdi_here
        mdi[i] = mdi_here

        denom = pdi_here + mdi_here + 1e-12
        dx = 100.0 * abs(pdi_here - mdi_here) / denom
        dx_list.append(dx)

        if len(dx_list) >= period:
            recent = dx_list[-period:]
            adx_val = sum(recent) / period
            adx[i] = adx_val

    return adx, pdi, mdi


def _choppiness_index(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> list[float]:
    tr = _true_ranges(highs, lows, closes)
    n = len(closes)
    out = [math.nan] * n
    if period < 2 or n <= period:
        return out

    logn = math.log10(float(period))
    for i in range(period, n):
        slice_tr = tr[i - period + 1 : i + 1]
        slice_h = highs[i - period + 1 : i + 1]
        slice_l = lows[i - period + 1 : i + 1]
        sum_tr = sum(slice_tr)
        hh = max(slice_h)
        ll = min(slice_l)
        denom = hh - ll
        if denom <= 1e-12:
            out[i] = 100.0
            continue
        ratio = sum_tr / denom
        ratio = max(ratio, 1e-12)
        ci = 100.0 * math.log10(ratio) / logn
        out[i] = max(0.0, min(100.0, ci))
    return out


def _log_close_regression(
    closes_tail: Sequence[float],
) -> tuple[float, float, float, float, float]:
    """对 ln(close) ~ 时间下标回归。返回 slope, intercept, r2, t_slope, stderr_slope。"""
    y_raw = [max(c, 1e-12) for c in closes_tail]
    y = [math.log(c) for c in y_raw]
    n = len(y)
    if n < 5:
        return 0.0, 0.0, 0.0, 0.0, math.nan

    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(y) / n
    ss_x = sum((xi - mx) ** 2 for xi in xs)
    ss_xy = sum((xi - mx) * (yi - my) for xi, yi in zip(xs, y, strict=False))
    if ss_x <= 1e-18:
        return 0.0, my, 0.0, 0.0, math.nan

    slope = ss_xy / ss_x
    intercept = my - slope * mx
    sse = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(xs, y, strict=False))
    sst = sum((yi - my) ** 2 for yi in y)
    r2 = 1.0 - (sse / sst) if sst > 1e-18 else 0.0

    dof = max(n - 2, 1)
    sigma2_hat = sse / dof
    se_slope = math.sqrt(sigma2_hat / ss_x) if ss_x > 1e-18 else math.nan
    t_slope = slope / se_slope if se_slope and se_slope > 1e-18 and math.isfinite(se_slope) else math.nan

    return slope, intercept, max(0.0, min(1.0, r2)), t_slope, se_slope


def _std(vals: Sequence[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    v = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(max(v, 0.0))


def _donchian_score(close_f: float, highs: Sequence[float], lows: Sequence[float], period: int) -> float:
    """[-1,1]：接近上轨偏多，接近下轨偏空。"""
    if period <= 1 or len(highs) < period or len(lows) < period:
        return 0.0
    hi = max(highs[-period:])
    lo = lows[-period:]
    lo_min = min(lo)
    rng = hi - lo_min
    if rng <= 1e-12:
        return 0.0
    pos = (close_f - lo_min) / rng
    return max(-1.0, min(1.0, 2.0 * pos - 1.0))


def _ema_alignment_score(close_f: float, ef: float, em: float, es: float) -> tuple[float, str]:
    """基于 EMA 排列与现价相对中长 EMA 的位置，输出 [-1,1]。"""
    if (
        math.isnan(ef)
        or math.isnan(em)
        or math.isnan(es)
    ):
        return 0.0, "ema_unavailable"

    bullish_stack = ef > em > es
    bearish_stack = ef < em < es
    tol = abs(es) * 1e-6 + 1e-12
    pad = tol * 10
    hi = max(ef, em, es)
    lo_v = min(ef, em, es)
    if bullish_stack:
        pos = _clamp_signed((close_f - em) / (hi - lo_v + pad), -1.0, 1.0)
        return max(0.2, float(pos)), "ema_bull_stack"
    if bearish_stack:
        pos = _clamp_signed((em - close_f) / (hi - lo_v + pad), -1.0, 1.0)
        return float(-max(0.2, abs(pos))), "ema_bear_stack"
    if close_f >= hi:
        return 0.35, "price_above_all_emas"
    if close_f <= lo_v:
        return -0.35, "price_below_all_emas"
    return 0.0, "ema_mixed"


def _clamp_signed(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _tanh_norm(x: float, div: float) -> float:
    if div <= 0:
        return math.tanh(x)
    return math.tanh(x / div)


def _count_ema_crosses(closes_tail: Sequence[float], em_mid_tail: Sequence[float]) -> int:
    """统计相对 EMA 中轴的符号变化次数（简易震荡近似）。"""
    if len(closes_tail) != len(em_mid_tail) or len(closes_tail) < 3:
        return 0
    signs: list[int] = []
    for c, e in zip(closes_tail, em_mid_tail, strict=False):
        if math.isnan(e):
            continue
        d = c - e
        if abs(d) < 1e-18:
            signs.append(0)
        elif d > 0:
            signs.append(1)
        else:
            signs.append(-1)
    last: int | None = None
    crosses = 0
    for s in signs:
        if s == 0:
            continue
        if last is None:
            last = s
            continue
        if s != last:
            crosses += 1
            last = s
    return crosses


def _adx_direction_score(adx_val: float, pdi_val: float, mdi_val: float, min_adx: float) -> tuple[float, str]:
    strength = max(0.0, min(1.0, (adx_val - min_adx) / 35.0)) if adx_val == adx_val else 0.0
    denom = abs(pdi_val) + abs(mdi_val) + 1e-12
    di_sign = _clamp_signed((pdi_val - mdi_val) / denom, -1.0, 1.0)
    mag = math.sqrt(abs(di_sign) * strength) * (1.0 if di_sign >= 0 else -1.0 if di_sign < 0 else 0)
    narrative = (
        f"ADX={adx_val:.2f}(min {min_adx:.1f}); +DI={pdi_val:.2f}, -DI={mdi_val:.2f}"
        if math.isfinite(adx_val)
        else "ADX=nan"
    )
    return _clamp_signed(mag, -1.0, 1.0), narrative


class TrendModel:
    """多周期趋势模型：离线喂入各周期 Candle 序列即可。"""

    def __init__(self, params: TrendModelParams | None = None) -> None:
        self.params = params or TrendModelParams()
        if not self.params.blended_weights_ok():
            raise ValueError(
                "weight_15m + weight_1h + weight_4h 必须之和为 1.0 "
                f"got {self.params.weight_15m}+{self.params.weight_1h}+{self.params.weight_4h}"
            )

    def evaluate(self, candles_by_interval: Mapping[str, Sequence[Candle]]) -> TrendSignal:
        assessments: list[TimeframeAssessment] = []
        per_scores: dict[str, float] = {}

        intervals = self.params.intervals_required
        for iv in intervals:
            candles = candles_by_interval.get(iv, ())
            a = self._assess_single(iv, candles)
            assessments.append(a)
            per_scores[iv] = a.score_tf

        rej_global: list[str] = []

        blended = (
            self.params.weight_15m * per_scores.get("15m", 0.0)
            + self.params.weight_1h * per_scores.get("1h", 0.0)
            + self.params.weight_4h * per_scores.get("4h", 0.0)
        )

        def _rejections_for(iv: str) -> tuple[str, ...]:
            for ass in assessments:
                if ass.interval == iv:
                    return ass.rejection_reasons
            return ()

        g4 = _rejections_for("4h")
        g1 = _rejections_for("1h")

        s4 = per_scores.get("4h", 0.0)
        s1 = per_scores.get("1h", 0.0)
        gates_ok = True
        if g4:
            rej_global.append(f"[4h] " + "; ".join(g4))
            gates_ok = False
        if g1:
            rej_global.append(f"[1h] " + "; ".join(g1))
            gates_ok = False

        reasons: list[str] = []

        reasons.append(
            "混合评分 trend_score="
            f"{blended:.4f}= {self.params.weight_4h}*score_4h({per_scores.get('4h', float('nan')):.4f})"
            f"+ {self.params.weight_1h}*score_1h({per_scores.get('1h', float('nan')):.4f})"
            f"+ {self.params.weight_15m}*score_15m({per_scores.get('15m', float('nan')):.4f})"
        )

        def _factor_lines(iv: str) -> list[str]:
            for ass in assessments:
                if ass.interval == iv:
                    return [
                        f"    - {fc.name}: norm={fc.normalized:+.4f} * w={fc.weight:.4f}"
                        f" => {fc.weighted:+.4f} | {fc.detail_zh}"
                        for fc in ass.factor_contribs
                    ]
            return []

        for iv in intervals:
            reasons.append(f"[{iv}] score_tf={per_scores.get(iv, float('nan')):.4f}")
            diag = {}
            for a in assessments:
                if a.interval == iv:
                    diag = dict(a.diagnostics)
            if diag:
                sk = ",".join(
                    f"{k}={v:.4f}"
                    for k, v in sorted(diag.items())
                    if isinstance(v, float) and math.isfinite(v)
                )
                reasons.append(f"    诊断摘要: {sk}")
            reasons.extend(_factor_lines(iv))
            rf = []
            for a in assessments:
                if a.interval == iv:
                    rf = list(a.rejection_reasons)
            if rf:
                reasons.append(f"    拒绝因子: {'; '.join(rf)}")
            else:
                reasons.append("    拒绝因子: 无（该周期硬性门槛已通过）")

        side = SignalSide.NO_TRADE

        long_ok = (
            gates_ok
            and blended >= self.params.long_threshold
            and s4 > 0
            and s1 > 0
            and (s4 > 0.05 or s1 > 0.05)
        )
        short_ok = (
            gates_ok
            and blended <= -self.params.short_threshold
            and s4 < 0
            and s1 < 0
            and (s4 < -0.05 or s1 < -0.05)
        )

        def _reject(msg: str) -> None:
            if msg not in rej_global:
                rej_global.append(msg)

        if not gates_ok:
            _reject("4h或1h硬性门槛失败（ADX/R²/Chop/震荡穿越/量价等）")

        elif long_ok and short_ok:
            side = SignalSide.NO_TRADE
            _reject("多空方向同时为真（异常混合信号）")

        elif long_ok:
            side = SignalSide.LONG

        elif short_ok:
            side = SignalSide.SHORT

        else:
            if gates_ok:
                parts = [
                    f"混合分={blended:.4f} ∈ (-{self.params.short_threshold}, {self.params.long_threshold})"
                ]
                if not (s4 > 0 and s1 > 0):
                    parts.append(f"非多头并排 score_4h={s4:+.4f}, score_1h={s1:+.4f}")
                if not (s4 < 0 and s1 < 0):
                    parts.append(f"非空头并排 score_4h={s4:+.4f}, score_1h={s1:+.4f}")
                _reject("；".join(parts))

        rej_global_final = () if side != SignalSide.NO_TRADE else tuple(rej_global)

        summary_lines = [
            *reasons,
            f"最终结果: {side.value}；mixed_score={blended:.4f}",
        ]
        if side == SignalSide.NO_TRADE and rej_global_final:
            summary_lines.append("全局拒绝: " + " | ".join(rej_global_final))

        return TrendSignal(
            side=side,
            score=float(blended),
            score_by_interval=dict(per_scores),
            timeframe_assessments=tuple(assessments),
            reasons=tuple(summary_lines),
            rejection_reasons=rej_global_final,
        )

    def _assess_single(self, interval: str, candles_seq: Sequence[Candle]) -> TimeframeAssessment:
        p = self.params
        if len(candles_seq) < p.regression_bars + 5:
            return TimeframeAssessment(
                interval=interval,
                factor_contribs=(),
                score_tf=0.0,
                diagnostics={},
                rejection_reasons=("warmup_insufficient bars < regression_bars+5",),
            )

        cs = sorted(candles_seq, key=lambda c: c.open_time_ms)
        closes = [c.close for c in cs]
        highs = [c.high for c in cs]
        lows = [c.low for c in cs]
        vols = [c.volume for c in cs]

        ema_f = _ema_series(closes, p.ema_fast)
        ema_m = _ema_series(closes, p.ema_mid)
        ema_s = _ema_series(closes, p.ema_slow)
        adx, pdi, mdi = _adx_di_series(highs, lows, closes, p.adx_period)
        chop = _choppiness_index(highs, lows, closes, p.chop_period)

        i = len(closes) - 1

        rej: list[str] = []

        tail_n = min(p.regression_bars, len(closes))
        tail_cls = closes[-tail_n:]
        slope, _intercept, r2, t_slope, _se_slope = _log_close_regression(tail_cls)
        log_returns = [math.log(tail_cls[j] / tail_cls[j - 1]) for j in range(1, len(tail_cls))]
        denom_std = max(_std(log_returns), 1e-12)
        slope_mag = slope / denom_std
        slope_score = float(_tanh_norm(slope_mag, p.slope_norm_divisor))

        t_start = len(closes) - tail_n
        crosses = _count_ema_crosses(closes[t_start:], ema_m[t_start:])
        chop_val = chop[i]

        adx_here = adx[i] if math.isfinite(adx[i]) else 0.0
        pdi_here = pdi[i] if math.isfinite(pdi[i]) else 0.0
        mdi_here = mdi[i] if math.isfinite(mdi[i]) else 0.0

        if adx_here < p.min_adx:
            rej.append(f"ADX不足 adx={adx_here:.2f} < min_adx={p.min_adx:.1f}")
        if r2 < p.min_regression_r2:
            rej.append(f"对数价位回归不稳定 R²={r2:.4f} < min_r2={p.min_regression_r2:.2f}")
        if chop_val == chop_val and chop_val >= p.chop_reject_above:
            rej.append(f"Choppiness震荡过高 CI={chop_val:.2f} >= threshold={p.chop_reject_above:.2f}")

        sma_v = _sma(vols, p.volume_ma_period)
        sma_v_val = sma_v[i] if i < len(sma_v) else float("nan")
        if math.isfinite(sma_v_val):
            vma = float(sma_v_val)
        else:
            sta = max(0, i + 1 - p.volume_ma_period)
            vma = sum(vols[sta : i + 1]) / max(1, (i + 1 - sta))

        vol_ratio = vols[i] / max(vma, 1e-18)

        ef = ema_f[i]
        em_here = ema_m[i]
        es = ema_s[i]
        fc_f = closes[i]

        ema_alignment, ema_tag = _ema_alignment_score(fc_f, ef, em_here, es)

        pb = max(0, i - p.donchian_period)
        prior_high = highs[pb:i]
        prior_low = lows[pb:i]
        breakout = (
            _donchian_score(fc_f, prior_high, prior_low, p.donchian_period)
            if len(prior_high) >= max(2, p.donchian_period)
            else 0.0
        )

        vm = float(p.volume_confirm_mult)
        vs = (
            float(_clamp_signed((vol_ratio - vm) / max(vm * 0.75, 0.05), -1.0, 1.0))
            if math.isfinite(vol_ratio)
            else 0.0
        )

        chop_pen_component = chop_val / 100.0 if chop_val == chop_val else 0.0

        if vol_ratio + 1e-9 < vm:
            rej.append(f"成交量确认失败 vol_ratio={vol_ratio:.3f} < mult={vm:.2f}*VMA")

        if crosses > p.max_ema_crosses:
            rej.append(
                "均价震荡穿透过多 "
                f"ema_mid_crosses_window={crosses} > max={p.max_ema_crosses}"
            )

        ts_txt = f"{t_slope:.2f}" if math.isfinite(t_slope) else "nan"
        slope_detail = (
            f"β/σ(logΔp)≈{slope_mag:.4f} tanh@{p.slope_norm_divisor}->{slope_score:+.3f}; "
            f"t_slope={ts_txt}; R²={r2:.3f}"
        )

        adx_score, adx_narrative = _adx_direction_score(adx_here, pdi_here, mdi_here, p.min_adx)

        wtotal = (
            p.w_slope + p.w_adx_bundle + p.w_ema + p.w_breakout + p.w_volume + p.w_chop_bundle
        )
        if abs(wtotal - 1.0) > 1e-6:
            raise ValueError(
                "TrendModelParams 内部权重之和须为 1.0 "
                f"（当前 {wtotal:.6f}）"
            )

        score_tf_before_clip = (
            p.w_slope * slope_score
            + p.w_adx_bundle * adx_score
            + p.w_ema * ema_alignment
            + p.w_breakout * breakout
            + p.w_volume * vs
            - p.w_chop_bundle * chop_pen_component
        )
        score_tf = float(_clamp_signed(score_tf_before_clip, -1.0, 1.0))

        factors = (
            FactorContribution(
                name="log_price_slope",
                normalized=float(slope_score),
                weight=p.w_slope,
                weighted=float(p.w_slope * slope_score),
                detail_zh=slope_detail,
            ),
            FactorContribution(
                name="adx_dmi",
                normalized=float(adx_score),
                weight=p.w_adx_bundle,
                weighted=float(p.w_adx_bundle * adx_score),
                detail_zh=adx_narrative,
            ),
            FactorContribution(
                name="ema_stack",
                normalized=float(ema_alignment),
                weight=p.w_ema,
                weighted=float(p.w_ema * ema_alignment),
                detail_zh=f"EMA({p.ema_fast}/{p.ema_mid}/{p.ema_slow}) {ema_tag}",
            ),
            FactorContribution(
                name="donchian_break",
                normalized=float(breakout),
                weight=p.w_breakout,
                weighted=float(p.w_breakout * breakout),
                detail_zh=(
                    "当前收盘在近 Donchian 区间（不包含当根 highs/lows）"
                    f" pb..i={pb}..{i} maps -> norm break"
                ),
            ),
            FactorContribution(
                name="volume_confirm",
                normalized=float(vs),
                weight=p.w_volume,
                weighted=float(p.w_volume * vs),
                detail_zh=f"vol/VMA{p.volume_ma_period}={vol_ratio:.3f} （硬性 ≥{vm}）",
            ),
            FactorContribution(
                name="chop_penalty",
                normalized=float(-chop_pen_component),
                weight=p.w_chop_bundle,
                weighted=float(-p.w_chop_bundle * chop_pen_component),
                detail_zh=f"震荡惩罚 Choppiness={chop_val:.2f}; EMA 穿越={crosses} 次/窗",
            ),
        )

        diagnostics: dict[str, float] = {
            "slope_raw": slope,
            "beta_over_std_lr": slope_mag,
            "r2": r2,
            "t_slope": t_slope if math.isfinite(t_slope) else float("nan"),
            "adx": adx_here,
            "plus_di": pdi_here,
            "minus_di": mdi_here,
            "chop_index": chop_val if chop_val == chop_val else float("nan"),
            "vol_ratio": vol_ratio,
            "ema_crosses": float(crosses),
            "score_tf_unclipped": score_tf_before_clip,
        }

        return TimeframeAssessment(
            interval=interval,
            factor_contribs=factors,
            score_tf=score_tf,
            diagnostics={k: v for k, v in diagnostics.items() if isinstance(v, float)},
            rejection_reasons=tuple(rej),
        )

