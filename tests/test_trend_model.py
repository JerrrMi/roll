"""趋势模型：结构、离线合成序列与线性回归一致性。"""

from __future__ import annotations

import math

from roll.trend_model import Candle, FactorContribution, SignalSide, TrendModel, TrendModelParams
from roll.trend_model import _log_close_regression


def _synth_uptrend_candles(*, bars: int, step: float = 0.008, base_ms: int = 1_700_000_000_000) -> list[Candle]:
    candles: list[Candle] = []
    price = 50.0
    vbase = 10.0
    for i in range(bars):
        growth = math.exp(step) if i > 0 else 1.0
        o = price
        price *= growth
        c = price
        hi = max(o, c) * 1.002
        lo = min(o, c) * 0.998
        vol = vbase + float(i % 13)
        candles.append(Candle(open_time_ms=base_ms + i * 60_000, open=o, high=hi, low=lo, close=c, volume=vol))
    # 放大最后一根量价以通过成交量硬性门槛（真实回测中用真实成交额）
    last = candles[-1]
    candles[-1] = Candle(
        open_time_ms=last.open_time_ms,
        open=last.open,
        high=last.high,
        low=last.low,
        close=last.close,
        volume=last.volume * 60.0,
    )
    return candles


def _repeat_tf(data: list[Candle]) -> tuple[list[Candle], list[Candle], list[Candle]]:
    return data, data, data


def test_trend_requires_three_frames() -> None:
    relaxed = TrendModelParams(
        long_threshold=0.001,
        min_adx=0.0,
        min_regression_r2=0.0,
        chop_reject_above=101.0,
        volume_confirm_mult=0.01,
        max_ema_crosses=999,
        short_threshold=0.99,
        weight_15m=0.15,
        weight_1h=0.35,
        weight_4h=0.50,
    )
    candles = _synth_uptrend_candles(bars=400)
    d15, h1, h4 = _repeat_tf(candles)
    m = TrendModel(relaxed)
    sig = m.evaluate({"15m": d15, "1h": h1, "4h": h4})

    assert sig.side == SignalSide.LONG
    assert sig.score >= relaxed.long_threshold
    assert sig.timeframe_assessments and len(sig.timeframe_assessments) == 3


def test_ranging_noise_tends_no_trade() -> None:
    strict = TrendModelParams(long_threshold=0.95)
    rng: list[Candle] = []
    p = 100.0
    base_ms = 1_800_000_000_000
    for i in range(260):
        p += math.sin(i / 2.8) * 0.05
        o = p
        p += math.cos(i / 5.5) * 0.06
        c = p
        rng.append(Candle(open_time_ms=base_ms + i * 60_000, open=o, high=max(o, c) + 0.01, low=min(o, c) - 0.01, close=c, volume=20.0))
    d15, h1, h4 = _repeat_tf(rng)
    sig = TrendModel(strict).evaluate({"15m": d15, "1h": h1, "4h": h4})

    assert sig.side == SignalSide.NO_TRADE
    assert sig.rejection_reasons


def test_insufficient_history_rejects_tf() -> None:
    candles = _synth_uptrend_candles(bars=30)
    sig = TrendModel().evaluate({"15m": candles, "1h": candles, "4h": candles})
    tags = tuple(a.interval for a in sig.timeframe_assessments if a.rejection_reasons)
    assert "15m" in tags


def test_timeframe_factors_include_required_names() -> None:
    candles = _synth_uptrend_candles(bars=400)
    d15, h1, h4 = _repeat_tf(candles)
    relaxed = TrendModelParams(volume_confirm_mult=0.01, min_adx=0.0, min_regression_r2=0.0, chop_reject_above=101.0)

    assessment = TrendModel(relaxed)._assess_single("1h", h1)

    names = {fc.name for fc in assessment.factor_contribs}
    expected = {"log_price_slope", "adx_dmi", "ema_stack", "donchian_break", "volume_confirm", "chop_penalty"}
    assert expected == names
    assert all(isinstance(fc, FactorContribution) for fc in assessment.factor_contribs)


def test_linreg_exponential_close_high_r2_positive_slope() -> None:
    """对数价格上指数增长 ⇒ 近似线性 ⇒ 正向斜率与高 R²。"""
    ys = [50.0 * math.exp(i * 0.01) for i in range(60)]
    slope, _i, r2, t_slope, _se = _log_close_regression([max(y, 1e-12) for y in ys])
    assert slope > 0.0 and r2 > 0.8
    assert t_slope > 10.0
