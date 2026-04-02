from __future__ import annotations

# =============================================================================
# SIGNAL FILTER — Rule-based pre-filter (Phase 1 of ML pipeline)
#
# These 3 filters are deterministic, backtestable, and require zero training data.
# They target the most common failure patterns observed in the 3-month backtest.
#
# FILTERS:
#   1. ATR volatility filter  — skip if market is too choppy
#   2. Candle body filter     — skip if breakout bar is weak (wick-heavy)
#   3. Session quality filter — skip low-quality time windows
#
# HOW TO ENABLE:
#   In config.py, set:
#     filter_atr_volatile   : bool = True
#     filter_weak_candle    : bool = True
#     filter_session_quality: bool = True
#
# BACKTEST FLAG:
#   python backtest.py ... --rule-filters
#
# EACH FILTER IS INDEPENDENT — enable/disable individually.
# Log the filter_applied reason to signal_log.csv for ML training later.
# =============================================================================

from dataclasses import dataclass
from typing import Optional


@dataclass
class FilterResult:
    passed: bool
    reason: str   # empty if passed, filter name if blocked


# =============================================================================
# FILTER 1 — ATR Volatility Filter
# =============================================================================
# Problem: When ATR is 2× its recent average, price is whipsawing.
# Breakouts in choppy conditions are fake-outs.
#
# Pattern from backtest:
#   Mar 26 – Apr 02: 7 straight SLs during tariff crash volatility spike
#   Jan 06 – Jan 09: 4 straight SLs during ranging high-ATR period
#
# Rule: Skip if ATR(14) > ATR_THRESHOLD × 20-period ATR average
# Default threshold: 1.8 (80% above average = danger zone)

ATR_VOLATILE_THRESHOLD = 1.8   # configurable

def filter_atr_volatile(bars_m5: list, threshold: float = ATR_VOLATILE_THRESHOLD) -> FilterResult:
    """
    Returns BLOCK if current ATR(14) is more than threshold× the 20-period ATR avg.
    Needs at least 35 bars (14 current + 20 comparison + 1 prev-bar).

    bars_m5: list of dicts {open, high, low, close} — most recent last.
    """
    if len(bars_m5) < 35:
        return FilterResult(passed=True, reason="")   # not enough data — pass through

    def _atr(bars_slice: list) -> float:
        trs = []
        for i in range(1, len(bars_slice)):
            b   = bars_slice[i]
            b_p = bars_slice[i-1]
            tr  = max(
                b["high"] - b["low"],
                abs(b["high"] - b_p["close"]),
                abs(b["low"]  - b_p["close"]),
            )
            trs.append(tr)
        return sum(trs) / len(trs) if trs else 0.0

    # ATR(14) from last 14 bars
    current_atr  = _atr(bars_m5[-15:])   # 15 bars → 14 TR values

    # Reference ATR from bars 15-35 (older 20-bar window)
    reference_atr = _atr(bars_m5[-35:-15])

    if reference_atr <= 0:
        return FilterResult(passed=True, reason="")

    ratio = current_atr / reference_atr

    if ratio > threshold:
        return FilterResult(
            passed=False,
            reason=f"atr_volatile(ratio={ratio:.2f}>{threshold})"
        )

    return FilterResult(passed=True, reason="")


# =============================================================================
# FILTER 2 — Candle Body Filter
# =============================================================================
# Problem: A breakout bar with a small body (mostly wicks) signals indecision,
# not conviction. Real breakouts have strong body momentum.
#
# Pattern from backtest:
#   Many SL days had the breakout bar close barely past the threshold —
#   the candle "touched" the level on a wick but lacked follow-through.
#
# Rule: Skip if breakout bar body < MIN_BODY_RATIO of total range
# Default: 0.45 (body must be at least 45% of the bar's total range)

MIN_BODY_RATIO = 0.45   # configurable

def filter_weak_candle(bars_m5: list, min_ratio: float = MIN_BODY_RATIO) -> FilterResult:
    """
    Returns BLOCK if the breakout bar (last closed bar = bars_m5[-2]) has
    a body smaller than min_ratio × total range.

    bars_m5[-1] is the current forming bar.
    bars_m5[-2] is the closed breakout bar.
    """
    if len(bars_m5) < 2:
        return FilterResult(passed=True, reason="")

    bar = bars_m5[-2]   # last closed bar
    bar_range = bar["high"] - bar["low"]

    if bar_range < 0.01:
        return FilterResult(passed=True, reason="")   # doji / no range — skip filter

    bar_body  = abs(bar["close"] - bar["open"])
    ratio     = bar_body / bar_range

    if ratio < min_ratio:
        return FilterResult(
            passed=False,
            reason=f"weak_candle(body_ratio={ratio:.2f}<{min_ratio})"
        )

    return FilterResult(passed=True, reason="")


# =============================================================================
# FILTER 3 — Session Quality Filter
# =============================================================================
# Problem: Certain time windows have systematically worse outcomes:
#   - Monday 00:00-06:00 UTC: thin liquidity, gap fills, erratic moves
#   - Friday 18:00+ UTC: position squaring, directional moves fade
#   - Late-night 20:00-23:00 UTC: low volume, fake breakouts
#
# Pattern from backtest:
#   Jan 29: SHORT SL at 22:50 UTC (late session)
#   Jan 16: SHORT SL at 15:20 UTC (Friday afternoon wind-down)
#   Multiple Monday early SLs
#
# Rule: Skip during known low-quality windows

def filter_session_quality(hour_utc: int, day_of_week: int) -> FilterResult:
    """
    Returns BLOCK for low-quality session windows.

    hour_utc: 0-23
    day_of_week: 0=Monday, 1=Tuesday, ..., 4=Friday, 5=Saturday, 6=Sunday
    """
    # Monday thin liquidity (00:00-05:59 UTC)
    if day_of_week == 0 and hour_utc < 6:
        return FilterResult(
            passed=False,
            reason=f"session_quality(monday_thin hour={hour_utc})"
        )

    # Friday position squaring (18:00-23:59 UTC)
    if day_of_week == 4 and hour_utc >= 18:
        return FilterResult(
            passed=False,
            reason=f"session_quality(friday_close hour={hour_utc})"
        )

    # Late night low-volume window (20:00-23:00 UTC, any day)
    if 20 <= hour_utc <= 22:
        return FilterResult(
            passed=False,
            reason=f"session_quality(late_night hour={hour_utc})"
        )

    return FilterResult(passed=True, reason="")


# =============================================================================
# MASTER GATE — Run all enabled filters
# =============================================================================

def apply_filters(
    bars_m5: list,
    hour_utc: int,
    day_of_week: int,
    enable_atr: bool    = True,
    enable_candle: bool = True,
    enable_session: bool = True,
) -> FilterResult:
    """
    Run all enabled filters in order. Returns on first block.
    All filters must pass for the signal to be traded.

    Args:
        bars_m5      : list of recent M5 bars, most recent last
        hour_utc     : current UTC hour (0-23)
        day_of_week  : 0=Mon, 4=Fri
        enable_*     : toggle each filter on/off

    Returns FilterResult with passed=True if all filters pass.
    """
    if enable_session:
        r = filter_session_quality(hour_utc, day_of_week)
        if not r.passed:
            return r

    if enable_atr:
        r = filter_atr_volatile(bars_m5)
        if not r.passed:
            return r

    if enable_candle:
        r = filter_weak_candle(bars_m5)
        if not r.passed:
            return r

    return FilterResult(passed=True, reason="")


# =============================================================================
# SELF-TEST
# =============================================================================

if __name__ == "__main__":
    import random

    print("Filter self-test\n" + "="*40)

    # ── ATR filter test ───────────────────────────────────────────────────
    def _make_bar(close, volatility=1.0):
        o = close - random.uniform(0, volatility)
        h = close + random.uniform(0, volatility)
        l = close - random.uniform(0, volatility * 1.5)
        return {"open": o, "high": h, "low": l, "close": close}

    # Normal market (low ATR)
    normal_bars = [_make_bar(2000 + i, 0.5) for i in range(40)]
    r = filter_atr_volatile(normal_bars)
    print(f"ATR normal  : passed={r.passed}  reason='{r.reason}'")

    # Volatile market (high ATR)
    volatile_bars = [_make_bar(2000 + i, 0.5) for i in range(20)]
    volatile_bars += [_make_bar(2000 + i, 5.0) for i in range(20)]
    r = filter_atr_volatile(volatile_bars)
    print(f"ATR volatile: passed={r.passed}  reason='{r.reason}'")

    # ── Candle filter test ────────────────────────────────────────────────
    strong_bars = [{"open": 2000, "high": 2010, "low": 1998, "close": 2008}] * 3
    r = filter_weak_candle(strong_bars)
    print(f"\nCandle strong: passed={r.passed}  reason='{r.reason}'")

    weak_bars = [{"open": 2004, "high": 2010, "low": 1998, "close": 2005}] * 3
    r = filter_weak_candle(weak_bars)
    print(f"Candle weak  : passed={r.passed}  reason='{r.reason}'")

    # ── Session filter test ───────────────────────────────────────────────
    print(f"\nSession Mon 03:00 : {filter_session_quality(3, 0)}")
    print(f"Session Mon 08:00 : {filter_session_quality(8, 0)}")
    print(f"Session Fri 19:00 : {filter_session_quality(19, 4)}")
    print(f"Session Tue 02:00 : {filter_session_quality(2, 1)}")
    print(f"Session Any 21:00 : {filter_session_quality(21, 2)}")
    print(f"Session Tue 04:00 : {filter_session_quality(4, 1)}")