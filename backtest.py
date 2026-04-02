from __future__ import annotations

# =============================================================================
# BACKTEST — fetch real MT5 M5 data, replay strategy logic bar by bar
#
# START PRICE SOURCE (priority order):
#   1. data/XAUUSD/<date>.json  ← written by start_price.py (EXACT match to live bot)
#   2. bar.open of first M5 bar at 00:00 UTC  ← fallback if no day file
#
#   Using day JSON files ensures backtest uses the SAME start price the real
#   bot used, including the exact tick-level lock at 00:00+ UTC.
#   Pass --data-dir to point at your bot's data folder.
#
# TIMING:
#   - MT5 UI = UTC → date_mt5 = UTC date → day boundary = UTC midnight
#   - lock_hhmm_mt5 = "00:00" UTC
#   - Server UTC+03 is display only
#
# CLI:
#   python backtest.py --capital 50000 --sl-target 100 --tp-target 200 --close-confirm --trend-filter
#   python backtest.py --capital 50000 --sl-target 100 --tp-target 200 --close-confirm --months 3 --data-dir data
# =============================================================================

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from config import StrategyConfig
from threshold import compute_levels
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
    time_utc:  datetime
    open:      float
    high:      float
    low:       float
    close:     float
    utc_hhmm:  str = ""

    def __post_init__(self):
        self.utc_hhmm = self.time_utc.strftime("%H:%M")

    @property
    def server_hhmm(self) -> str:
        return self.utc_hhmm

    def init_server_time(self, _offset: int):
        pass


@dataclass
class TradeRecord:
    day:         str
    direction:   Direction
    entry_price: float
    tp_price:    float
    sl_price:    float
    entry_bar:   str
    exit_price:  float = 0.0
    exit_bar:    str   = ""
    outcome:     str   = ""
    gross_pnl:   float = 0.0
    spread_cost: float = 0.0
    net_pnl:     float = 0.0
    be_triggered: bool = False


@dataclass
class DayReport:
    date:             str
    start_price:      float
    lock_utc:         str  = ""
    start_source:     str  = ""   # "day_file" | "bar_open"
    trades:           list[TradeRecord] = field(default_factory=list)
    day_gross:        float = 0.0
    day_net:          float = 0.0
    day_spread:       float = 0.0
    hit_profit:       bool  = False
    hit_loss:         bool  = False
    hit_consec_pause: bool  = False
    no_trigger:       bool  = False
    threshold_used:   float = 0.0    # dynamic threshold for this day (0=static)
    be_triggered:     bool  = False   # any trade hit breakeven stop


# =============================================================================
# START PRICE FROM DAY FILES
# =============================================================================

def _load_start_from_day_file(date: str, data_dir: str, symbol: str) -> tuple[float, str] | None:
    """
    Load start price from the day JSON file written by start_price.py.
    Path: <data_dir>/<symbol>/<date>.json
    Returns (price, locked_utc_hhmm) or None if file not found / not locked.
    """
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
        # Extract UTC HH:MM from locked_tick_time_utc
        lock_iso = start.get("locked_tick_time_utc", "")
        lock_hhmm = ""
        if lock_iso:
            try:
                lock_dt = datetime.fromisoformat(lock_iso.replace("Z", "+00:00"))
                lock_hhmm = lock_dt.strftime("%H:%M")
            except Exception:
                lock_hhmm = lock_iso[11:16]  # fallback slice
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
        Bar(
            time_utc = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]),   close=float(r["close"]),
        )
        for r in rates
    ]
    print(f"[BACKTEST] Fetched {len(bars):,} bars")
    return bars


def group_by_day(bars: list[Bar], server_utc_offset: int = 0) -> dict[str, list[Bar]]:
    day_map: dict[str, list[Bar]] = defaultdict(list)
    for bar in bars:
        key = bar.time_utc.strftime("%Y-%m-%d")
        day_map[key].append(bar)
    return dict(sorted(day_map.items()))


# =============================================================================
# DYNAMIC THRESHOLD — ATR
# =============================================================================

def compute_atr(bars: list[Bar], period: int = 14) -> float:
    """Compute ATR from M5 bars (uses true range: max of H-L, |H-prevC|, |L-prevC|)."""
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    # Simple moving average of last `period` true ranges
    return sum(trs[-period:]) / period


def compute_dynamic_threshold(
    prev_day_bars: list[Bar],
    atr_period: int = 14,
    atr_multiplier: float = 1.0,
    clamp_min: float = 10.0,
    clamp_max: float = 50.0,
) -> float:
    """ATR-based dynamic threshold, clamped to [clamp_min, clamp_max] pips."""
    atr_val = compute_atr(prev_day_bars, atr_period)
    threshold = atr_val * atr_multiplier
    return max(clamp_min, min(clamp_max, round(threshold, 1)))


# =============================================================================
# TIME FILTER — BLACKOUT WINDOWS
# =============================================================================

BLACKOUT_WINDOWS = [
    ("00:00", "01:00"),   # midnight thin market
    ("08:30", "09:30"),   # London open spike
    ("13:15", "13:45"),   # NY open spike
]


def _in_blackout(hhmm: str) -> bool:
    """Check if HH:MM falls within any blackout window."""
    for start, end in BLACKOUT_WINDOWS:
        if start <= hhmm < end:
            return True
    return False


# =============================================================================
# INTRABAR EXECUTION ENGINE
# =============================================================================

SPREAD_PIP = 0.35


def _spread_cost(lot_size: float) -> float:
    return round(SPREAD_PIP * 100.0 * lot_size, 2)


def _pnl(direction: Direction, entry: float, exit_p: float, lot_size: float) -> float:
    ppl = lot_size * 100.0
    return round(
        (exit_p - entry) * ppl if direction == "LONG"
        else (entry - exit_p) * ppl, 2
    )


def _check_exit_on_bar(
    direction: Direction, entry: float, tp: float, sl: float,
    bar: Bar, entry_on_bar: bool = False,
) -> tuple[str, float] | None:
    """
    Check TP/SL on bar using high/low.
    entry_on_bar=True: use close as tiebreaker when both SL and TP in range.
    entry_on_bar=False: SL wins (worst-case for existing position).
    """
    if direction == "LONG":
        sl_hit = bar.low  <= sl
        tp_hit = bar.high >= tp
        if sl_hit and tp_hit:
            return ("TP", tp) if (entry_on_bar and bar.close >= tp) else ("SL", sl)
        if sl_hit: return "SL", sl
        if tp_hit: return "TP", tp
    else:
        sl_hit = bar.high >= sl
        tp_hit = bar.low  <= tp
        if sl_hit and tp_hit:
            return ("TP", tp) if (entry_on_bar and bar.close <= tp) else ("SL", sl)
        if sl_hit: return "SL", sl
        if tp_hit: return "TP", tp
    return None


def _check_entry_on_bar(direction: Direction, entry: float, bar: Bar, overshoot: float) -> bool:
    if direction == "LONG":
        return bar.high >= entry and (bar.close - entry) <= overshoot
    else:
        return bar.low <= entry and (entry - bar.close) <= overshoot


def _check_entry_close_confirm(direction: Direction, entry: float, bar: Bar) -> bool:
    """Bar must CLOSE past entry level to confirm breakout."""
    if direction == "LONG":
        return bar.close >= entry
    else:
        return bar.close <= entry


def _trend_filter_ok(direction: Direction, bar: Bar, start_price: float) -> bool:
    if direction == "LONG":
        return bar.open >= start_price
    else:
        return bar.open <= start_price


# =============================================================================
# DAY SIMULATION
# =============================================================================

def run_day(
    date:               str,
    bars:               list[Bar],
    cfg:                StrategyConfig,
    close_confirm:      bool = False,
    trend_filter:       bool = False,
    data_dir:           str  = "",
    session_start_utc:  str  = "",   # "07:00" → only enter at/after this UTC time
    session_end_utc:    str  = "",   # "16:00" → only enter before this UTC time
    consec_loss_pause:  int  = 0,    # pause rest of day after N consecutive SL hits
    # ── NEW FLAGS ────────────────────────────────────────
    dynamic_threshold:  str  = "",   # "atr" to enable ATR-based threshold
    atr_period:         int  = 14,
    atr_multiplier:     float = 1.0,
    prev_day_bars:      list[Bar] | None = None,
    continuation_bias:  bool = False,
    prev_day_outcome:   str  = "",   # "SL_LONG", "SL_SHORT", "TP", ""
    time_filter:        bool = False,
    daily_trend_align:  bool = False,
    prev_day_start:     float = 0.0,
    breakeven_stop:     float = 0.0,  # pips of profit to trigger BE
) -> DayReport:
    """
    Replay one UTC calendar day.

    Start price source priority:
      1. <data_dir>/<symbol>/<date>.json — exact real bot lock price
      2. bar.open of first M5 bar at session_start — fallback
    """
    report          = DayReport(date=date, start_price=0.0)
    already_traded: set[Direction] = set()
    trade_count     = 0
    realized_pnl    = 0.0
    open_trade: TradeRecord | None = None
    pending_entry:  tuple | None   = None
    consec_losses   = 0    # consecutive SL hits counter

    # ── LOCK START PRICE ─────────────────────────────────────────────────────
    # Try day file first (exact real bot price)
    start_price  = 0.0
    lock_hhmm    = ""
    start_source = "bar_open"

    if data_dir:
        result = _load_start_from_day_file(date, data_dir, cfg.symbol)
        if result is not None:
            start_price, lock_hhmm = result
            start_source = "day_file"

    # Fallback: first M5 bar at/after session_start
    if start_price == 0.0:
        start_bar = next((b for b in bars if b.utc_hhmm >= cfg.session_start_hhmm), None)
        if start_bar is None:
            report.no_trigger = True
            return report
        start_price  = start_bar.open
        lock_hhmm    = start_bar.utc_hhmm
        start_source = "bar_open"

    report.start_price  = start_price
    report.lock_utc     = lock_hhmm
    report.start_source = start_source

    # ── DYNAMIC THRESHOLD (FLAG 1) ──────────────────────────────────────────
    day_cfg = cfg
    if dynamic_threshold == "atr" and prev_day_bars:
        dyn_thresh = compute_dynamic_threshold(
            prev_day_bars, atr_period, atr_multiplier,
        )
        report.threshold_used = dyn_thresh
        # Rebuild config with dynamic threshold: entry=1.25x, exit=2.0x
        entry_off = dyn_thresh * cfg.entry_multiplier
        exit_off  = dyn_thresh * cfg.exit_multiplier
        sl_pips   = entry_off - dyn_thresh
        tp_pips   = exit_off - entry_off
        dyn_lot   = round(cfg.sl_dollar_target / (sl_pips * cfg.pip_value_per_lot), 2) if sl_pips > 0 else cfg.lot_size
        day_cfg = StrategyConfig(
            account_size             = cfg.account_size,
            symbol                   = cfg.symbol,
            threshold_pips           = dyn_thresh,
            entry_multiplier         = cfg.entry_multiplier,
            exit_multiplier          = cfg.exit_multiplier,
            sl_dollar_target         = cfg.sl_dollar_target,
            tp_dollar_target         = cfg.tp_dollar_target,
            daily_profit_target_usd  = cfg.daily_profit_target_usd,
            max_daily_loss_usd       = cfg.max_daily_loss_usd,
            max_trades_per_day       = cfg.max_trades_per_day,
            direction_mode           = cfg.direction_mode,
            max_entry_overshoot_pips = cfg.max_entry_overshoot_pips,
            session_start_hhmm       = cfg.session_start_hhmm,
            session_end_hhmm         = cfg.session_end_hhmm,
            force_close_hhmm         = cfg.force_close_hhmm,
            server_utc_offset_hours  = cfg.server_utc_offset_hours,
        )

    levels = compute_levels(start_price, day_cfg)

    # ── CONTINUATION BIAS (FLAG 2) ──────────────────────────────────────────
    bias_skip: set[Direction] = set()
    if continuation_bias and prev_day_outcome:
        if prev_day_outcome == "SL_LONG":
            bias_skip.add("SHORT")   # prev SL on LONG → only trade LONG today
        elif prev_day_outcome == "SL_SHORT":
            bias_skip.add("LONG")    # prev SL on SHORT → only trade SHORT today

    # ── DAILY TREND ALIGN (FLAG 4) ──────────────────────────────────────────
    trend_skip: set[Direction] = set()
    if daily_trend_align and prev_day_start > 0.0:
        if start_price > prev_day_start:
            trend_skip.add("SHORT")  # bullish → only LONG
        elif start_price < prev_day_start:
            trend_skip.add("LONG")   # bearish → only SHORT
        # equal → trade both

    # ── Breakeven stop tracking (FLAG 5) ────────────────────────────────────
    be_active = False  # whether breakeven has been triggered for current trade
    effective_sl: float = 0.0  # overridden SL when BE triggers
    lot = day_cfg.lot_size

    # ── BAR REPLAY ───────────────────────────────────────────────────────────
    for bar in bars:
        if bar.utc_hhmm < day_cfg.session_start_hhmm:
            continue

        # Force close
        if bar.utc_hhmm >= day_cfg.force_close_hhmm:
            pending_entry = None
            if open_trade is not None:
                ep    = bar.open
                gross = _pnl(open_trade.direction, open_trade.entry_price, ep, lot)
                sp    = _spread_cost(lot)
                realized_pnl          += gross
                open_trade.exit_price  = ep
                open_trade.exit_bar    = bar.utc_hhmm
                open_trade.outcome     = "FORCE_CLOSE"
                open_trade.gross_pnl   = gross
                open_trade.spread_cost = sp
                open_trade.net_pnl     = round(gross - sp, 2)
                open_trade = None
            break

        # ── Close-confirm: execute pending at this bar's open ────────────────
        if close_confirm and pending_entry is not None:
            p_dir, p_entry, p_tp, p_sl, p_signal_bar = pending_entry
            pending_entry = None

            exec_price = bar.open

            # Overshoot check: cancel if gap too large
            if p_dir == "LONG":
                open_overshoot = exec_price - p_entry
            else:
                open_overshoot = p_entry - exec_price

            if open_overshoot > day_cfg.max_entry_overshoot_pips:
                pass  # cancelled — gap too large, do not trade
            else:
                # Recalculate SL/TP from actual fill — direction-aware
                if p_dir == "LONG":
                    sl_dist = p_entry - p_sl   # positive: pips below entry
                    tp_dist = p_tp - p_entry   # positive: pips above entry
                    actual_sl = round(exec_price - sl_dist, 3)
                    actual_tp = round(exec_price + tp_dist, 3)
                else:  # SHORT: SL above entry, TP below entry
                    sl_dist = p_sl - p_entry   # positive: pips above entry
                    tp_dist = p_entry - p_tp   # positive: pips below entry
                    actual_sl = round(exec_price + sl_dist, 3)
                    actual_tp = round(exec_price - tp_dist, 3)

                snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                    trade_count=trade_count, open_position_count=0)
                allowed, _ = can_place_trade(snap, day_cfg)

                if allowed:
                    trade = TradeRecord(
                        day=date, direction=p_dir,
                        entry_price=exec_price, tp_price=actual_tp, sl_price=actual_sl,
                        entry_bar=f"{p_signal_bar}→{bar.utc_hhmm}",
                    )
                    report.trades.append(trade)
                    already_traded.add(p_dir)
                    trade_count += 1
                    open_trade = trade
                    be_active = False
                    effective_sl = actual_sl

        # ── Step 1: resolve open position ────────────────────────────────────
        if open_trade is not None:
            # ── Breakeven stop check (FLAG 5) ────────────────────────────
            if breakeven_stop > 0 and not be_active:
                if open_trade.direction == "LONG":
                    if bar.high >= open_trade.entry_price + breakeven_stop:
                        be_active = True
                        effective_sl = open_trade.entry_price
                        open_trade.be_triggered = True
                        report.be_triggered = True
                else:  # SHORT
                    if bar.low <= open_trade.entry_price - breakeven_stop:
                        be_active = True
                        effective_sl = open_trade.entry_price
                        open_trade.be_triggered = True
                        report.be_triggered = True

            # Use effective SL (may be moved to breakeven)
            current_sl = effective_sl if (breakeven_stop > 0 and be_active) else open_trade.sl_price

            result = _check_exit_on_bar(
                open_trade.direction, open_trade.entry_price,
                open_trade.tp_price, current_sl,
                bar, entry_on_bar=False,
            )
            if result is not None:
                outcome, exit_price = result
                # If BE is active and SL hit, exit at entry (breakeven)
                if be_active and outcome == "SL":
                    exit_price = open_trade.entry_price
                    outcome = "BE"
                gross = _pnl(open_trade.direction, open_trade.entry_price, exit_price, lot)
                sp    = _spread_cost(lot)
                realized_pnl          += gross
                open_trade.exit_price  = exit_price
                open_trade.exit_bar    = bar.utc_hhmm
                open_trade.outcome     = outcome
                open_trade.gross_pnl   = gross
                open_trade.spread_cost = sp
                open_trade.net_pnl     = round(gross - sp, 2)
                open_trade = None
                be_active = False
                # Track consecutive losses for circuit breaker
                if outcome == "SL":
                    consec_losses += 1
                else:
                    consec_losses = 0

        # ── Step 2: daily gates ───────────────────────────────────────────────
        if is_daily_profit_hit(realized_pnl, day_cfg):
            report.hit_profit = True; break
        if is_daily_limit_breached(realized_pnl, day_cfg):
            report.hit_loss = True; break
        if trade_count >= day_cfg.max_trades_per_day:
            break
        if consec_loss_pause > 0 and consec_losses >= consec_loss_pause:
            report.hit_consec_pause = True; break
        if open_trade is not None or pending_entry is not None:
            continue

        # ── Step 3: session filter ───────────────────────────────────────────
        if session_start_utc and bar.utc_hhmm < session_start_utc:
            continue
        if session_end_utc and bar.utc_hhmm >= session_end_utc:
            continue

        # ── Step 3b: time filter blackout (FLAG 3) ───────────────────────────
        if time_filter and _in_blackout(bar.utc_hhmm):
            continue

        # ── Step 4: entry signal ──────────────────────────────────────────────
        mode = day_cfg.direction_mode
        if mode == "first_only" and already_traded:
            continue

        dirs: list[Direction] = []
        if "LONG"  not in already_traded: dirs.append("LONG")
        if "SHORT" not in already_traded: dirs.append("SHORT")

        for direction in dirs:
            # ── Continuation bias filter (FLAG 2) ────────────────────────
            if direction in bias_skip:
                continue
            # ── Daily trend align filter (FLAG 4) ────────────────────────
            if direction in trend_skip:
                continue

            entry = levels.long_entry  if direction == "LONG"  else levels.short_entry
            tp    = levels.long_tp     if direction == "LONG"  else levels.short_tp
            sl    = levels.long_sl     if direction == "LONG"  else levels.short_sl

            if trend_filter and not _trend_filter_ok(direction, bar, start_price):
                continue

            if close_confirm:
                if not _check_entry_close_confirm(direction, entry, bar):
                    continue
                pending_entry = (direction, entry, tp, sl, bar.utc_hhmm)
                break
            else:
                # Tick-touch mode
                if not _check_entry_on_bar(direction, entry, bar, day_cfg.max_entry_overshoot_pips):
                    continue
                snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                    trade_count=trade_count, open_position_count=0)
                allowed, _ = can_place_trade(snap, day_cfg)
                if not allowed:
                    continue

                trade = TradeRecord(day=date, direction=direction,
                                    entry_price=entry, tp_price=tp, sl_price=sl,
                                    entry_bar=bar.utc_hhmm)
                report.trades.append(trade)
                already_traded.add(direction)
                trade_count += 1
                be_active = False
                effective_sl = sl

                result = _check_exit_on_bar(direction, entry, tp, sl, bar, entry_on_bar=True)
                if result is not None:
                    outcome, exit_price = result
                    gross = _pnl(direction, entry, exit_price, lot)
                    sp    = _spread_cost(lot)
                    realized_pnl    += gross
                    trade.exit_price = exit_price
                    trade.exit_bar   = bar.utc_hhmm
                    trade.outcome    = outcome
                    trade.gross_pnl  = gross
                    trade.spread_cost= sp
                    trade.net_pnl    = round(gross - sp, 2)
                    open_trade       = None
                else:
                    open_trade = trade

                if mode == "first_only":
                    break

    # EOD close
    if open_trade is not None and bars:
        ep    = bars[-1].close
        gross = _pnl(open_trade.direction, open_trade.entry_price, ep, lot)
        sp    = _spread_cost(lot)
        realized_pnl          += gross
        open_trade.exit_price  = ep
        open_trade.exit_bar    = "EOD"
        open_trade.outcome     = "FORCE_CLOSE"
        open_trade.gross_pnl   = gross
        open_trade.spread_cost = sp
        open_trade.net_pnl     = round(gross - sp, 2)

    report.day_gross  = round(sum(t.gross_pnl   for t in report.trades), 2)
    report.day_spread = round(sum(t.spread_cost  for t in report.trades), 2)
    report.day_net    = round(report.day_gross - report.day_spread, 2)
    if not report.trades:
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
    close_confirm:      bool = False,
    trend_filter:       bool = False,
    data_dir:           str  = "",
    session_start_utc:  str  = "",
    session_end_utc:    str  = "",
    consec_loss_pause:  int  = 0,
    # ── NEW FLAGS ────────────────────────────────────────
    dynamic_threshold:  str  = "",
    continuation_bias:  bool = False,
    time_filter:        bool = False,
    daily_trend_align:  bool = False,
    breakeven_stop:     float = 0.0,
):
    total_gross  = sum(r.day_gross  for r in reports)
    total_spread = sum(r.day_spread for r in reports)
    total_net    = sum(r.day_net    for r in reports)
    total_trades = sum(len(r.trades) for r in reports)

    all_tp = sum(1 for r in reports for t in r.trades if t.outcome == "TP")
    all_sl = sum(1 for r in reports for t in r.trades if t.outcome == "SL")
    all_be = sum(1 for r in reports for t in r.trades if t.outcome == "BE")
    all_fc = sum(1 for r in reports for t in r.trades if t.outcome == "FORCE_CLOSE")
    be_days = sum(1 for r in reports if r.be_triggered)
    dyn_thresholds = [r.threshold_used for r in reports if r.threshold_used > 0]
    avg_dyn_thresh = sum(dyn_thresholds) / len(dyn_thresholds) if dyn_thresholds else 0.0

    win_days      = [r for r in reports if r.day_net > 0]
    loss_days     = [r for r in reports if r.day_net < 0]
    no_trig_days  = [r for r in reports if r.no_trigger]
    profit_days   = [r for r in reports if r.hit_profit]
    loss_lim_days = [r for r in reports if r.hit_loss]
    consec_days   = [r for r in reports if r.hit_consec_pause]
    day_file_days = sum(1 for r in reports if r.start_source == "day_file")

    win_rate    = (all_tp / total_trades * 100) if total_trades else 0
    roi         = (total_net / cfg.account_size * 100) if cfg.account_size else 0
    active_days = len(reports) - len(no_trig_days)
    avg_per_day = total_net / active_days if active_days else 0

    sep = "═" * W
    thn = "─" * W

    entry_mode = "CLOSE-CONFIRM+NEXT-BAR" if close_confirm else "TICK-TOUCH⚠️ (same-bar SL possible)"
    if trend_filter: entry_mode += "+TREND-FILTER"
    if session_start_utc or session_end_utc:
        sess = f"{session_start_utc or '00:00'}-{session_end_utc or '23:00'} UTC"
        entry_mode += f"+SESSION({sess})"
    if consec_loss_pause:
        entry_mode += f"+CONSEC-PAUSE({consec_loss_pause})"
    if dynamic_threshold:
        entry_mode += f"+DYN-THRESH({dynamic_threshold.upper()})"
    if continuation_bias:
        entry_mode += "+CONT-BIAS"
    if time_filter:
        entry_mode += "+TIME-FILTER"
    if daily_trend_align:
        entry_mode += "+DAILY-TREND-ALIGN"
    if breakeven_stop > 0:
        entry_mode += f"+BE-STOP({breakeven_stop:.0f}pip)"
    start_mode = f"day_files({day_file_days}d)+bar_open_fallback" if data_dir else "bar_open(fallback only)"

    print(f"\n╔{sep}╗")
    print(f"║{'BACKTEST REPORT — XAUUSD THRESHOLD STRATEGY':^{W}}║")
    print(f"╠{sep}╣")
    print((f"║  {symbol}  {months}m  ${cfg.account_size:,.0f}  lot={cfg.lot_size}  "
           f"SL=${cfg.sl_dollar:.0f}  TP=${cfg.tp_dollar:.0f}  R:R=1:{cfg.risk_reward:.1f}  "
           f"DailyLoss=-${cfg.max_daily_loss_usd:.0f}  DailyProfit=+${cfg.daily_profit_target_usd:.0f}").ljust(W+1) + "║")
    print(f"║  Entry: {entry_mode}".ljust(W+1) + "║")
    print(f"║  Start price: {start_mode}".ljust(W+1) + "║")
    print(f"╠{sep}╣")
    print(f"║{'DAY-BY-DAY  (all times UTC)':^{W}}║")
    print(f"╠{thn}╣")

    for r in reports:
        src_tag = "📂" if r.start_source == "day_file" else "📊"
        dyn_tag = f"  T={r.threshold_used:.1f}" if r.threshold_used > 0 else ""
        be_day_tag = "  BE" if r.be_triggered else ""
        if r.no_trigger:
            print(f"║  {r.date}  {src_tag}S={r.start_price:<10.3f}  lock@{r.lock_utc}{dyn_tag}  No signal triggered".ljust(W+1) + "║")
            continue
        for i, t in enumerate(r.trades):
            sym  = "✅" if t.outcome == "TP" else "❌" if t.outcome == "SL" else "🔄" if t.outcome == "BE" else "⚠️ "
            be_tag = " BE" if t.be_triggered else ""
            stop = (" 🎯PROFIT" if (r.hit_profit and i == len(r.trades)-1)
                    else " ⛔LOSS"   if (r.hit_loss   and i == len(r.trades)-1) else "")
            net_s = f"+${t.net_pnl:,.0f}" if t.net_pnl >= 0 else f"-${abs(t.net_pnl):,.0f}"
            grs_s = f"+${t.gross_pnl:,.0f}" if t.gross_pnl >= 0 else f"-${abs(t.gross_pnl):,.0f}"
            lock_s = f"lock@{r.lock_utc}" if i == 0 else " " * 9
            date_s = r.date if i == 0 else " " * 10
            src_s  = src_tag if i == 0 else "  "
            dyn_s  = dyn_tag if i == 0 else ""
            print(
                f"║  {date_s}  {src_s}S={r.start_price:<9.3f}  {lock_s}{dyn_s}  "
                f"{t.direction:<5}  {t.entry_bar}  "
                f"@{t.entry_price:.3f}→{t.exit_price:.3f}  "
                f"{sym}{t.outcome:<11}{be_tag}  {grs_s}  net={net_s}{stop}".ljust(W+1) + "║"
            )
        day_s = f"+${r.day_net:,.0f}" if r.day_net >= 0 else f"-${abs(r.day_net):,.0f}"
        print(f"║{'':>12}  DAY → gross={r.day_gross:+,.0f}  spread=-${r.day_spread:,.0f}  net={day_s}".ljust(W+1) + "║")
        print(f"╠{thn}╣")

    def kv(l, v):
        return f"║  {l:<36}{str(v):<{W-40}}  ║"

    print(f"╠{sep}╣")
    print(f"║{'MONTHLY SUMMARY':^{W}}║")
    print(f"╠{sep}╣")
    print(kv("Trading days:", len(reports)))
    print(kv("Active (had trades):", f"{active_days}  ({len(win_days)} win / {len(loss_days)} loss)"))
    print(kv("No-trigger days:", len(no_trig_days)))
    print(kv("Profit-stop days:", len(profit_days)))
    print(kv("Loss-limit days:", len(loss_lim_days)))
    if consec_days:
        print(kv("Consec-pause days:", len(consec_days)))
    print(kv("Start from day files:", f"{day_file_days} days (📂) vs {len(reports)-day_file_days} bar_open fallback (📊)"))
    print(f"║{thn}║")
    print(kv("Total trades:", total_trades))
    print(kv("TP hits:", f"{all_tp}  ({win_rate:.1f}% win rate)"))
    print(kv("SL hits:", all_sl))
    if all_be:
        print(kv("BE (breakeven) exits:", all_be))
    print(kv("Force closed:", all_fc))
    if be_days:
        print(kv("BE triggered days:", f"{be_days} of {active_days} active"))
    if dyn_thresholds:
        print(kv("Avg dynamic threshold:", f"{avg_dyn_thresh:.1f} pips ({len(dyn_thresholds)} days)"))
    print(f"║{thn}║")
    print(kv("Gross P&L:", f"${total_gross:+,.2f}"))
    print(kv("Spread cost:", f"-${total_spread:,.2f}"))
    print(kv("NET P&L:", f"${total_net:+,.2f}"))
    print(kv("Avg net / active day:", f"${avg_per_day:+,.2f}"))
    print(kv("Monthly ROI:", f"{roi:.2f}%"))
    print(f"╠{sep}╣")
    print(f"║{'INCOME PROJECTION':^{W}}║")
    print(f"╠{sep}╣")
    proj_22  = avg_per_day * 22
    proj_yr  = proj_22 * 12
    proj_roi = (proj_yr / cfg.account_size * 100) if cfg.account_size else 0
    print(kv("Avg net / active day:", f"${avg_per_day:+,.2f}"))
    print(kv("Projected 22-day month:", f"${proj_22:+,.2f}"))
    print(kv("Projected annual:", f"${proj_yr:+,.2f}"))
    print(kv("Projected annual ROI:", f"{proj_roi:.1f}%"))

    # ── Flags comparison section ─────────────────────────────────────────────
    active_flags = []
    if dynamic_threshold:  active_flags.append(f"dynamic-threshold={dynamic_threshold}")
    if continuation_bias:  active_flags.append("continuation-bias")
    if time_filter:        active_flags.append("time-filter")
    if daily_trend_align:  active_flags.append("daily-trend-align")
    if breakeven_stop > 0: active_flags.append(f"breakeven-stop={breakeven_stop:.0f}")

    if len(active_flags) > 1:
        print(f"╠{sep}╣")
        print(f"║{'ACTIVE FLAGS COMBINATION':^{W}}║")
        print(f"╠{sep}╣")
        for flag in active_flags:
            print(kv(f"  {flag}:", "ON"))
        print(kv("Combined effect:", f"{total_trades} trades, {win_rate:.1f}% WR, ${total_net:+,.2f} net"))

    print(f"╚{sep}╝")
    print()
    print(f"[INFO] Entry: {entry_mode}")
    print(f"[INFO] Start source: {start_mode}")
    print(f"[INFO] SL=${cfg.sl_dollar:.0f}/trade  TP=${cfg.tp_dollar:.0f}/trade  "
          f"R:R=1:{cfg.risk_reward:.1f}  Breakeven={cfg.breakeven_win_rate*100:.1f}%  Actual={win_rate:.1f}%")
    print()


# =============================================================================
# CLI
# =============================================================================

def _make_cfg(args) -> StrategyConfig:
    # --sl-pips overrides the SL pip distance
    # Lot size shrinks to keep dollar risk identical
    # TP pips scales to maintain the same R:R ratio
    #
    # Default (sl_pips=2):  SL=2pip, TP=4pip, lot=0.5  at $100 SL target
    # With --sl-pips 5:     SL=5pip, TP=10pip, lot=0.2  at $100 SL target
    # With --sl-pips 7:     SL=7pip, TP=14pip, lot=~0.14 at $100 SL target
    #
    # Dollar risk identical. R:R identical. Only lot size and pip levels change.

    sl_pips_raw = 2.0                                   # default: entry_offset - breakout_offset
    rr = args.tp_target / args.sl_target               # e.g. 200/100 = 2.0

    sl_pips_override = getattr(args, 'sl_pips', None)
    if sl_pips_override and sl_pips_override != sl_pips_raw:
        # Rebuild multipliers from new SL pip count
        # entry_offset = breakout_offset + sl_pips = 20 + sl_pips
        # exit_offset  = entry_offset + tp_pips    = entry_offset + sl_pips * rr
        entry_offset   = 20.0 + sl_pips_override
        tp_pips        = sl_pips_override * rr
        exit_offset    = entry_offset + tp_pips
        entry_mult     = round(entry_offset / 20.0, 4)
        exit_mult      = round(exit_offset  / 20.0, 4)
    else:
        sl_pips_override = sl_pips_raw
        tp_pips          = sl_pips_raw * rr
        exit_offset      = 22.0 + tp_pips
        entry_mult       = 1.1
        exit_mult        = round(exit_offset / 20.0, 4)

    return StrategyConfig(
        account_size             = args.capital,
        symbol                   = args.symbol,
        sl_dollar_target         = args.sl_target,
        tp_dollar_target         = args.tp_target,
        entry_multiplier         = entry_mult,
        exit_multiplier          = exit_mult,
        daily_profit_target_usd  = args.daily_profit,
        max_daily_loss_usd       = args.daily_loss,
        session_start_hhmm       = args.session_start,
        session_end_hhmm         = args.session_end,
        force_close_hhmm         = args.force_close,
        max_entry_overshoot_pips = args.overshoot,
        server_utc_offset_hours  = 0,
    )


def parse_args():
    p = argparse.ArgumentParser(
        description     = "XAUUSD Threshold Strategy Backtest",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
--sl-pips: widen SL buffer without changing dollar risk or R:R
  Lot size shrinks automatically to keep SL=$100.
  TP pips scales to keep R:R=2:1.
  Eliminates noise-level SL hits from tight 2-pip buffer.

  --sl-pips 2  →  SL=2pip lot=0.50  TP=4pip  (default, noisy)
  --sl-pips 5  →  SL=5pip lot=0.20  TP=10pip (recommended)
  --sl-pips 7  →  SL=7pip lot=0.14  TP=14pip (wider, fewer trades)

Examples:
  python backtest.py --capital 50000 --sl-target 100 --tp-target 200 --close-confirm --trend-filter --data-dir data --sl-pips 2
  python backtest.py --capital 50000 --sl-target 100 --tp-target 200 --close-confirm --trend-filter --data-dir data --sl-pips 5
  python backtest.py --capital 50000 --sl-target 100 --tp-target 200 --close-confirm --trend-filter --data-dir data --sl-pips 7
        """
    )
    p.add_argument("--capital",       type=float, required=True)
    p.add_argument("--sl-target",     type=float, default=100.0)
    p.add_argument("--tp-target",     type=float, default=200.0)
    p.add_argument("--sl-pips",       type=float, default=2.0,
                   help="SL pip width (default 2). Wider = smaller lot, same $ risk. "
                        "Use 5-7 to avoid M5 noise hits.")
    p.add_argument("--daily-loss",    type=float, default=100.0)
    p.add_argument("--daily-profit",  type=float, default=150.0)
    p.add_argument("--months",        type=int,   default=1)
    p.add_argument("--symbol",        type=str,   default="XAUUSD")
    p.add_argument("--data-dir",      type=str,   default="",
                   help="Path to bot data/ folder for real start prices (e.g. data)")
    p.add_argument("--close-confirm", action="store_true",
                   help="Entry on bar CLOSE, execute at next bar open. RECOMMENDED.")
    p.add_argument("--trend-filter",  action="store_true",
                   help="Only LONG if above start, SHORT if below start.")
    p.add_argument("--session-start",    type=str,   default="00:00")
    p.add_argument("--session-end",      type=str,   default="23:00")
    p.add_argument("--session-london-ny", action="store_true",
                   help="Only enter during London+NY overlap: 07:00-16:00 UTC. "
                        "Filters out Asian session fake breakouts.")
    p.add_argument("--consec-loss-pause", type=int, default=0,
                   help="Pause rest of day after N consecutive SL hits. "
                        "E.g. --consec-loss-pause 2 stops after 2 SL hits in a row.")
    p.add_argument("--force-close",   type=str,   default="23:30")
    p.add_argument("--overshoot",     type=float, default=3.0)
    p.add_argument("--verbose",       action="store_true")

    # ── NEW FLAGS ────────────────────────────────────────────────────────────
    p.add_argument("--dynamic-threshold", type=str, default="", choices=["", "atr"],
                   help="Dynamic threshold mode. 'atr' = ATR-based from prev day M5 bars.")
    p.add_argument("--atr-period",   type=int,   default=14,
                   help="ATR lookback period in bars (default 14).")
    p.add_argument("--atr-multiplier", type=float, default=1.0,
                   help="ATR multiplier for threshold (default 1.0). Result clamped 10-50 pips.")
    p.add_argument("--continuation-bias", action="store_true",
                   help="After prev day SL on LONG → only LONG today. SL SHORT → only SHORT.")
    p.add_argument("--time-filter",  action="store_true",
                   help="Skip entries during blackout windows: 00-01, 08:30-09:30, 13:15-13:45 UTC.")
    p.add_argument("--daily-trend-align", action="store_true",
                   help="Only LONG if today start > yesterday start. SHORT if below.")
    p.add_argument("--breakeven-stop", type=float, default=0.0,
                   help="Move SL to entry after N pips profit (e.g. --breakeven-stop 8).")

    return p.parse_args()


def main():
    args = parse_args()
    if not MT5_AVAILABLE:
        print("❌ pip install MetaTrader5")
        sys.exit(1)

    cfg = _make_cfg(args)
    print(cfg.summary())

    if args.data_dir:
        print(f"[BACKTEST] Start price source: day files in {args.data_dir}/")
    else:
        print(f"[BACKTEST] Start price source: M5 bar open fallback (add --data-dir for exact match)")

    try:
        bars = fetch_bars(args.symbol, args.months)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    day_map = group_by_day(bars)
    dates = list(day_map.keys())
    print(f"[BACKTEST] {len(day_map)} UTC trading days\n")

    reports: list[DayReport] = []
    prev_day_outcome: str = ""
    prev_day_start: float = 0.0
    prev_day_bars_cache: list[Bar] | None = None

    for idx, date in enumerate(dates):
        day_bars = day_map[date]
        if args.verbose:
            fb = day_bars[0]
            print(f"[BACKTEST] {date}  bars={len(day_bars)}  first={fb.utc_hhmm}  open={fb.open:.3f}")
        sess_start = "07:00" if args.session_london_ny else ""
        sess_end   = "16:00" if args.session_london_ny else ""

        report = run_day(
            date, day_bars, cfg,
            close_confirm      = args.close_confirm,
            trend_filter       = args.trend_filter,
            data_dir           = args.data_dir,
            session_start_utc  = sess_start,
            session_end_utc    = sess_end,
            consec_loss_pause  = args.consec_loss_pause,
            # ── NEW FLAGS ────────────────────────────────────
            dynamic_threshold  = args.dynamic_threshold,
            atr_period         = args.atr_period,
            atr_multiplier     = args.atr_multiplier,
            prev_day_bars      = prev_day_bars_cache,
            continuation_bias  = args.continuation_bias,
            prev_day_outcome   = prev_day_outcome,
            time_filter        = args.time_filter,
            daily_trend_align  = args.daily_trend_align,
            prev_day_start     = prev_day_start,
            breakeven_stop     = args.breakeven_stop,
        )
        reports.append(report)

        # ── Track prev day state for next iteration ──────────────────────
        prev_day_bars_cache = day_bars
        prev_day_start = report.start_price

        # Determine prev_day_outcome for continuation bias
        prev_day_outcome = ""
        if report.trades:
            last_trade = report.trades[-1]
            if last_trade.outcome == "SL":
                prev_day_outcome = f"SL_{last_trade.direction}"
            elif last_trade.outcome == "TP":
                prev_day_outcome = "TP"

    sess_start = "07:00" if args.session_london_ny else ""
    sess_end   = "16:00" if args.session_london_ny else ""
    print_report(
        reports, cfg, args.months, args.symbol,
        close_confirm      = args.close_confirm,
        trend_filter       = args.trend_filter,
        data_dir           = args.data_dir,
        session_start_utc  = sess_start,
        session_end_utc    = sess_end,
        consec_loss_pause  = args.consec_loss_pause,
        dynamic_threshold  = args.dynamic_threshold,
        continuation_bias  = args.continuation_bias,
        time_filter        = args.time_filter,
        daily_trend_align  = args.daily_trend_align,
        breakeven_stop     = args.breakeven_stop,
    )


if __name__ == "__main__":
    main()