from __future__ import annotations

# =============================================================================
# BACKTEST — aligned with runner.py behavior
#
# DISCREPANCY NOTES vs runner.py (read before running):
# ─────────────────────────────────────────────────────────────────────────────
# 1. ENTRY MODE:
#    runner.py fires when mid=(bid+ask)/2 crosses entry_price in real time.
#    Backtest default (no --close-confirm): fires when bar.high/low touches entry.
#    Backtest --close-confirm: fires only on bar CLOSE then executes next bar open.
#    → Runner is closest to backtest WITHOUT --close-confirm.
#    → --close-confirm is MORE conservative than live (fewer, better entries).
#
# 2. RULE FILTERS (CRITICAL — runner always runs these):
#    runner.py always calls apply_filters() (signal_filter.py):
#      - ATR volatility filter    (blocks whipsaw conditions)
#      - Candle body filter       (blocks weak breakout bars)
#      - Session quality filter   (blocks Mon 00-06, Fri 18+, 20-22 UTC)
#    Backtest --rule-filters replicates this exactly.
#    WITHOUT --rule-filters, backtest has NO filters → results will be rosier
#    than live. Always use --rule-filters for accurate comparison.
#
# 3. PIP VALUE / LOT SIZE:
#    config.py: pip_value_per_lot = $100/lot → lot_size = 0.4
#    MetaQuotes demo: tick_value is 10x undervalued → live bot trades 4.0 lots
#    Dollar P&L is identical (4.0 lots × $10 = 0.4 lots × $100).
#    Backtest uses --pip-value 100 (config.py value) → correct in dollar terms.
#
# 4. NEWS BLACKOUT DAYS:
#    runner.py checks cfg.news_blackout_dates and skips trading entirely.
#    Backtest does not simulate this (negligible impact unless you trade NFP/FOMC).
# ─────────────────────────────────────────────────────────────────────────────
#
# RECOMMENDED COMMAND (matches runner.py most closely):
#   python backtest.py --capital 50000 --sl-target 200 --tp-target 600 \
#     --sl-pips 5 --daily-loss 200 --daily-profit 600 \
#     --threshold-pips 19 --data-dir data --months 10 \
#     --rule-filters
#   (no --close-confirm: runner fires on mid price immediately)
#
# WITH DATE RANGE:
#   ... --from-date 2025-09-01 --to-date 2025-12-31
#
# WITH TICK SIM (MQL5 Every Tick approximation):
#   ... --tick-sim
# =============================================================================

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Literal

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from config import StrategyConfig
from threshold import compute_levels, ThresholdLevels
from trade_signal import Direction
from risk_control import (
    RiskSnapshot, can_place_trade,
    is_daily_profit_hit, is_daily_limit_breached,
)


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Bar:
    time_utc: datetime
    open:     float
    high:     float
    low:      float
    close:    float
    utc_hhmm: str = ""

    def __post_init__(self):
        self.utc_hhmm = self.time_utc.strftime("%H:%M")


@dataclass
class TradeRecord:
    day:           str
    direction:     Direction
    entry_price:   float
    tp_price:      float
    sl_price:      float
    entry_bar:     str
    exit_price:    float = 0.0
    exit_bar:      str   = ""
    outcome:       str   = ""
    gross_pnl:     float = 0.0
    spread_cost:   float = 0.0
    net_pnl:       float = 0.0
    be_triggered:  bool  = False
    filter_reason: str   = ""   # non-empty = filtered signal (not traded)


@dataclass
class DayReport:
    date:              str
    start_price:       float
    lock_utc:          str   = ""
    start_source:      str   = ""
    threshold_pips:    float = 0.0
    trades:            list[TradeRecord] = field(default_factory=list)
    day_gross:         float = 0.0
    day_net:           float = 0.0
    day_spread:        float = 0.0
    hit_profit:        bool  = False
    hit_loss:          bool  = False
    hit_consec_pause:  bool  = False
    no_trigger:        bool  = False
    direction_bias:    str   = ""
    signals_filtered:  int   = 0


# =============================================================================
# RULE-BASED SIGNAL FILTERS
# Mirrors signal_filter.py exactly — same logic runner.py uses via apply_filters()
# =============================================================================

ATR_VOLATILE_THRESHOLD = 1.8
MIN_BODY_RATIO         = 0.45


def _atr_filter(bars: list[Bar], threshold: float = ATR_VOLATILE_THRESHOLD) -> tuple[bool, str]:
    """
    Block if ATR(14) > threshold × 20-period reference ATR.
    Mirrors signal_filter.filter_atr_volatile() exactly.
    """
    if len(bars) < 35:
        return True, ""

    def _atr(sl: list[Bar]) -> float:
        trs = []
        for i in range(1, len(sl)):
            h = sl[i].high; l = sl[i].low; pc = sl[i-1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    current_atr   = _atr(bars[-15:])
    reference_atr = _atr(bars[-35:-15])
    if reference_atr <= 0:
        return True, ""
    ratio = current_atr / reference_atr
    if ratio > threshold:
        return False, f"atr_volatile(ratio={ratio:.2f}>{threshold})"
    return True, ""


def _candle_filter(bars: list[Bar], min_ratio: float = MIN_BODY_RATIO) -> tuple[bool, str]:
    """
    Block if the closed breakout bar (bars[-2]) body < min_ratio × range.
    Mirrors signal_filter.filter_weak_candle() exactly.
    """
    if len(bars) < 2:
        return True, ""
    bar = bars[-2]
    bar_range = bar.high - bar.low
    if bar_range < 0.01:
        return True, ""
    ratio = abs(bar.close - bar.open) / bar_range
    if ratio < min_ratio:
        return False, f"weak_candle(body_ratio={ratio:.2f}<{min_ratio})"
    return True, ""


def _session_filter(hour_utc: int, day_of_week: int) -> tuple[bool, str]:
    """
    Block low-quality session windows.
    Mirrors signal_filter.filter_session_quality() exactly.
    Mon 00-05 UTC | Fri 18+ UTC | 20-22 UTC any day
    """
    if day_of_week == 0 and hour_utc < 6:
        return False, f"session_quality(monday_thin hour={hour_utc})"
    if day_of_week == 4 and hour_utc >= 18:
        return False, f"session_quality(friday_close hour={hour_utc})"
    if 20 <= hour_utc <= 22:
        return False, f"session_quality(late_night hour={hour_utc})"
    return True, ""


def _apply_rule_filters(
    bars: list[Bar], bar_time: datetime,
    enable_atr=True, enable_candle=True, enable_session=True,
) -> tuple[bool, str]:
    """
    Run all enabled filters. Returns (passed, reason).
    Mirrors runner.py → _handle_signal() → apply_filters() call exactly.
    """
    hour = bar_time.hour
    dow  = bar_time.weekday()  # 0=Mon, 4=Fri

    if enable_session:
        ok, r = _session_filter(hour, dow)
        if not ok: return False, r
    if enable_atr:
        ok, r = _atr_filter(bars)
        if not ok: return False, r
    if enable_candle:
        ok, r = _candle_filter(bars)
        if not ok: return False, r
    return True, ""


# =============================================================================
# START PRICE FROM DAY FILES
# =============================================================================

def _load_start_from_day_file(date: str, data_dir: str, symbol: str) -> tuple[float, str] | None:
    path = os.path.join(data_dir, symbol, f"{date}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        start = data.get("start", {})
        if start.get("status") != "LOCKED":
            return None
        price = start.get("price")
        if price is None:
            return None
        lock_iso = start.get("locked_tick_time_utc", "")
        lock_hhmm = ""
        if lock_iso:
            try:
                lock_dt = datetime.fromisoformat(lock_iso.replace("Z", "+00:00"))
                lock_hhmm = lock_dt.strftime("%H:%M")
            except Exception:
                lock_hhmm = lock_iso[11:16]
        return float(price), lock_hhmm
    except Exception:
        return None


# =============================================================================
# MT5 FETCH
# =============================================================================

def fetch_bars(symbol: str, months: int) -> list[Bar]:
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    to_dt   = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=months * 31)
    print(f"[BACKTEST] Fetching M5: {symbol} | {from_dt.date()} → {to_dt.date()} (UTC)")
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, from_dt, to_dt)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No M5 data for {symbol}.")
    bars = [
        Bar(time_utc=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]),   close=float(r["close"]))
        for r in rates
    ]
    print(f"[BACKTEST] Fetched {len(bars):,} bars")
    return bars


def fetch_bars_range(symbol: str, from_dt: datetime, to_dt: datetime) -> list[Bar]:
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    print(f"[BACKTEST] Fetching M5: {symbol} | {from_dt.date()} → {to_dt.date()} (UTC)")
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, from_dt, to_dt)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No M5 data for {symbol} in range.")
    bars = [
        Bar(time_utc=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]),   close=float(r["close"]))
        for r in rates
    ]
    print(f"[BACKTEST] Fetched {len(bars):,} bars")
    return bars


def group_by_day(bars: list[Bar]) -> dict[str, list[Bar]]:
    day_map: dict[str, list[Bar]] = defaultdict(list)
    for bar in bars:
        day_map[bar.time_utc.strftime("%Y-%m-%d")].append(bar)
    return dict(sorted(day_map.items()))


# =============================================================================
# DYNAMIC THRESHOLD
# =============================================================================

def _compute_atr_bars(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 20.0
    trs = []
    for i in range(1, len(bars)):
        h = bars[i].high; l = bars[i].low; pc = bars[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:] if len(trs) >= period else trs
    return round(sum(recent) / len(recent), 2) if recent else 20.0


def compute_dynamic_threshold_atr(prev_bars, atr_period, atr_multiplier) -> float:
    if not prev_bars:
        return 20.0
    return max(10.0, min(50.0, round(_compute_atr_bars(prev_bars, atr_period) * atr_multiplier, 1)))


def compute_dynamic_threshold_prev_range(prev_bars, range_factor) -> float:
    if not prev_bars:
        return 20.0
    return max(10.0, min(50.0, round(
        (max(b.high for b in prev_bars) - min(b.low for b in prev_bars)) * range_factor, 1
    )))


def _make_dynamic_cfg(base_cfg: StrategyConfig, threshold_pips: float) -> StrategyConfig:
    from dataclasses import replace
    return replace(base_cfg, threshold_pips=threshold_pips, entry_multiplier=1.25, exit_multiplier=2.0)


# =============================================================================
# SYNTHETIC TICK ENGINE  (MQL5 Every Tick approximation)
# =============================================================================

def _synthesize_ticks(bar: Bar) -> list[float]:
    """Bullish: O→L→H→C  |  Bearish: O→H→L→C"""
    if bar.close >= bar.open:
        return [bar.open, bar.low, bar.high, bar.close]
    return [bar.open, bar.high, bar.low, bar.close]


def _check_exit_tick_sim(direction, entry, tp, sl, bar) -> tuple[str, float] | None:
    for price in _synthesize_ticks(bar):
        if direction == "LONG":
            if price <= sl: return "SL", sl
            if price >= tp: return "TP", tp
        else:
            if price >= sl: return "SL", sl
            if price <= tp: return "TP", tp
    return None


# =============================================================================
# INTRABAR EXECUTION ENGINE
# =============================================================================

_PIP_VALUE   = 100.0
_SPREAD_PIPS = 0.35


def _spread_cost(lot_size: float) -> float:
    return round(_SPREAD_PIPS * _PIP_VALUE * lot_size, 2)


def _pnl(direction, entry, exit_p, lot_size) -> float:
    ppl = lot_size * _PIP_VALUE
    return round((exit_p - entry) * ppl if direction == "LONG"
                 else (entry - exit_p) * ppl, 2)


def _check_exit_on_bar(direction, entry, tp, sl, bar, entry_on_bar=False):
    if direction == "LONG":
        sl_hit = bar.low  <= sl; tp_hit = bar.high >= tp
        if sl_hit and tp_hit: return ("TP", tp) if (entry_on_bar and bar.close >= tp) else ("SL", sl)
        if sl_hit: return "SL", sl
        if tp_hit: return "TP", tp
    else:
        sl_hit = bar.high >= sl; tp_hit = bar.low  <= tp
        if sl_hit and tp_hit: return ("TP", tp) if (entry_on_bar and bar.close <= tp) else ("SL", sl)
        if sl_hit: return "SL", sl
        if tp_hit: return "TP", tp
    return None


def _resolve_exit(direction, entry, tp, sl, bar, tick_sim, entry_on_bar=False):
    if tick_sim:
        return _check_exit_tick_sim(direction, entry, tp, sl, bar)
    return _check_exit_on_bar(direction, entry, tp, sl, bar, entry_on_bar)


def _check_entry_on_bar(direction, entry, bar, overshoot) -> bool:
    """
    Runner-equivalent: fires when bar.high/low touches entry.
    Runner fires on mid price in real time → equivalent to 'touched this bar'.
    """
    if direction == "LONG":
        return bar.high >= entry and (bar.close - entry) <= overshoot
    return bar.low <= entry and (entry - bar.close) <= overshoot


def _check_entry_close_confirm(direction, entry, bar) -> bool:
    """More conservative than runner — requires bar CLOSE past entry."""
    if direction == "LONG": return bar.close >= entry
    return bar.close <= entry


def _trend_filter_ok(direction, bar, start_price) -> bool:
    if direction == "LONG": return bar.open >= start_price
    return bar.open <= start_price


# =============================================================================
# DAY SIMULATION
# =============================================================================

def run_day(
    date:               str,
    bars:               list[Bar],
    cfg:                StrategyConfig,
    close_confirm:      bool  = False,
    trend_filter:       bool  = False,
    rule_filters:       bool  = False,   # mirrors runner.py apply_filters()
    data_dir:           str   = "",
    session_start_utc:  str   = "",
    session_end_utc:    str   = "",
    consec_loss_pause:  int   = 0,
    dynamic_threshold:  float = 0.0,
    direction_bias:     str   = "BOTH",
    daily_trend_align:  bool  = False,
    prev_start_price:   float = 0.0,
    breakeven_stop:     float = 0.0,
    tick_sim:           bool  = False,
) -> DayReport:

    report         = DayReport(date=date, start_price=0.0)
    already_traded: set[Direction] = set()
    trade_count    = 0
    realized_pnl   = 0.0
    open_trade: TradeRecord | None = None
    pending_entry:  tuple | None   = None
    consec_losses  = 0
    be_sl: float | None = None

    # ── START PRICE ───────────────────────────────────────────────────────────
    start_price = 0.0; lock_hhmm = ""; start_source = "bar_open"
    if data_dir:
        r = _load_start_from_day_file(date, data_dir, cfg.symbol)
        if r: start_price, lock_hhmm = r; start_source = "day_file"
    if start_price == 0.0:
        start_bar = next((b for b in bars if b.utc_hhmm >= cfg.session_start_hhmm), None)
        if start_bar is None:
            report.no_trigger = True; return report
        start_price = start_bar.open; lock_hhmm = start_bar.utc_hhmm; start_source = "bar_open"

    # ── DYNAMIC THRESHOLD ─────────────────────────────────────────────────────
    active_cfg = cfg
    if dynamic_threshold > 0.0:
        active_cfg = _make_dynamic_cfg(cfg, dynamic_threshold)
        report.threshold_pips = dynamic_threshold
    else:
        report.threshold_pips = cfg.threshold_pips

    report.start_price = start_price; report.lock_utc = lock_hhmm
    report.start_source = start_source; report.direction_bias = direction_bias
    levels = compute_levels(start_price, active_cfg)

    # ── DAILY TREND ALIGN ─────────────────────────────────────────────────────
    allowed_directions: set[str] = {"LONG", "SHORT"}
    if daily_trend_align and prev_start_price > 0:
        if start_price > prev_start_price:   allowed_directions = {"LONG"}
        elif start_price < prev_start_price: allowed_directions = {"SHORT"}
    if direction_bias == "LONG":   allowed_directions &= {"LONG"}
    elif direction_bias == "SHORT": allowed_directions &= {"SHORT"}
    if not allowed_directions:     allowed_directions = {"LONG", "SHORT"}

    # ── BAR REPLAY ────────────────────────────────────────────────────────────
    for idx, bar in enumerate(bars):
        if bar.utc_hhmm < cfg.session_start_hhmm:
            continue

        # Force close
        if bar.utc_hhmm >= cfg.force_close_hhmm:
            pending_entry = None
            if open_trade is not None:
                ep = bar.open
                gross = _pnl(open_trade.direction, open_trade.entry_price, ep, active_cfg.lot_size)
                sp    = _spread_cost(active_cfg.lot_size)
                realized_pnl += gross
                open_trade.exit_price = ep; open_trade.exit_bar = bar.utc_hhmm
                open_trade.outcome = "FORCE_CLOSE"; open_trade.gross_pnl = gross
                open_trade.spread_cost = sp; open_trade.net_pnl = round(gross - sp, 2)
                open_trade = None
            break

        # ── Execute pending close-confirm ─────────────────────────────────────
        if close_confirm and pending_entry is not None:
            p_dir, p_entry, p_tp, p_sl, p_bar = pending_entry
            pending_entry = None
            exec_price = bar.open
            os_dist = (exec_price - p_entry) if p_dir == "LONG" else (p_entry - exec_price)
            if os_dist <= cfg.max_entry_overshoot_pips:
                if p_dir == "LONG":
                    sl_d = p_entry - p_sl; tp_d = p_tp - p_entry
                    act_sl = round(exec_price - sl_d, 3); act_tp = round(exec_price + tp_d, 3)
                else:
                    sl_d = p_sl - p_entry; tp_d = p_entry - p_tp
                    act_sl = round(exec_price + sl_d, 3); act_tp = round(exec_price - tp_d, 3)
                snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                    trade_count=trade_count, open_position_count=0)
                allowed, _ = can_place_trade(snap, active_cfg)
                if allowed:
                    trade = TradeRecord(day=date, direction=p_dir,
                                        entry_price=exec_price, tp_price=act_tp, sl_price=act_sl,
                                        entry_bar=f"{p_bar}→{bar.utc_hhmm}")
                    report.trades.append(trade); already_traded.add(p_dir)
                    trade_count += 1; open_trade = trade; be_sl = None

        # ── Resolve open trade ────────────────────────────────────────────────
        if open_trade is not None:
            eff_sl = be_sl if be_sl is not None else open_trade.sl_price
            if breakeven_stop > 0 and be_sl is None:
                if open_trade.direction == "LONG" and bar.high >= open_trade.entry_price + breakeven_stop:
                    be_sl = open_trade.entry_price; eff_sl = be_sl; open_trade.be_triggered = True
                elif open_trade.direction == "SHORT" and bar.low <= open_trade.entry_price - breakeven_stop:
                    be_sl = open_trade.entry_price; eff_sl = be_sl; open_trade.be_triggered = True
            result = _resolve_exit(open_trade.direction, open_trade.entry_price,
                                   open_trade.tp_price, eff_sl, bar, tick_sim)
            if result is not None:
                outcome, exit_price = result
                gross = _pnl(open_trade.direction, open_trade.entry_price, exit_price, active_cfg.lot_size)
                sp    = _spread_cost(active_cfg.lot_size)
                realized_pnl += gross
                open_trade.exit_price = exit_price; open_trade.exit_bar = bar.utc_hhmm
                open_trade.outcome = outcome; open_trade.gross_pnl = gross
                open_trade.spread_cost = sp; open_trade.net_pnl = round(gross - sp, 2)
                open_trade = None; be_sl = None
                consec_losses = (consec_losses + 1) if outcome == "SL" else 0

        # ── Daily gates ───────────────────────────────────────────────────────
        if is_daily_profit_hit(realized_pnl, active_cfg):
            report.hit_profit = True; break
        if is_daily_limit_breached(realized_pnl, active_cfg):
            report.hit_loss = True; break
        if trade_count >= active_cfg.max_trades_per_day and open_trade is None and pending_entry is None:
            break
        if consec_loss_pause > 0 and consec_losses >= consec_loss_pause:
            report.hit_consec_pause = True; break
        if open_trade is not None or pending_entry is not None:
            continue

        # ── Session filter ────────────────────────────────────────────────────
        if session_start_utc and bar.utc_hhmm < session_start_utc: continue
        if session_end_utc   and bar.utc_hhmm >= session_end_utc:  continue

        # ── Entry signal ──────────────────────────────────────────────────────
        mode = active_cfg.direction_mode
        if mode == "first_only" and already_traded:
            continue

        dirs: list[Direction] = []
        if "LONG"  not in already_traded and "LONG"  in allowed_directions: dirs.append("LONG")
        if "SHORT" not in already_traded and "SHORT" in allowed_directions: dirs.append("SHORT")

        for direction in dirs:
            entry = levels.long_entry  if direction == "LONG"  else levels.short_entry
            tp    = levels.long_tp     if direction == "LONG"  else levels.short_tp
            sl    = levels.long_sl     if direction == "LONG"  else levels.short_sl

            if trend_filter and not _trend_filter_ok(direction, bar, start_price):
                continue

            # ── Entry trigger ─────────────────────────────────────────────────
            if close_confirm:
                triggered = _check_entry_close_confirm(direction, entry, bar)
            else:
                triggered = _check_entry_on_bar(direction, entry, bar, cfg.max_entry_overshoot_pips)

            if not triggered:
                continue

            # ── RULE FILTERS — same as runner.py _handle_signal() ─────────────
            if rule_filters:
                recent_bars = bars[max(0, idx - 39):idx + 1]
                ok, reason = _apply_rule_filters(recent_bars, bar.time_utc)
                if not ok:
                    filtered_trade = TradeRecord(
                        day=date, direction=direction,
                        entry_price=entry, tp_price=tp, sl_price=sl,
                        entry_bar=bar.utc_hhmm, outcome="FILTERED", filter_reason=reason,
                    )
                    report.trades.append(filtered_trade)
                    report.signals_filtered += 1
                    # Runner: signal is consumed (evaluate_signal won't re-fire same direction)
                    already_traded.add(direction)
                    break

            # ── Risk check ────────────────────────────────────────────────────
            snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                trade_count=trade_count, open_position_count=0)
            allowed, _ = can_place_trade(snap, active_cfg)
            if not allowed:
                continue

            if close_confirm:
                pending_entry = (direction, entry, tp, sl, bar.utc_hhmm)
                break
            else:
                trade = TradeRecord(day=date, direction=direction,
                                    entry_price=entry, tp_price=tp, sl_price=sl,
                                    entry_bar=bar.utc_hhmm)
                report.trades.append(trade); already_traded.add(direction)
                trade_count += 1; be_sl = None

                result = _resolve_exit(direction, entry, tp, sl, bar, tick_sim, entry_on_bar=True)
                if result is not None:
                    outcome, exit_price = result
                    gross = _pnl(direction, entry, exit_price, active_cfg.lot_size)
                    sp    = _spread_cost(active_cfg.lot_size)
                    realized_pnl += gross
                    trade.exit_price = exit_price; trade.exit_bar = bar.utc_hhmm
                    trade.outcome = outcome; trade.gross_pnl = gross
                    trade.spread_cost = sp; trade.net_pnl = round(gross - sp, 2)
                    open_trade = None
                else:
                    open_trade = trade
                if mode == "first_only":
                    break

    # EOD close
    if open_trade is not None and bars:
        ep = bars[-1].close
        gross = _pnl(open_trade.direction, open_trade.entry_price, ep, active_cfg.lot_size)
        sp    = _spread_cost(active_cfg.lot_size)
        realized_pnl += gross
        open_trade.exit_price = ep; open_trade.exit_bar = "EOD"
        open_trade.outcome = "FORCE_CLOSE"; open_trade.gross_pnl = gross
        open_trade.spread_cost = sp; open_trade.net_pnl = round(gross - sp, 2)

    real_trades = [t for t in report.trades if t.outcome != "FILTERED"]
    report.day_gross  = round(sum(t.gross_pnl   for t in real_trades), 2)
    report.day_spread = round(sum(t.spread_cost  for t in real_trades), 2)
    report.day_net    = round(report.day_gross - report.day_spread, 2)
    if not real_trades:
        report.no_trigger = True
    return report


# =============================================================================
# REPORT PRINTER
# =============================================================================

W = 88


def print_report(
    reports:            list[DayReport],
    cfg:                StrategyConfig,
    months:             int,
    symbol:             str,
    close_confirm:      bool  = False,
    trend_filter:       bool  = False,
    rule_filters:       bool  = False,
    data_dir:           str   = "",
    session_start_utc:  str   = "",
    session_end_utc:    str   = "",
    consec_loss_pause:  int   = 0,
    dynamic_threshold:  str   = "",
    continuation_bias:  bool  = False,
    daily_trend_align:  bool  = False,
    breakeven_stop:     float = 0.0,
    tick_sim:           bool  = False,
    from_date:          str   = "",
    to_date:            str   = "",
    label:              str   = "",
):
    real_t_all   = [t for r in reports for t in r.trades if t.outcome != "FILTERED"]
    total_trades = len(real_t_all)
    total_filt   = sum(r.signals_filtered for r in reports)
    all_tp = sum(1 for t in real_t_all if t.outcome == "TP")
    all_sl = sum(1 for t in real_t_all if t.outcome == "SL")
    all_fc = sum(1 for t in real_t_all if t.outcome == "FORCE_CLOSE")
    all_be = sum(1 for t in real_t_all if t.be_triggered)
    total_gross  = sum(r.day_gross  for r in reports)
    total_spread = sum(r.day_spread for r in reports)
    total_net    = sum(r.day_net    for r in reports)
    win_days     = [r for r in reports if r.day_net > 0]
    loss_days    = [r for r in reports if r.day_net < 0]
    no_trig_days = [r for r in reports if r.no_trigger]
    profit_days  = [r for r in reports if r.hit_profit]
    loss_lim_days= [r for r in reports if r.hit_loss]
    day_file_days= sum(1 for r in reports if r.start_source == "day_file")
    thresholds   = [r.threshold_pips for r in reports if r.threshold_pips > 0]
    avg_threshold= round(sum(thresholds)/len(thresholds),1) if thresholds else cfg.threshold_pips
    win_rate     = (all_tp / total_trades * 100) if total_trades else 0
    roi          = (total_net / cfg.account_size * 100) if cfg.account_size else 0
    active_days  = len(reports) - len(no_trig_days)
    avg_per_day  = total_net / active_days if active_days else 0

    sep = "═" * W; thn = "─" * W

    entry_mode = "CLOSE-CONFIRM+NEXT-BAR" if close_confirm else "TICK-TOUCH(runner≈)"
    if trend_filter:      entry_mode += "+TREND-FILTER"
    if rule_filters:      entry_mode += "+RULE-FILTERS(ATR+CANDLE+SESSION)"
    if tick_sim:          entry_mode += "+TICK-SIM(MQL5≈)"
    if daily_trend_align: entry_mode += "+TREND-ALIGN"
    if breakeven_stop > 0:entry_mode += f"+BE-STOP({breakeven_stop:.0f}pips)"
    if continuation_bias: entry_mode += "+CONTINUATION-BIAS"
    if consec_loss_pause: entry_mode += f"+CONSEC-PAUSE({consec_loss_pause})"

    threshold_mode = dynamic_threshold if dynamic_threshold else f"fixed({cfg.threshold_pips}pips)"
    start_mode = f"day_files({day_file_days}d)+bar_open_fallback" if data_dir else "bar_open(fallback)"
    period_str = (f"{reports[0].date} → {reports[-1].date}" if reports
                  else f"{from_date or '?'} → {to_date or 'today'}")
    title = f"BACKTEST REPORT — XAUUSD  {label}" if label else "BACKTEST REPORT — XAUUSD THRESHOLD STRATEGY"

    print(f"\n╔{sep}╗")
    print(f"║{title:^{W}}║")
    print(f"╠{sep}╣")
    print((f"║  {symbol}  {period_str}  ${cfg.account_size:,.0f}  lot={cfg.lot_size}  "
           f"SL=${cfg.sl_dollar:.0f}  TP=${cfg.tp_dollar:.0f}  R:R=1:{cfg.risk_reward:.1f}  "
           f"DailyLoss=-${cfg.max_daily_loss_usd:.0f}  DailyProfit=+${cfg.daily_profit_target_usd:.0f}").ljust(W+1) + "║")
    print(f"║  Threshold: {threshold_mode}  AvgT={avg_threshold}pips".ljust(W+1) + "║")
    print(f"║  Entry: {entry_mode}".ljust(W+1) + "║")
    print(f"║  Start price: {start_mode}".ljust(W+1) + "║")
    print(f"╠{sep}╣")
    print(f"║{'DAY-BY-DAY  (all times UTC)':^{W}}║")
    print(f"╠{thn}╣")

    for r in reports:
        src_tag = "📂" if r.start_source == "day_file" else "📊"
        t_tag   = f"T={r.threshold_pips:.0f}" if dynamic_threshold else ""
        real_t  = [t for t in r.trades if t.outcome != "FILTERED"]
        filt_t  = [t for t in r.trades if t.outcome == "FILTERED"]
        if r.no_trigger:
            fn = f" [{r.signals_filtered} filtered]" if r.signals_filtered else ""
            print(f"║  {r.date}  {src_tag}S={r.start_price:<9.3f} {t_tag:<5} lock@{r.lock_utc}  No signal triggered{fn}".ljust(W+1) + "║")
            continue
        for i, t in enumerate(real_t):
            sym  = "✅" if t.outcome == "TP" else "❌" if t.outcome == "SL" else "⚠️ "
            be_t = "🔒BE" if t.be_triggered else ""
            stop = (" 🎯PROFIT" if (r.hit_profit and i == len(real_t)-1)
                    else " ⛔LOSS"  if (r.hit_loss   and i == len(real_t)-1) else "")
            ns = f"+${t.net_pnl:,.0f}" if t.net_pnl >= 0 else f"-${abs(t.net_pnl):,.0f}"
            gs = f"+${t.gross_pnl:,.0f}" if t.gross_pnl >= 0 else f"-${abs(t.gross_pnl):,.0f}"
            ls = f"lock@{r.lock_utc}" if i == 0 else " " * 9
            ds = r.date if i == 0 else " " * 10
            ss = src_tag if i == 0 else "  "
            ts = t_tag if i == 0 else " " * len(t_tag)
            print(
                f"║  {ds}  {ss}S={r.start_price:<8.3f} {ts:<5} {ls}  "
                f"{t.direction:<5}  {t.entry_bar}  "
                f"@{t.entry_price:.3f}→{t.exit_price:.3f}  "
                f"{sym}{t.outcome:<11}  {gs}  net={ns}{be_t}{stop}".ljust(W+1) + "║"
            )
        for ft in filt_t:
            print(f"║    🚫 FILTERED {ft.direction:<5}  {ft.entry_bar}  @{ft.entry_price:.3f}  {ft.filter_reason}".ljust(W+1) + "║")
        day_s = f"+${r.day_net:,.0f}" if r.day_net >= 0 else f"-${abs(r.day_net):,.0f}"
        fn = f"  [{r.signals_filtered} filtered]" if r.signals_filtered else ""
        print(f"║{'':>12}  DAY → gross={r.day_gross:+,.0f}  spread=-${r.day_spread:,.0f}  net={day_s}{fn}".ljust(W+1) + "║")
        print(f"╠{thn}╣")

    def kv(l, v): return f"║  {l:<36}{str(v):<{W-40}}  ║"

    print(f"╠{sep}╣")
    print(f"║{'MONTHLY SUMMARY':^{W}}║")
    print(f"╠{sep}╣")
    print(kv("Trading days:", len(reports)))
    print(kv("Active (had trades):", f"{active_days}  ({len(win_days)} win / {len(loss_days)} loss)"))
    print(kv("No-trigger days:", len(no_trig_days)))
    print(kv("Profit-stop days:", len(profit_days)))
    print(kv("Loss-limit days:", len(loss_lim_days)))
    print(kv("Start from day files:", f"{day_file_days} days (📂) vs {len(reports)-day_file_days} bar_open (📊)"))
    print(f"║{thn}║")
    print(kv("Total trades (real):", total_trades))
    if rule_filters:
        print(kv("Signals filtered:", f"{total_filt}  (ATR+candle+session — mirrors runner.py)"))
    else:
        print(kv("Signals filtered:", f"0  ⚠️ --rule-filters not set (runner filters NOT applied)"))
    print(kv("TP hits:", f"{all_tp}  ({win_rate:.1f}% win rate)"))
    print(kv("SL hits:", all_sl))
    print(kv("Force closed:", all_fc))
    if all_be: print(kv("Breakeven triggered:", f"{all_be} trades"))
    if tick_sim: print(kv("Tick sim mode:", "ON — synthetic OHLC path (MQL5≈)"))
    if dynamic_threshold:
        print(kv("Avg threshold pips:", f"{avg_threshold}  (min={min(thresholds):.0f}  max={max(thresholds):.0f})"))
    print(f"║{thn}║")
    print(kv("Gross P&L:", f"${total_gross:+,.2f}"))
    print(kv("Spread cost:", f"-${total_spread:,.2f}"))
    print(kv("NET P&L:", f"${total_net:+,.2f}"))
    print(kv("Avg net / active day:", f"${avg_per_day:+,.2f}"))
    print(kv("Monthly ROI:", f"{roi:.2f}%"))
    print(f"╠{sep}╣")
    print(f"║{'INCOME PROJECTION':^{W}}║")
    print(f"╠{sep}╣")
    proj_22 = avg_per_day * 22; proj_yr = proj_22 * 12
    proj_roi = (proj_yr / cfg.account_size * 100) if cfg.account_size else 0
    print(kv("Avg net / active day:", f"${avg_per_day:+,.2f}"))
    print(kv("Projected 22-day month:", f"${proj_22:+,.2f}"))
    print(kv("Projected annual:", f"${proj_yr:+,.2f}"))
    print(kv("Projected annual ROI:", f"{proj_roi:.1f}%"))
    print(f"╚{sep}╝\n")
    print(f"[INFO] Entry: {entry_mode}")
    print(f"[INFO] SL=${cfg.sl_dollar:.0f}  TP=${cfg.tp_dollar:.0f}  "
          f"R:R=1:{cfg.risk_reward:.1f}  Breakeven={cfg.breakeven_win_rate*100:.1f}%  Actual={win_rate:.1f}%\n")


# =============================================================================
# COMPARISON
# =============================================================================

def print_comparison(fixed_reports, dynamic_reports, fixed_cfg, dynamic_label):
    def stats(reports):
        tt = sum(len([t for t in r.trades if t.outcome != "FILTERED"]) for r in reports)
        tp = sum(1 for r in reports for t in r.trades if t.outcome == "TP")
        tn = sum(r.day_net for r in reports)
        ac = sum(1 for r in reports if not r.no_trigger)
        return tt, tp, (tp/tt*100) if tt else 0, tn, ac

    ft, ftp, fwr, fnet, fa = stats(fixed_reports)
    dt, dtp, dwr, dnet, da = stats(dynamic_reports)
    sep = "─" * 60
    print(f"\n{'='*60}\n  COMPARISON: Fixed vs {dynamic_label}\n{'='*60}")
    print(f"  {'Metric':<25} {'Fixed':>12} {'Dynamic':>12}\n{sep}")
    print(f"  {'Trades':<25} {ft:>12} {dt:>12}")
    print(f"  {'TP hits':<25} {ftp:>12} {dtp:>12}")
    print(f"  {'Win rate':<25} {fwr:>11.1f}% {dwr:>11.1f}%")
    print(f"  {'Net P&L':<25} ${fnet:>+10,.0f} ${dnet:>+10,.0f}")
    print(sep)
    winner = "DYNAMIC" if dnet > fnet else "FIXED"
    print(f"  Winner: {winner}  (${abs(dnet-fnet):,.0f} difference)\n{'='*60}\n")


# =============================================================================
# CLI
# =============================================================================

def _make_cfg(args) -> StrategyConfig:
    rr             = args.tp_target / args.sl_target
    base_threshold = getattr(args, 'threshold_pips', 0.0) or 20.0
    sl_pips        = getattr(args, 'sl_pips', 5.0) or 5.0
    entry_offset   = base_threshold + sl_pips
    exit_offset    = entry_offset + sl_pips * rr
    entry_mult     = round(entry_offset / base_threshold, 6)
    exit_mult      = round(exit_offset  / base_threshold, 6)

    from dataclasses import replace as dc_replace
    cfg = StrategyConfig(
        account_size=args.capital, symbol=args.symbol,
        sl_dollar_target=args.sl_target, tp_dollar_target=args.tp_target,
        entry_multiplier=entry_mult, exit_multiplier=exit_mult,
        daily_profit_target_usd=args.daily_profit, max_daily_loss_usd=args.daily_loss,
        session_start_hhmm=args.session_start, session_end_hhmm=args.session_end,
        force_close_hhmm=args.force_close, max_entry_overshoot_pips=args.overshoot,
        server_utc_offset_hours=0,
    )
    if getattr(args, 'max_trades', 0) > 0:
        cfg = dc_replace(cfg, max_trades_per_day=args.max_trades)
    if getattr(args, 'threshold_pips', 0.0) > 0:
        cfg = dc_replace(cfg, threshold_pips=args.threshold_pips,
                         daily_profit_target_usd=cfg.tp_dollar)
    return cfg


def parse_args():
    p = argparse.ArgumentParser(
        description="XAUUSD Threshold Strategy Backtest — aligned with runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
RUNNER-ALIGNED (most accurate):
  python backtest.py --capital 50000 --sl-target 200 --tp-target 600 \\
    --sl-pips 5 --daily-loss 200 --daily-profit 600 \\
    --threshold-pips 19 --data-dir data --months 10 --rule-filters

DATE RANGE:
  ... --from-date 2025-09-01 --to-date 2025-12-31

TICK SIM (MQL5 comparison):
  ... --tick-sim

CONSERVATIVE RESEARCH:
  ... --close-confirm --trend-filter --rule-filters

KEY FLAGS:
  --rule-filters  : ATR+candle+session filters (same as runner.py). RECOMMENDED.
  --close-confirm : bar close confirm, more conservative than live runner.
  --tick-sim      : OHLC → synthetic tick path (MQL5 Every Tick approx).
        """
    )
    p.add_argument("--capital",       type=float, required=True)
    p.add_argument("--sl-target",     type=float, default=200.0)
    p.add_argument("--tp-target",     type=float, default=600.0)
    p.add_argument("--sl-pips",       type=float, default=5.0)
    p.add_argument("--daily-loss",    type=float, default=200.0)
    p.add_argument("--daily-profit",  type=float, default=600.0)
    p.add_argument("--months",        type=int,   default=1)
    p.add_argument("--symbol",        type=str,   default="XAUUSD")
    p.add_argument("--data-dir",      type=str,   default="")
    p.add_argument("--close-confirm", action="store_true")
    p.add_argument("--trend-filter",  action="store_true")
    p.add_argument("--rule-filters",  action="store_true",
                   help="[RECOMMENDED] ATR+candle+session filters — same as runner.py apply_filters()")
    p.add_argument("--session-start", type=str,   default="00:00")
    p.add_argument("--session-end",   type=str,   default="23:00")
    p.add_argument("--session-london-ny", action="store_true")
    p.add_argument("--consec-loss-pause", type=int, default=0)
    p.add_argument("--force-close",   type=str,   default="23:30")
    p.add_argument("--overshoot",     type=float, default=3.0)
    p.add_argument("--verbose",       action="store_true")
    p.add_argument("--from-date",     type=str,   default="")
    p.add_argument("--to-date",       type=str,   default="")
    p.add_argument("--tick-sim",      action="store_true")
    p.add_argument("--two-trade-mode",action="store_true")
    p.add_argument("--max-trades",    type=int,   default=0)
    p.add_argument("--dynamic-threshold", type=str, default="", choices=["", "atr", "prev-day"])
    p.add_argument("--atr-period",    type=int,   default=14)
    p.add_argument("--atr-multiplier",type=float, default=1.0)
    p.add_argument("--range-factor",  type=float, default=0.35)
    p.add_argument("--continuation-bias", action="store_true")
    p.add_argument("--daily-trend-align", action="store_true")
    p.add_argument("--breakeven-stop", type=float, default=0.0)
    p.add_argument("--compare-fixed", action="store_true")
    p.add_argument("--threshold-pips", type=float, default=0.0)
    p.add_argument("--pip-value",  type=float, default=100.0,
                   help="$/pip/lot. config.py=$100. MetaQuotes demo=$10 (10x low). "
                        "Dollar P&L identical — lot auto-scales.")
    p.add_argument("--spread",     type=float, default=0.35)
    return p.parse_args()


def main():
    args = parse_args()
    if not MT5_AVAILABLE:
        print("❌ pip install MetaTrader5")
        sys.exit(1)

    if getattr(args, 'two_trade_mode', False):
        args.daily_loss = args.sl_target * 2
        if not args.max_trades: args.max_trades = 2
        print(f"[MODE] TWO-TRADE: daily-loss=${args.daily_loss:.0f}")

    if getattr(args, 'max_trades', 0) > 0 and args.daily_loss == args.sl_target:
        args.daily_loss = args.sl_target * args.max_trades
        print(f"[MODE] MAX-TRADES={args.max_trades}: daily-loss=${args.daily_loss:.0f}")

    global _PIP_VALUE, _SPREAD_PIPS
    _PIP_VALUE = args.pip_value; _SPREAD_PIPS = args.spread
    print(f"[BACKTEST] Pip value : ${_PIP_VALUE:.0f}/pip/lot")
    print(f"[BACKTEST] Spread    : {_SPREAD_PIPS} pips")
    print(f"[BACKTEST] Entry     : {'CLOSE-CONFIRM (conservative, not runner-equiv)' if args.close_confirm else 'TICK-TOUCH (runner-equivalent)'}")
    print(f"[BACKTEST] Filters   : {'RULE-FILTERS ON (ATR+candle+session — matches runner.py)' if args.rule_filters else '⚠️  OFF — add --rule-filters for accurate comparison'}")

    cfg = _make_cfg(args)
    print(cfg.summary())

    # ── Fetch bars ────────────────────────────────────────────────────────────
    try:
        from_date = args.from_date.strip(); to_date = args.to_date.strip()
        if from_date or to_date:
            to_dt   = (datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                       if to_date else datetime.now(timezone.utc))
            from_dt = (datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                       if from_date else to_dt - timedelta(days=args.months * 31))
            all_bars = fetch_bars_range(args.symbol, from_dt - timedelta(days=60), to_dt)
            bars     = [b for b in all_bars if from_dt.date() <= b.time_utc.date() <= to_dt.date()]
            pre_bars = [b for b in all_bars if b.time_utc.date() < from_dt.date()]
            print(f"[BACKTEST] Range: {from_dt.date()} → {to_dt.date()} "
                  f"({len(bars):,} bars, {len(pre_bars):,} pre-window)")
        else:
            bars = fetch_bars(args.symbol, args.months)
            pre_bars = []; from_date = ""; to_date = ""
    except Exception as e:
        print(f"❌ {e}"); sys.exit(1)

    day_map     = group_by_day(bars)
    pre_day_map = group_by_day(pre_bars) if pre_bars else {}
    print(f"[BACKTEST] {len(day_map)} UTC trading days\n")

    dates            = list(day_map.keys())
    all_dates_sorted = sorted(list(pre_day_map.keys()) + dates)
    sess_start = "07:00" if args.session_london_ny else ""
    sess_end   = "16:00" if args.session_london_ny else ""

    fixed_reports: list[DayReport] = []
    dynamic_reports: list[DayReport] = []
    prev_outcome = ""; prev_direction = ""; prev_start = 0.0

    for i, date in enumerate(dates):
        day_bars  = day_map[date]
        global_idx = all_dates_sorted.index(date)
        prev_bars_day = []
        if global_idx > 0:
            pd = all_dates_sorted[global_idx - 1]
            prev_bars_day = day_map.get(pd) or pre_day_map.get(pd) or []

        direction_bias = "BOTH"
        if args.continuation_bias and prev_outcome == "SL" and prev_direction:
            direction_bias = prev_direction

        dyn_threshold = 0.0
        if args.dynamic_threshold == "atr":
            dyn_threshold = compute_dynamic_threshold_atr(prev_bars_day, args.atr_period, args.atr_multiplier)
        elif args.dynamic_threshold == "prev-day":
            dyn_threshold = compute_dynamic_threshold_prev_range(prev_bars_day, args.range_factor)

        if args.verbose:
            fb = day_bars[0]
            print(f"[BACKTEST] {date}  bars={len(day_bars)}  open={fb.open:.3f}  "
                  f"dyn_T={dyn_threshold:.1f}  bias={direction_bias}")

        run_kwargs = dict(
            close_confirm=args.close_confirm, trend_filter=args.trend_filter,
            rule_filters=args.rule_filters, data_dir=args.data_dir,
            session_start_utc=sess_start, session_end_utc=sess_end,
            consec_loss_pause=args.consec_loss_pause, direction_bias=direction_bias,
            daily_trend_align=args.daily_trend_align, prev_start_price=prev_start,
            breakeven_stop=args.breakeven_stop, tick_sim=args.tick_sim,
        )

        r = run_day(date, day_bars, cfg, dynamic_threshold=dyn_threshold, **run_kwargs)
        dynamic_reports.append(r)

        if args.compare_fixed and args.dynamic_threshold:
            rf = run_day(date, day_bars, cfg, dynamic_threshold=0.0, **run_kwargs)
            fixed_reports.append(rf)

        real_t = [t for t in r.trades if t.outcome != "FILTERED"]
        if real_t:
            prev_outcome = real_t[-1].outcome; prev_direction = real_t[-1].direction
        else:
            prev_outcome = ""; prev_direction = ""
        prev_start = r.start_price if r.start_price > 0 else prev_start

    dyn_label = args.dynamic_threshold.upper() if args.dynamic_threshold else ""
    print_report(
        dynamic_reports, cfg, args.months, args.symbol,
        close_confirm=args.close_confirm, trend_filter=args.trend_filter,
        rule_filters=args.rule_filters, data_dir=args.data_dir,
        session_start_utc=sess_start, session_end_utc=sess_end,
        consec_loss_pause=args.consec_loss_pause, dynamic_threshold=dyn_label,
        continuation_bias=args.continuation_bias, daily_trend_align=args.daily_trend_align,
        breakeven_stop=args.breakeven_stop, tick_sim=args.tick_sim,
        from_date=args.from_date, to_date=args.to_date, label=dyn_label,
    )

    if args.compare_fixed and args.dynamic_threshold and fixed_reports:
        print_report(fixed_reports, cfg, args.months, args.symbol,
                     rule_filters=args.rule_filters, label="FIXED (comparison)")
        print_comparison(fixed_reports, dynamic_reports, cfg, dyn_label)

    _generate_html_report(dynamic_reports, cfg, args.months, args.symbol, args)


# =============================================================================
# HTML DASHBOARD
# =============================================================================

def _generate_html_report(reports, cfg, months_count, symbol, args):
    import webbrowser, json as _json
    from collections import defaultdict

    real_t_all   = [t for r in reports for t in r.trades if t.outcome != "FILTERED"]
    total_trades = len(real_t_all)
    all_tp       = sum(1 for t in real_t_all if t.outcome == "TP")
    all_sl       = sum(1 for t in real_t_all if t.outcome in ("SL", "FORCE_CLOSE"))
    total_net    = sum(r.day_net for r in reports)
    total_filt   = sum(r.signals_filtered for r in reports)
    no_trig      = sum(1 for r in reports if r.no_trigger)
    active       = len(reports) - no_trig
    win_rate     = round(all_tp / total_trades * 100, 1) if total_trades else 0
    roi          = round(total_net / cfg.account_size * 100, 2) if cfg.account_size else 0
    avg_day      = round(total_net / active, 2) if active else 0
    monthly_proj = round(avg_day * 22, 2)

    equity = cfg.account_size
    day_rows = []
    for r in reports:
        equity += r.day_net
        result = 'NO' if r.no_trigger else ('TP' if r.day_net > 0 else 'SL')
        day_rows.append({'date': r.date[5:], 'result': result,
                         'pnl': round(r.day_net, 2), 'eq': round(equity, 2),
                         'filtered': r.signals_filtered})

    peak = cfg.account_size; max_dd = 0; running = cfg.account_size
    for d in day_rows:
        running = d['eq']
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd

    month_pnl = defaultdict(float)
    for r in reports: month_pnl[r.date[:7]] += r.day_net
    month_labels = sorted(month_pnl.keys())
    month_vals   = [round(month_pnl[m], 2) for m in month_labels]

    period_label = (f"{args.from_date} → {args.to_date}" if (args.from_date or args.to_date)
                    else f"{reports[0].date} → {reports[-1].date}" if reports
                    else f"{months_count}m")

    data_json = _json.dumps({
        'days': day_rows, 'months': [m[5:] for m in month_labels], 'monthVals': month_vals,
        'capital': cfg.account_size, 'totalNet': round(total_net, 2), 'roi': roi,
        'winRate': win_rate, 'totalTrades': total_trades, 'allTp': all_tp, 'allSl': all_sl,
        'noTrig': no_trig, 'monthlyProj': monthly_proj, 'peak': round(peak, 2),
        'maxDd': round(max_dd, 2), 'maxDdPct': round(max_dd / peak * 100, 2) if peak else 0,
        'symbol': symbol, 'slTarget': cfg.sl_dollar_target, 'tpTarget': cfg.tp_dollar_target,
        'lot': cfg.lot_size, 'threshold': cfg.threshold_pips,
        'rr': round(cfg.tp_dollar_target / cfg.sl_dollar_target, 1),
        'period': period_label, 'tickSim': args.tick_sim, 'ruleFilters': args.rule_filters,
        'totalFiltered': total_filt,
    })

    warn_html = ('' if args.rule_filters else
                 '<div class="warn">⚠️ Running WITHOUT --rule-filters. Runner.py applies '
                 'ATR+candle+session filters on every signal. Add --rule-filters for '
                 'accurate comparison with live bot.</div>')

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest — {symbol}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;padding:24px}}
h1{{font-size:18px;font-weight:500;color:#94a3b8;margin-bottom:20px}}
h2{{font-size:12px;font-weight:500;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin:28px 0 12px}}
.grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:24px}}
.card{{background:#1e2330;border:1px solid #2d3448;border-radius:10px;padding:14px 18px}}
.card .label{{font-size:11px;color:#64748b;margin-bottom:6px}}
.card .val{{font-size:24px;font-weight:500}}
.card .sub{{font-size:11px;color:#64748b;margin-top:4px}}
.green{{color:#4ade80}} .red{{color:#f87171}} .amber{{color:#fbbf24}} .blue{{color:#60a5fa}}
.chart-wrap{{position:relative;width:100%;border-radius:10px;padding:16px;background:#1e2330;border:1px solid #2d3448}}
.legend{{display:flex;gap:20px;font-size:12px;color:#94a3b8;align-items:center;margin-bottom:12px;flex-wrap:wrap}}
.dot{{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:5px}}
.badge{{font-size:10px;padding:2px 8px;border-radius:4px;background:#7c3aed;color:#e9d5ff;margin-left:6px}}
.warn{{background:#1a1000;border:1px solid #854d0e;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px;color:#fde68a}}
@media(max-width:700px){{.grid4{{grid-template-columns:1fr 1fr}}.grid2{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>{symbol} · Threshold Strategy · Backtest
  <span style="font-size:13px;color:#475569;margin-left:10px">{period_label}</span>
  {'<span class="badge">TICK-SIM</span>' if args.tick_sim else ''}
  {'<span class="badge" style="background:#16a34a">RULE-FILTERS ON</span>' if args.rule_filters else '<span class="badge" style="background:#dc2626">NO FILTERS</span>'}
</h1>
{warn_html}
<div class="grid4" id="stats"></div>
<div class="grid4" id="stats2"></div>
<h2>Equity Curve</h2>
<div class="chart-wrap">
  <div class="legend">
    <span><span class="dot" style="background:#4ade80"></span>TP</span>
    <span><span class="dot" style="background:#f87171"></span>SL</span>
    <span><span class="dot" style="background:#475569;width:6px;height:6px"></span>No signal</span>
    <span style="margin-left:auto;font-size:11px;color:#475569">{symbol} · lot={cfg.lot_size} · SL=${cfg.sl_dollar_target:.0f} TP=${cfg.tp_dollar_target:.0f}</span>
  </div>
  <div style="position:relative;height:280px"><canvas id="eqChart"></canvas></div>
</div>
<div class="grid2">
  <div><h2>Daily P&amp;L</h2><div class="chart-wrap"><div style="position:relative;height:180px"><canvas id="dailyChart"></canvas></div></div></div>
  <div><h2>Monthly P&amp;L</h2><div class="chart-wrap"><div style="position:relative;height:180px"><canvas id="monthChart"></canvas></div></div></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D={data_json};
const days=D.days,labels=days.map(d=>d.date),equity=days.map(d=>d.eq);
const ptBg=days.map(d=>d.result==='TP'?'#4ade80':d.result==='SL'?'#f87171':'#475569');
const ptR=days.map(d=>d.result==='NO'?2:d.result==='TP'?5:4);
const ptBd=days.map(d=>d.result==='TP'?'#22c55e':d.result==='SL'?'#ef4444':'#334155');
function sc(l,v,s,c=''){{return`<div class="card"><div class="label">${{l}}</div><div class="val ${{c}}">${{v}}</div><div class="sub">${{s}}</div></div>`;}}
const ac=days.filter(d=>d.result!='NO').length||1;
document.getElementById('stats').innerHTML=[
  sc('Net P&L',`${{D.totalNet>=0?'+':''}}$${{D.totalNet.toLocaleString()}}`,`+${{D.roi}}% on $${{D.capital.toLocaleString()}}`,D.totalNet>=0?'green':'red'),
  sc('Win rate',`${{D.winRate}}%`,`${{D.allTp}} TP / ${{D.allSl}} SL / ${{D.noTrig}} no-signal`,'amber'),
  sc('Monthly proj',`+$${{D.monthlyProj.toLocaleString()}}`,`$${{Math.round(D.totalNet/ac)}}/active day`,'blue'),
  sc('Max drawdown',`-$${{D.maxDd.toLocaleString()}}`,`-${{D.maxDdPct}}% peak-to-trough`,'red'),
].join('');
document.getElementById('stats2').innerHTML=[
  sc('Peak equity',`$${{D.peak.toLocaleString()}}`,`+${{((D.peak/D.capital-1)*100).toFixed(1)}}% above start`),
  sc('R:R ratio',`1 : ${{D.rr}}`,`TP $${{D.tpTarget}} / SL $${{D.slTarget}}`),
  sc('Breakeven WR','25.0%',`Actual ${{D.winRate}}% — ${{(D.winRate-25).toFixed(1)}}pt edge`,'green'),
  sc('Signals filtered',D.totalFiltered,D.ruleFilters?'ATR+candle+session (runner equiv)':'⚠️ No filters applied'),
].join('');
new Chart(document.getElementById('eqChart'),{{type:'line',data:{{labels,datasets:[{{data:equity,borderColor:'#3b82f6',borderWidth:1.5,fill:true,backgroundColor:'rgba(59,130,246,0.06)',tension:0.3,pointBackgroundColor:ptBg,pointBorderColor:ptBd,pointRadius:ptR,pointHoverRadius:7,pointBorderWidth:1}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#1e2330',titleColor:'#94a3b8',bodyColor:'#e2e8f0',borderColor:'#2d3448',borderWidth:1,callbacks:{{label:ctx=>{{const d=days[ctx.dataIndex];const s=d.pnl>0?'+':'';const t=d.result==='NO'?'No signal':`${{d.result}} ${{s}}$${{Math.abs(d.pnl)}}${{d.filtered?' ['+d.filtered+' filtered]':''}}`;return[`Equity: $${{ctx.raw.toLocaleString()}}`,t];}}}}}}}},scales:{{x:{{ticks:{{color:'#475569',font:{{size:10}},maxRotation:45,autoSkip:true,maxTicksLimit:20}},grid:{{color:'rgba(255,255,255,0.04)'}}}},y:{{ticks:{{callback:v=>'$'+Math.round(v/1000)+'k',color:'#475569',font:{{size:10}}}},grid:{{color:'rgba(255,255,255,0.04)'}},min:Math.floor((Math.min(...equity)-500)/1000)*1000,max:Math.ceil((Math.max(...equity)+500)/1000)*1000}}}}}}}}]);
const dp=days.map(d=>d.pnl),bc=days.map(d=>d.pnl>0?'rgba(74,222,128,0.8)':d.pnl<0?'rgba(248,113,113,0.8)':'rgba(71,85,105,0.5)');
new Chart(document.getElementById('dailyChart'),{{type:'bar',data:{{labels,datasets:[{{data:dp,backgroundColor:bc,borderWidth:0,borderRadius:1}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#1e2330',bodyColor:'#e2e8f0',borderColor:'#2d3448',borderWidth:1,callbacks:{{label:ctx=>{{const v=ctx.raw;return(v>0?'+$':v<0?'-$':'$')+Math.abs(v);}}}}}}}},scales:{{x:{{ticks:{{display:false}},grid:{{display:false}}}},y:{{ticks:{{callback:v=>(v>=0?'+':'')+v,color:'#475569',font:{{size:10}}}},grid:{{color:'rgba(255,255,255,0.04)'}}}}}}}}}}]);
const mC=D.monthVals.map(v=>v>=0?'rgba(74,222,128,0.8)':'rgba(248,113,113,0.8)');
new Chart(document.getElementById('monthChart'),{{type:'bar',data:{{labels:D.months,datasets:[{{data:D.monthVals,backgroundColor:mC,borderWidth:0,borderRadius:4}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#1e2330',bodyColor:'#e2e8f0',borderColor:'#2d3448',borderWidth:1,callbacks:{{label:ctx=>{{const v=ctx.raw;return(v>=0?'+$':'-$')+Math.abs(v).toLocaleString();}}}}}}}},scales:{{x:{{ticks:{{color:'#475569',font:{{size:11}}}},grid:{{display:false}}}},y:{{ticks:{{callback:v=>(v>=0?'+$':'-$')+Math.abs(v/1000).toFixed(1)+'k',color:'#475569',font:{{size:10}}}},grid:{{color:'rgba(255,255,255,0.04)'}}}}}}}}}}]);
</script></body></html>"""

    out_path = os.path.abspath("backtest_report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n[HTML] Report saved → {out_path}")
    try:
        import webbrowser
        webbrowser.open(f"file:///{out_path}")
        print(f"[HTML] Opened in browser")
    except Exception:
        print(f"[HTML] Open manually in browser")


if __name__ == "__main__":
    main()