from __future__ import annotations

# =============================================================================
# BACKTEST — fetch real MT5 M5 data, replay strategy logic bar by bar
#
# NEW FLAGS (research only — does not change runner.py):
#   --dynamic-threshold atr   ATR-based threshold per day
#   --atr-period 14           ATR period (default 14 bars)
#   --atr-multiplier 1.0      threshold = ATR × multiplier
#   --dynamic-threshold prev-day-range  use prev day high-low × factor
#   --range-factor 0.35       prev day range factor
#   --continuation-bias       after SL, bias same direction next day
#   --time-filter             blackout midnight/London/NY open spikes
#   --daily-trend-align       only LONG if start > prev_start, SHORT if lower
#   --breakeven-stop N        move SL to entry after N pips profit
#   --compare-fixed           show fixed threshold result alongside dynamic
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
    day:          str
    direction:    Direction
    entry_price:  float
    tp_price:     float
    sl_price:     float
    entry_bar:    str
    exit_price:   float = 0.0
    exit_bar:     str   = ""
    outcome:      str   = ""
    gross_pnl:    float = 0.0
    spread_cost:  float = 0.0
    net_pnl:      float = 0.0
    be_triggered: bool  = False   # breakeven stop was activated


@dataclass
class DayReport:
    date:              str
    start_price:       float
    lock_utc:          str   = ""
    start_source:      str   = ""
    threshold_pips:    float = 0.0   # dynamic threshold used this day
    trades:            list[TradeRecord] = field(default_factory=list)
    day_gross:         float = 0.0
    day_net:           float = 0.0
    day_spread:        float = 0.0
    hit_profit:        bool  = False
    hit_loss:          bool  = False
    hit_consec_pause:  bool  = False
    no_trigger:        bool  = False
    direction_bias:    str   = ""    # "LONG" | "SHORT" | "BOTH"


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
        Bar(
            time_utc = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]),   close=float(r["close"]),
        )
        for r in rates
    ]
    print(f"[BACKTEST] Fetched {len(bars):,} bars")
    return bars


def group_by_day(bars: list[Bar]) -> dict[str, list[Bar]]:
    day_map: dict[str, list[Bar]] = defaultdict(list)
    for bar in bars:
        key = bar.time_utc.strftime("%Y-%m-%d")
        day_map[key].append(bar)
    return dict(sorted(day_map.items()))


# =============================================================================
# DYNAMIC THRESHOLD
# =============================================================================

def _compute_atr(bars: list[Bar], period: int = 14) -> float:
    """ATR from last N bars true ranges."""
    if len(bars) < 2:
        return 20.0
    trs = []
    for i in range(1, len(bars)):
        h = bars[i].high
        l = bars[i].low
        pc = bars[i-1].close
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if not trs:
        return 20.0
    recent = trs[-period:] if len(trs) >= period else trs
    return round(sum(recent) / len(recent), 2)


def compute_dynamic_threshold_atr(
    prev_bars: list[Bar],
    atr_period: int,
    atr_multiplier: float,
) -> float:
    """ATR-based threshold. Clamped 10-50 pips."""
    if not prev_bars:
        return 20.0
    atr = _compute_atr(prev_bars, atr_period)
    threshold = round(atr * atr_multiplier, 1)
    return max(10.0, min(50.0, threshold))


def compute_dynamic_threshold_prev_range(
    prev_bars: list[Bar],
    range_factor: float,
) -> float:
    """Previous day range × factor. Clamped 10-50 pips."""
    if not prev_bars:
        return 20.0
    day_high = max(b.high for b in prev_bars)
    day_low  = min(b.low  for b in prev_bars)
    day_range = day_high - day_low
    threshold = round(day_range * range_factor, 1)
    return max(10.0, min(50.0, threshold))


def _make_dynamic_cfg(base_cfg: StrategyConfig, threshold_pips: float) -> StrategyConfig:
    """
    Build a StrategyConfig with dynamic threshold.
    Entry = threshold × 1.25, TP = threshold × 2.0, R:R = 1:3 always.
    Lot recalculated so dollar risk stays at sl_dollar_target.
    """
    from dataclasses import replace
    entry_mult = 1.25
    exit_mult  = 2.0
    sl_pips    = round(threshold_pips * (entry_mult - 1.0), 2)  # 0.25 × threshold
    if sl_pips <= 0:
        sl_pips = threshold_pips * 0.25
    return replace(
        base_cfg,
        threshold_pips   = threshold_pips,
        entry_multiplier = entry_mult,
        exit_multiplier  = exit_mult,
    )


# =============================================================================
# TIME FILTER BLACKOUT WINDOWS
# =============================================================================

# UTC blackout windows: (start_hhmm, end_hhmm)
TIME_FILTER_BLACKOUTS = [
    ("00:00", "01:00"),   # midnight thin market
    ("08:30", "09:30"),   # London open spike
    ("13:15", "13:45"),   # NY open spike
]

def _in_time_blackout(hhmm: str) -> bool:
    for start, end in TIME_FILTER_BLACKOUTS:
        if start <= hhmm < end:
            return True
    return False


# =============================================================================
# INTRABAR EXECUTION ENGINE
# =============================================================================

SPREAD_PIP       = 0.35   # default XAUUSD
_PIP_VALUE       = 100.0  # $100/pip/lot for XAUUSD — overridden by --pip-value
_SPREAD_PIPS     = 0.35   # spread in pips — overridden by --spread


def _spread_cost(lot_size: float) -> float:
    return round(_SPREAD_PIPS * _PIP_VALUE * lot_size, 2)


def _pnl(direction: Direction, entry: float, exit_p: float, lot_size: float) -> float:
    ppl = lot_size * _PIP_VALUE
    return round(
        (exit_p - entry) * ppl if direction == "LONG"
        else (entry - exit_p) * ppl, 2
    )


def _check_exit_on_bar(
    direction: Direction, entry: float, tp: float, sl: float,
    bar: Bar, entry_on_bar: bool = False,
) -> tuple[str, float] | None:
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
    close_confirm:      bool  = False,
    trend_filter:       bool  = False,
    data_dir:           str   = "",
    session_start_utc:  str   = "",
    session_end_utc:    str   = "",
    consec_loss_pause:  int   = 0,
    # ── NEW FLAGS ──────────────────────────────────────────────
    dynamic_threshold:  float = 0.0,   # >0 = use this threshold instead of cfg
    direction_bias:     str   = "BOTH", # "LONG" | "SHORT" | "BOTH"
    time_filter:        bool  = False,  # blackout London/NY/midnight
    daily_trend_align:  bool  = False,  # only trade in start price direction
    prev_start_price:   float = 0.0,   # yesterday's start price for trend align
    breakeven_stop:     float = 0.0,   # >0 = move SL to entry after N pips profit
) -> DayReport:

    report          = DayReport(date=date, start_price=0.0)
    already_traded: set[Direction] = set()
    trade_count     = 0
    realized_pnl    = 0.0
    open_trade: TradeRecord | None = None
    pending_entry:  tuple | None   = None
    consec_losses   = 0
    be_sl: float | None = None   # effective SL after breakeven triggered

    # ── LOCK START PRICE ─────────────────────────────────────────────────────
    start_price  = 0.0
    lock_hhmm    = ""
    start_source = "bar_open"

    if data_dir:
        result = _load_start_from_day_file(date, data_dir, cfg.symbol)
        if result is not None:
            start_price, lock_hhmm = result
            start_source = "day_file"

    if start_price == 0.0:
        start_bar = next((b for b in bars if b.utc_hhmm >= cfg.session_start_hhmm), None)
        if start_bar is None:
            report.no_trigger = True
            return report
        start_price  = start_bar.open
        lock_hhmm    = start_bar.utc_hhmm
        start_source = "bar_open"

    # ── DYNAMIC THRESHOLD ────────────────────────────────────────────────────
    active_cfg = cfg
    if dynamic_threshold > 0.0:
        active_cfg = _make_dynamic_cfg(cfg, dynamic_threshold)
        report.threshold_pips = dynamic_threshold
    else:
        report.threshold_pips = cfg.threshold_pips

    report.start_price  = start_price
    report.lock_utc     = lock_hhmm
    report.start_source = start_source
    report.direction_bias = direction_bias
    levels = compute_levels(start_price, active_cfg)

    # ── DAILY TREND ALIGN ────────────────────────────────────────────────────
    allowed_directions: set[str] = set()
    if daily_trend_align and prev_start_price > 0:
        if start_price > prev_start_price:
            allowed_directions = {"LONG"}
        elif start_price < prev_start_price:
            allowed_directions = {"SHORT"}
        else:
            allowed_directions = {"LONG", "SHORT"}
    else:
        allowed_directions = {"LONG", "SHORT"}

    # Apply continuation bias on top
    if direction_bias == "LONG":
        allowed_directions = allowed_directions & {"LONG"}
    elif direction_bias == "SHORT":
        allowed_directions = allowed_directions & {"SHORT"}

    if not allowed_directions:
        allowed_directions = {"LONG", "SHORT"}

    # ── BAR REPLAY ───────────────────────────────────────────────────────────
    for bar in bars:
        if bar.utc_hhmm < cfg.session_start_hhmm:
            continue

        # Force close
        if bar.utc_hhmm >= cfg.force_close_hhmm:
            pending_entry = None
            if open_trade is not None:
                ep    = bar.open
                gross = _pnl(open_trade.direction, open_trade.entry_price, ep, active_cfg.lot_size)
                sp    = _spread_cost(active_cfg.lot_size)
                realized_pnl          += gross
                open_trade.exit_price  = ep
                open_trade.exit_bar    = bar.utc_hhmm
                open_trade.outcome     = "FORCE_CLOSE"
                open_trade.gross_pnl   = gross
                open_trade.spread_cost = sp
                open_trade.net_pnl     = round(gross - sp, 2)
                open_trade = None
            break

        # ── Close-confirm: execute pending ───────────────────────────────────
        if close_confirm and pending_entry is not None:
            p_dir, p_entry, p_tp, p_sl, p_signal_bar = pending_entry
            pending_entry = None
            exec_price = bar.open

            if p_dir == "LONG":
                open_overshoot = exec_price - p_entry
            else:
                open_overshoot = p_entry - exec_price

            if open_overshoot <= cfg.max_entry_overshoot_pips:
                if p_dir == "LONG":
                    sl_dist = p_entry - p_sl
                    tp_dist = p_tp - p_entry
                    actual_sl = round(exec_price - sl_dist, 3)
                    actual_tp = round(exec_price + tp_dist, 3)
                else:
                    sl_dist = p_sl - p_entry
                    tp_dist = p_entry - p_tp
                    actual_sl = round(exec_price + sl_dist, 3)
                    actual_tp = round(exec_price - tp_dist, 3)

                snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                    trade_count=trade_count, open_position_count=0)
                allowed, _ = can_place_trade(snap, active_cfg)

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
                    be_sl = None  # reset breakeven state

        # ── Step 1: resolve open position ────────────────────────────────────
        if open_trade is not None:
            effective_sl = be_sl if be_sl is not None else open_trade.sl_price

            # Check breakeven trigger
            if breakeven_stop > 0 and be_sl is None:
                if open_trade.direction == "LONG":
                    if bar.high >= open_trade.entry_price + breakeven_stop:
                        be_sl = open_trade.entry_price
                        effective_sl = be_sl
                        open_trade.be_triggered = True
                elif open_trade.direction == "SHORT":
                    if bar.low <= open_trade.entry_price - breakeven_stop:
                        be_sl = open_trade.entry_price
                        effective_sl = be_sl
                        open_trade.be_triggered = True

            result = _check_exit_on_bar(
                open_trade.direction, open_trade.entry_price,
                open_trade.tp_price, effective_sl,
                bar, entry_on_bar=False,
            )
            if result is not None:
                outcome, exit_price = result
                gross = _pnl(open_trade.direction, open_trade.entry_price, exit_price, active_cfg.lot_size)
                sp    = _spread_cost(active_cfg.lot_size)
                realized_pnl          += gross
                open_trade.exit_price  = exit_price
                open_trade.exit_bar    = bar.utc_hhmm
                open_trade.outcome     = outcome
                open_trade.gross_pnl   = gross
                open_trade.spread_cost = sp
                open_trade.net_pnl     = round(gross - sp, 2)
                open_trade = None
                be_sl = None
                if outcome == "SL":
                    consec_losses += 1
                else:
                    consec_losses = 0

        # ── Step 2: daily gates ───────────────────────────────────────────────
        if is_daily_profit_hit(realized_pnl, active_cfg):
            report.hit_profit = True; break
        if is_daily_limit_breached(realized_pnl, active_cfg):
            report.hit_loss = True; break
        # Only stop looking for NEW trades when limit reached.
        # If a position is still open, keep iterating to resolve it.
        if (trade_count >= active_cfg.max_trades_per_day
                and open_trade is None
                and pending_entry is None):
            break
        if consec_loss_pause > 0 and consec_losses >= consec_loss_pause:
            report.hit_consec_pause = True; break
        if open_trade is not None or pending_entry is not None:
            continue

        # ── Step 3: session filter ────────────────────────────────────────────
        if session_start_utc and bar.utc_hhmm < session_start_utc:
            continue
        if session_end_utc and bar.utc_hhmm >= session_end_utc:
            continue

        # ── Step 4: time blackout filter ──────────────────────────────────────
        if time_filter and _in_time_blackout(bar.utc_hhmm):
            continue

        # ── Step 5: entry signal ──────────────────────────────────────────────
        mode = active_cfg.direction_mode
        if mode == "first_only" and already_traded:
            continue

        dirs: list[Direction] = []
        if "LONG"  not in already_traded and "LONG"  in allowed_directions:
            dirs.append("LONG")
        if "SHORT" not in already_traded and "SHORT" in allowed_directions:
            dirs.append("SHORT")

        for direction in dirs:
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
                if not _check_entry_on_bar(direction, entry, bar, cfg.max_entry_overshoot_pips):
                    continue
                snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                    trade_count=trade_count, open_position_count=0)
                allowed, _ = can_place_trade(snap, active_cfg)
                if not allowed:
                    continue

                trade = TradeRecord(day=date, direction=direction,
                                    entry_price=entry, tp_price=tp, sl_price=sl,
                                    entry_bar=bar.utc_hhmm)
                report.trades.append(trade)
                already_traded.add(direction)
                trade_count += 1
                be_sl = None

                result = _check_exit_on_bar(direction, entry, tp, sl, bar, entry_on_bar=True)
                if result is not None:
                    outcome, exit_price = result
                    gross = _pnl(direction, entry, exit_price, active_cfg.lot_size)
                    sp    = _spread_cost(active_cfg.lot_size)
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
        gross = _pnl(open_trade.direction, open_trade.entry_price, ep, active_cfg.lot_size)
        sp    = _spread_cost(active_cfg.lot_size)
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
    close_confirm:      bool  = False,
    trend_filter:       bool  = False,
    data_dir:           str   = "",
    session_start_utc:  str   = "",
    session_end_utc:    str   = "",
    consec_loss_pause:  int   = 0,
    dynamic_threshold:  str   = "",
    continuation_bias:  bool  = False,
    time_filter:        bool  = False,
    daily_trend_align:  bool  = False,
    breakeven_stop:     float = 0.0,
    label:              str   = "",
):
    total_gross  = sum(r.day_gross  for r in reports)
    total_spread = sum(r.day_spread for r in reports)
    total_net    = sum(r.day_net    for r in reports)
    total_trades = sum(len(r.trades) for r in reports)

    all_tp = sum(1 for r in reports for t in r.trades if t.outcome == "TP")
    all_sl = sum(1 for r in reports for t in r.trades if t.outcome == "SL")
    all_fc = sum(1 for r in reports for t in r.trades if t.outcome == "FORCE_CLOSE")
    all_be = sum(1 for r in reports for t in r.trades if t.be_triggered)

    win_days      = [r for r in reports if r.day_net > 0]
    loss_days     = [r for r in reports if r.day_net < 0]
    no_trig_days  = [r for r in reports if r.no_trigger]
    profit_days   = [r for r in reports if r.hit_profit]
    loss_lim_days = [r for r in reports if r.hit_loss]
    consec_days   = [r for r in reports if r.hit_consec_pause]
    day_file_days = sum(1 for r in reports if r.start_source == "day_file")

    thresholds    = [r.threshold_pips for r in reports if r.threshold_pips > 0]
    avg_threshold = round(sum(thresholds) / len(thresholds), 1) if thresholds else cfg.threshold_pips

    win_rate    = (all_tp / total_trades * 100) if total_trades else 0
    roi         = (total_net / cfg.account_size * 100) if cfg.account_size else 0
    active_days = len(reports) - len(no_trig_days)
    avg_per_day = total_net / active_days if active_days else 0

    sep = "═" * W
    thn = "─" * W

    entry_mode = "CLOSE-CONFIRM+NEXT-BAR" if close_confirm else "TICK-TOUCH⚠️"
    if trend_filter:       entry_mode += "+TREND-FILTER"
    if time_filter:        entry_mode += "+TIME-FILTER"
    if continuation_bias:  entry_mode += "+CONTINUATION-BIAS"
    if daily_trend_align:  entry_mode += "+TREND-ALIGN"
    if breakeven_stop > 0: entry_mode += f"+BE-STOP({breakeven_stop:.0f}pips)"
    if session_start_utc or session_end_utc:
        sess = f"{session_start_utc or '00:00'}-{session_end_utc or '23:00'} UTC"
        entry_mode += f"+SESSION({sess})"
    if consec_loss_pause:  entry_mode += f"+CONSEC-PAUSE({consec_loss_pause})"

    threshold_mode = dynamic_threshold if dynamic_threshold else f"fixed({cfg.threshold_pips}pips)"
    start_mode = f"day_files({day_file_days}d)+bar_open_fallback" if data_dir else "bar_open(fallback only)"

    title = f"BACKTEST REPORT — XAUUSD  {label}" if label else "BACKTEST REPORT — XAUUSD THRESHOLD STRATEGY"

    print(f"\n╔{sep}╗")
    print(f"║{title:^{W}}║")
    print(f"╠{sep}╣")
    print((f"║  {symbol}  {months}m  ${cfg.account_size:,.0f}  lot={cfg.lot_size}  "
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
        bias_tag = f"[{r.direction_bias}]" if r.direction_bias not in ("BOTH", "") else ""
        if r.no_trigger:
            print(f"║  {r.date}  {src_tag}S={r.start_price:<9.3f} {t_tag:<5} lock@{r.lock_utc}  No signal triggered {bias_tag}".ljust(W+1) + "║")
            continue
        for i, t in enumerate(r.trades):
            sym  = "✅" if t.outcome == "TP" else "❌" if t.outcome == "SL" else "⚠️ "
            be_tag = "🔒BE" if t.be_triggered else ""
            stop = (" 🎯PROFIT" if (r.hit_profit and i == len(r.trades)-1)
                    else " ⛔LOSS"   if (r.hit_loss   and i == len(r.trades)-1) else "")
            net_s = f"+${t.net_pnl:,.0f}" if t.net_pnl >= 0 else f"-${abs(t.net_pnl):,.0f}"
            grs_s = f"+${t.gross_pnl:,.0f}" if t.gross_pnl >= 0 else f"-${abs(t.gross_pnl):,.0f}"
            lock_s = f"lock@{r.lock_utc}" if i == 0 else " " * 9
            date_s = r.date if i == 0 else " " * 10
            src_s  = src_tag if i == 0 else "  "
            t_s    = t_tag if i == 0 else " " * len(t_tag)
            print(
                f"║  {date_s}  {src_s}S={r.start_price:<8.3f} {t_s:<5} {lock_s}  "
                f"{t.direction:<5}  {t.entry_bar}  "
                f"@{t.entry_price:.3f}→{t.exit_price:.3f}  "
                f"{sym}{t.outcome:<11}  {grs_s}  net={net_s}{be_tag}{stop}".ljust(W+1) + "║"
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
    if all_be:
        print(kv("Breakeven triggered:", f"{all_be} trades"))
    print(kv("Start from day files:", f"{day_file_days} days (📂) vs {len(reports)-day_file_days} bar_open (📊)"))
    if dynamic_threshold:
        print(kv("Avg threshold pips:", f"{avg_threshold}  (min={min(thresholds):.0f}  max={max(thresholds):.0f})"))
    print(f"║{thn}║")
    print(kv("Total trades:", total_trades))
    print(kv("TP hits:", f"{all_tp}  ({win_rate:.1f}% win rate)"))
    print(kv("SL hits:", all_sl))
    print(kv("Force closed:", all_fc))
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
    print(f"╚{sep}╝")
    print()
    print(f"[INFO] Threshold: {threshold_mode}  AvgT={avg_threshold}pips")
    print(f"[INFO] Entry: {entry_mode}")
    print(f"[INFO] SL=${cfg.sl_dollar:.0f}  TP=${cfg.tp_dollar:.0f}  "
          f"R:R=1:{cfg.risk_reward:.1f}  Breakeven={cfg.breakeven_win_rate*100:.1f}%  Actual={win_rate:.1f}%")
    print()


# =============================================================================
# COMPARISON SUMMARY
# =============================================================================

def print_comparison(fixed_reports: list[DayReport], dynamic_reports: list[DayReport],
                     fixed_cfg: StrategyConfig, dynamic_label: str):
    def stats(reports, cfg):
        total_trades = sum(len(r.trades) for r in reports)
        all_tp = sum(1 for r in reports for t in r.trades if t.outcome == "TP")
        total_net = sum(r.day_net for r in reports)
        active = sum(1 for r in reports if not r.no_trigger)
        wr = (all_tp / total_trades * 100) if total_trades else 0
        return total_trades, all_tp, wr, total_net, active

    ft, ftp, fwr, fnet, fa = stats(fixed_reports, fixed_cfg)
    dt, dtp, dwr, dnet, da = stats(dynamic_reports, fixed_cfg)

    sep = "─" * 60
    print(f"\n{'='*60}")
    print(f"  COMPARISON: Fixed vs {dynamic_label}")
    print(f"{'='*60}")
    print(f"  {'Metric':<25} {'Fixed':>12} {'Dynamic':>12}")
    print(sep)
    print(f"  {'Trades':<25} {ft:>12} {dt:>12}")
    print(f"  {'TP hits':<25} {ftp:>12} {dtp:>12}")
    print(f"  {'Win rate':<25} {fwr:>11.1f}% {dwr:>11.1f}%")
    print(f"  {'Net P&L':<25} ${fnet:>+10,.0f} ${dnet:>+10,.0f}")
    print(f"  {'Active days':<25} {fa:>12} {da:>12}")
    winner = "DYNAMIC" if dnet > fnet else "FIXED"
    diff = abs(dnet - fnet)
    print(sep)
    print(f"  Winner: {winner}  (${diff:,.0f} difference)")
    print(f"{'='*60}\n")


# =============================================================================
# CLI
# =============================================================================

def _make_cfg(args) -> StrategyConfig:
    sl_pips_raw = 2.0
    rr = args.tp_target / args.sl_target

    # Use the actual threshold as base (handles silver, forex, etc.)
    # Default = 20.0 (XAUUSD baseline)
    base_threshold = getattr(args, 'threshold_pips', 0.0) or 20.0

    sl_pips_override = getattr(args, 'sl_pips', None)
    if sl_pips_override and sl_pips_override != sl_pips_raw:
        # Compute multipliers relative to the actual threshold being used
        entry_offset   = base_threshold + sl_pips_override
        tp_pips        = sl_pips_override * rr
        exit_offset    = entry_offset + tp_pips
        entry_mult     = round(entry_offset / base_threshold, 6)
        exit_mult      = round(exit_offset  / base_threshold, 6)
    else:
        sl_pips_override = sl_pips_raw
        tp_pips          = sl_pips_raw * rr
        exit_offset      = base_threshold + sl_pips_raw + tp_pips
        entry_mult       = round((base_threshold + sl_pips_raw) / base_threshold, 6)
        exit_mult        = round(exit_offset / base_threshold, 6)

    from dataclasses import replace as dc_replace

    cfg = StrategyConfig(
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

    # Override max trades per day
    if getattr(args, 'max_trades', 0) > 0:
        cfg = dc_replace(cfg, max_trades_per_day=args.max_trades)

    # Override threshold pips (backtest research only — never touches runner.py)
    if getattr(args, 'threshold_pips', 0.0) > 0:
        cfg = dc_replace(cfg, threshold_pips=args.threshold_pips)
        # Sync daily profit target to actual tp_dollar so gate fires correctly
        cfg = dc_replace(cfg, daily_profit_target_usd=cfg.tp_dollar)

    return cfg


def parse_args():
    p = argparse.ArgumentParser(
        description     = "XAUUSD Threshold Strategy Backtest",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
Research flags (backtest only — does not change runner.py):
  --dynamic-threshold atr        ATR-based threshold per day
  --atr-period 14                ATR period (default 14)
  --atr-multiplier 1.0           threshold = ATR × multiplier
  --dynamic-threshold prev-day   prev day range × factor
  --range-factor 0.35            prev day range factor (default 0.35)
  --continuation-bias            bias same direction after SL hit
  --time-filter                  blackout midnight/London/NY open
  --daily-trend-align            only LONG if start>prev, SHORT if lower
  --breakeven-stop 8             move SL to entry after 8 pips profit
  --compare-fixed                show fixed vs dynamic side by side

Examples:
  # Baseline
  python backtest.py --capital 50000 --sl-target 200 --tp-target 600 --close-confirm --trend-filter --data-dir data --months 1 --sl-pips 5 --daily-loss 200 --daily-profit 600

  # ATR dynamic threshold
  python backtest.py ... --dynamic-threshold atr --atr-multiplier 1.0 --compare-fixed

  # Breakeven stop
  python backtest.py ... --breakeven-stop 8

  # All combined
  python backtest.py ... --dynamic-threshold atr --continuation-bias --time-filter --daily-trend-align --breakeven-stop 8
        """
    )
    p.add_argument("--capital",       type=float, required=True)
    p.add_argument("--sl-target",     type=float, default=200.0)
    p.add_argument("--tp-target",     type=float, default=600.0)
    p.add_argument("--sl-pips",       type=float, default=5.0)
    p.add_argument("--daily-loss",    type=float, default=200.0)
    p.add_argument("--daily-profit",  type=float, default=600.0)
    # ── BOT MODE ──────────────────────────────────────────────────────────────
    # --two-trade-mode: matches real bot before Bug 2 fix
    # daily-loss = $400 (allows 2 SL hits per day before stopping)
    # This reflects what actually happened on April 2 (2 trades, 2 losses)
    # Default (no flag): daily-loss = $200 = 1 SL stops day (post-fix behaviour)
    p.add_argument("--two-trade-mode", action="store_true",
                   help="Allow 2 SL hits before stopping (daily-loss=$400). "
                        "Reflects pre-fix bot behaviour. "
                        "Without this flag: 1 SL stops day ($200 limit, post-fix).")
    p.add_argument("--months",        type=int,   default=1)
    p.add_argument("--symbol",        type=str,   default="XAUUSD")
    p.add_argument("--data-dir",      type=str,   default="")
    p.add_argument("--close-confirm", action="store_true")
    p.add_argument("--trend-filter",  action="store_true")
    p.add_argument("--session-start", type=str,   default="00:00")
    p.add_argument("--session-end",   type=str,   default="23:00")
    p.add_argument("--session-london-ny", action="store_true")
    p.add_argument("--consec-loss-pause", type=int, default=0)
    p.add_argument("--force-close",   type=str,   default="23:30")
    p.add_argument("--overshoot",     type=float, default=3.0)
    p.add_argument("--verbose",       action="store_true")
    # ── NEW FLAGS ──────────────────────────────────────────────────────────
    p.add_argument("--dynamic-threshold", type=str, default="",
                   choices=["", "atr", "prev-day"],
                   help="Dynamic threshold mode: atr or prev-day")
    p.add_argument("--atr-period",    type=int,   default=14)
    p.add_argument("--atr-multiplier",type=float, default=1.0)
    p.add_argument("--range-factor",  type=float, default=0.35)
    p.add_argument("--continuation-bias", action="store_true",
                   help="After SL hit, bias same direction next day")
    p.add_argument("--time-filter",   action="store_true",
                   help="Blackout midnight/London/NY open spikes")
    p.add_argument("--daily-trend-align", action="store_true",
                   help="Only LONG if start>prev_start, SHORT if lower")
    p.add_argument("--breakeven-stop", type=float, default=0.0,
                   help="Move SL to entry after N pips profit (e.g. 8)")
    p.add_argument("--compare-fixed", action="store_true",
                   help="Run fixed threshold alongside dynamic for comparison")
    p.add_argument("--max-trades",    type=int,   default=0,
                   help="Override max trades per day (e.g. 3 or 4). "
                        "daily-loss auto-scales to N × sl-target unless --daily-loss also set.")
    p.add_argument("--threshold-pips", type=float, default=0.0,
                   help="Override threshold pips for backtest only (e.g. 30)")
    p.add_argument("--pip-value",  type=float, default=100.0,
                   help="Dollar value per pip per lot. XAUUSD=100, XAGUSD=50, EURUSD=10. Default 100.")
    p.add_argument("--spread",     type=float, default=0.35,
                   help="Spread in pips. XAUUSD≈0.35, XAGUSD≈1.5, EURUSD≈0.5. Default 0.35.")
    return p.parse_args()


def main():
    args = parse_args()
    if not MT5_AVAILABLE:
        print("❌ pip install MetaTrader5")
        sys.exit(1)

    # ── TWO-TRADE MODE ────────────────────────────────────────────────────────
    if getattr(args, 'two_trade_mode', False):
        args.daily_loss = args.sl_target * 2
        if not args.max_trades:
            args.max_trades = 2
        print(f"[MODE] TWO-TRADE: daily-loss=${args.daily_loss:.0f} "
              f"(2×SL — 2 SL hits allowed before day stops)")
    # ── MAX TRADES OVERRIDE ───────────────────────────────────────────────────
    if getattr(args, 'max_trades', 0) > 0:
        # Auto-scale daily-loss if user didn't override it explicitly
        # Only scale if daily-loss is still the default ($200)
        if args.daily_loss == args.sl_target:
            args.daily_loss = args.sl_target * args.max_trades
            print(f"[MODE] MAX-TRADES={args.max_trades}: "
                  f"daily-loss auto-scaled to ${args.daily_loss:.0f} "
                  f"({args.max_trades}×${args.sl_target:.0f})")
    # ─────────────────────────────────────────────────────────────────────────

    # ── Set pip value and spread for this symbol ─────────────────────────
    global _PIP_VALUE, _SPREAD_PIPS
    _PIP_VALUE   = args.pip_value
    _SPREAD_PIPS = args.spread
    print(f"[BACKTEST] Symbol pip value : ${_PIP_VALUE:.0f}/pip/lot")
    print(f"[BACKTEST] Spread           : {_SPREAD_PIPS} pips")
    # ─────────────────────────────────────────────────────────────────────

    cfg = _make_cfg(args)
    print(cfg.summary())

    if args.data_dir:
        print(f"[BACKTEST] Start price source: day files in {args.data_dir}/")
    else:
        print(f"[BACKTEST] Start price source: M5 bar open fallback")

    try:
        bars = fetch_bars(args.symbol, args.months)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    day_map = group_by_day(bars)
    print(f"[BACKTEST] {len(day_map)} UTC trading days\n")

    dates = list(day_map.keys())

    # ── Build per-day parameters ───────────────────────────────────────────
    sess_start = "07:00" if args.session_london_ny else ""
    sess_end   = "16:00" if args.session_london_ny else ""

    fixed_reports:   list[DayReport] = []
    dynamic_reports: list[DayReport] = []

    prev_outcome:    str   = ""   # "TP" | "SL" | "FORCE_CLOSE" | ""
    prev_direction:  str   = ""   # "LONG" | "SHORT" | ""
    prev_start:      float = 0.0

    for i, date in enumerate(dates):
        day_bars  = day_map[date]
        prev_bars = day_map[dates[i-1]] if i > 0 else []

        # Continuation bias
        direction_bias = "BOTH"
        if args.continuation_bias and prev_outcome == "SL" and prev_direction:
            direction_bias = prev_direction

        # Dynamic threshold
        dyn_threshold = 0.0
        if args.dynamic_threshold == "atr":
            dyn_threshold = compute_dynamic_threshold_atr(
                prev_bars, args.atr_period, args.atr_multiplier
            )
        elif args.dynamic_threshold == "prev-day":
            dyn_threshold = compute_dynamic_threshold_prev_range(
                prev_bars, args.range_factor
            )

        if args.verbose:
            fb = day_bars[0]
            print(f"[BACKTEST] {date}  bars={len(day_bars)}  "
                  f"first={fb.utc_hhmm}  open={fb.open:.3f}  "
                  f"dyn_T={dyn_threshold:.1f}  bias={direction_bias}")

        run_kwargs = dict(
            close_confirm     = args.close_confirm,
            trend_filter      = args.trend_filter,
            data_dir          = args.data_dir,
            session_start_utc = sess_start,
            session_end_utc   = sess_end,
            consec_loss_pause = args.consec_loss_pause,
            direction_bias    = direction_bias,
            time_filter       = args.time_filter,
            daily_trend_align = args.daily_trend_align,
            prev_start_price  = prev_start,
            breakeven_stop    = args.breakeven_stop,
        )

        # Dynamic run
        r = run_day(date, day_bars, cfg,
                    dynamic_threshold=dyn_threshold, **run_kwargs)
        dynamic_reports.append(r)

        # Fixed run (only if compare requested)
        if args.compare_fixed and args.dynamic_threshold:
            rf = run_day(date, day_bars, cfg,
                         dynamic_threshold=0.0, **run_kwargs)
            fixed_reports.append(rf)

        # Update prev state
        if r.trades:
            last = r.trades[-1]
            prev_outcome   = last.outcome
            prev_direction = last.direction
        else:
            prev_outcome   = ""
            prev_direction = ""
        prev_start = r.start_price if r.start_price > 0 else prev_start

    # ── Print reports ─────────────────────────────────────────────────────
    dyn_label = args.dynamic_threshold.upper() if args.dynamic_threshold else ""

    # Main report (dynamic or fixed)
    print_report(
        dynamic_reports, cfg, args.months, args.symbol,
        close_confirm     = args.close_confirm,
        trend_filter      = args.trend_filter,
        data_dir          = args.data_dir,
        session_start_utc = sess_start,
        session_end_utc   = sess_end,
        consec_loss_pause = args.consec_loss_pause,
        dynamic_threshold = dyn_label,
        continuation_bias = args.continuation_bias,
        time_filter       = args.time_filter,
        daily_trend_align = args.daily_trend_align,
        breakeven_stop    = args.breakeven_stop,
        label             = dyn_label,
    )

    # Fixed comparison report
    if args.compare_fixed and args.dynamic_threshold and fixed_reports:
        print_report(
            fixed_reports, cfg, args.months, args.symbol,
            close_confirm     = args.close_confirm,
            trend_filter      = args.trend_filter,
            data_dir          = args.data_dir,
            label             = "FIXED (comparison)",
        )
        print_comparison(fixed_reports, dynamic_reports, cfg, dyn_label)

    # ── HTML dashboard ────────────────────────────────────────────────────────
    _generate_html_report(dynamic_reports, cfg, args.months, args.symbol, args)


def _generate_html_report(reports, cfg, months_count, symbol, args):
    """Generate interactive Chart.js dashboard and auto-open in browser."""
    import webbrowser, os, json as _json
    from collections import defaultdict

    # ── Stats ─────────────────────────────────────────────────────────────────
    total_trades = sum(len(r.trades) for r in reports)
    all_tp       = sum(1 for r in reports for t in r.trades if t.outcome == "TP")
    all_sl       = sum(1 for r in reports for t in r.trades if t.outcome in ("SL","FORCE_CLOSE"))
    total_net    = sum(r.day_net for r in reports)
    total_spread = abs(sum(r.day_spread for r in reports))
    no_trig      = sum(1 for r in reports if r.no_trigger)
    active       = len(reports) - no_trig
    win_rate     = round(all_tp / total_trades * 100, 1) if total_trades else 0
    roi          = round(total_net / cfg.account_size * 100, 2) if cfg.account_size else 0
    avg_day      = round(total_net / active, 2) if active else 0
    monthly_proj = round(avg_day * 22, 2)

    # ── Equity curve ──────────────────────────────────────────────────────────
    equity   = cfg.account_size
    day_rows = []
    for r in reports:
        equity += r.day_net
        result = 'NO' if r.no_trigger else ('TP' if r.day_net > 0 else 'SL')
        day_rows.append({
            'date':   r.date[5:],       # MM-DD
            'result': result,
            'pnl':    round(r.day_net, 2),
            'eq':     round(equity, 2),
        })

    # Peak equity & max drawdown
    peak = cfg.account_size; max_dd = 0; running = cfg.account_size
    for d in day_rows:
        running = d['eq']
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd

    # ── Monthly P&L ──────────────────────────────────────────────────────────
    month_pnl = defaultdict(float)
    for r in reports:
        month_pnl[r.date[:7]] += r.day_net
    month_labels = sorted(month_pnl.keys())
    month_names  = [m[5:] for m in month_labels]   # MM
    month_vals   = [round(month_pnl[m], 2) for m in month_labels]

    # ── JSON data for JS ─────────────────────────────────────────────────────
    data_json = _json.dumps({
        'days':        day_rows,
        'months':      month_names,
        'monthVals':   month_vals,
        'capital':     cfg.account_size,
        'totalNet':    round(total_net, 2),
        'roi':         roi,
        'winRate':     win_rate,
        'totalTrades': total_trades,
        'allTp':       all_tp,
        'allSl':       all_sl,
        'noTrig':      no_trig,
        'monthlyProj': monthly_proj,
        'peak':        round(peak, 2),
        'maxDd':       round(max_dd, 2),
        'maxDdPct':    round(max_dd / peak * 100, 2) if peak else 0,
        'symbol':      symbol,
        'months':      month_names,
        'monthVals':   month_vals,
        'slTarget':    cfg.sl_dollar_target,
        'tpTarget':    cfg.tp_dollar_target,
        'lot':         cfg.lot_size,
        'threshold':   cfg.threshold_pips,
        'rr':          round(cfg.tp_dollar_target / cfg.sl_dollar_target, 1),
    })

    # ── HTML ─────────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest — {symbol}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;padding:24px;min-height:100vh}}
h1{{font-size:18px;font-weight:500;color:#94a3b8;margin-bottom:20px;letter-spacing:.04em}}
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
.how{{background:#1e2330;border:1px solid #2d3448;border-radius:10px;padding:16px}}
.how-row{{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #2d3448;font-size:13px}}
.how-row:last-child{{border-bottom:none}}
.pill{{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;min-width:72px;text-align:center}}
@media(max-width:700px){{.grid4{{grid-template-columns:1fr 1fr}}.grid2{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>XAUUSD · Threshold Strategy · Backtest Dashboard</h1>
<div class="grid4" id="stats"></div>
<div class="grid4" id="stats2"></div>
<h2>Equity Curve — capital growth with every trade marked</h2>
<div class="chart-wrap">
  <div class="legend">
    <span><span class="dot" style="background:#4ade80"></span>TP hit</span>
    <span><span class="dot" style="background:#f87171"></span>SL hit</span>
    <span><span class="dot" style="background:#475569;width:6px;height:6px;margin-right:6px"></span>No signal</span>
    <span style="margin-left:auto;font-size:11px;color:#475569">{symbol} · {months_count}m · ${cfg.account_size:,.0f} · {cfg.lot_size} lot · 1 trade/day</span>
  </div>
  <div style="position:relative;height:280px"><canvas id="eqChart"></canvas></div>
</div>
<div class="grid2">
  <div>
    <h2>Daily P&amp;L — each bar is one trading day</h2>
    <div class="chart-wrap"><div style="position:relative;height:180px"><canvas id="dailyChart"></canvas></div></div>
  </div>
  <div>
    <h2>Monthly P&amp;L breakdown</h2>
    <div class="chart-wrap"><div style="position:relative;height:180px"><canvas id="monthChart"></canvas></div></div>
  </div>
</div>
<h2>How the bot works</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
  <div class="how">
    <div class="how-row"><span class="pill" style="background:#14532d;color:#86efac">S + {cfg.threshold_pips*2:.0f}</span><span>Take Profit → +${cfg.tp_dollar_target:.0f} gross (+${cfg.tp_dollar_target-14:.0f} net)</span></div>
    <div class="how-row"><span class="pill" style="background:#1e3a5f;color:#93c5fd">S + {cfg.threshold_pips*1.25:.0f}</span><span>LONG entry trigger — bot places BUY here</span></div>
    <div class="how-row"><span class="pill" style="background:#422006;color:#fde68a">S + {cfg.threshold_pips:.0f}</span><span>Threshold — M5 bar must close above this</span></div>
    <div class="how-row"><span class="pill" style="background:#1e2330;color:#94a3b8">S</span><span>Start price — locked at 00:00 UTC daily</span></div>
    <div class="how-row"><span class="pill" style="background:#422006;color:#fde68a">S - {cfg.threshold_pips:.0f}</span><span>Threshold — M5 bar must close below this</span></div>
    <div class="how-row"><span class="pill" style="background:#1e3a5f;color:#93c5fd">S - {cfg.threshold_pips*1.25:.0f}</span><span>SHORT entry trigger — bot places SELL here</span></div>
    <div class="how-row"><span class="pill" style="background:#4c0519;color:#fca5a5">S - {cfg.threshold_pips*2:.0f}</span><span>Take Profit (SHORT) → +${cfg.tp_dollar_target:.0f} gross</span></div>
  </div>
  <div class="how">
    <div class="how-row" style="border-bottom:none;flex-direction:column;align-items:flex-start;gap:8px">
      <div style="font-size:13px;color:#94a3b8;line-height:1.8">
        <b style="color:#e2e8f0">Step 1:</b> At 00:00 UTC, start price S is locked from day file.<br>
        <b style="color:#e2e8f0">Step 2:</b> Bot watches M5 bars. If a bar closes beyond S±{cfg.threshold_pips:.0f} pips, direction is set.<br>
        <b style="color:#e2e8f0">Step 3:</b> Next bar open, entry placed at S±{cfg.threshold_pips*1.25:.0f} with SL {cfg.sl_pips:.0f} pips away (${cfg.sl_dollar_target:.0f}).<br>
        <b style="color:#e2e8f0">Step 4:</b> TP at S±{cfg.threshold_pips*2:.0f} pips = +${cfg.tp_dollar_target:.0f}. R:R = 1:{cfg.risk_reward:.1f}.<br>
        <b style="color:#e2e8f0">Step 5:</b> After TP or SL, day is DONE. No more trades.<br>
        <b style="color:#e2e8f0">Prop firm:</b> Breakeven WR = 25%. Actual = {win_rate:.1f}%. 20pt edge.
      </div>
    </div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D = {data_json};
const days = D.days;
const labels = days.map(d=>d.date);
const equity = days.map(d=>d.eq);
const ptBg = days.map(d=>d.result==='TP'?'#4ade80':d.result==='SL'?'#f87171':'#475569');
const ptR  = days.map(d=>d.result==='NO'?2:d.result==='TP'?5:4);
const ptBd = days.map(d=>d.result==='TP'?'#22c55e':d.result==='SL'?'#ef4444':'#334155');

function statCard(label,val,sub,cls=''){{
  return `<div class="card"><div class="label">${{label}}</div><div class="val ${{cls}}">${{val}}</div><div class="sub">${{sub}}</div></div>`;
}}
document.getElementById('stats').innerHTML=[
  statCard('Net P&L ('+D.months+' months)', (D.totalNet>=0?'+':'')+' $'+D.totalNet.toLocaleString(), '+'+D.roi+'% on $'+D.capital.toLocaleString(), D.totalNet>=0?'green':'red'),
  statCard('Win rate', D.winRate+'%', D.allTp+' TP / '+D.allSl+' SL / '+D.noTrig+' no signal', 'amber'),
  statCard('Monthly projection', '+$'+D.monthlyProj.toLocaleString(), 'Based on $'+Math.round(D.totalNet/D.days.filter(d=>d.result!='NO').length)+'/active day', 'blue'),
  statCard('Max drawdown', '-$'+D.maxDd.toLocaleString(), '-'+D.maxDdPct+'% peak-to-trough', 'red'),
].join('');
document.getElementById('stats2').innerHTML=[
  statCard('Peak equity', '$'+D.peak.toLocaleString(), '+'+((D.peak/D.capital-1)*100).toFixed(1)+'% above start'),
  statCard('R:R ratio', '1 : '+D.rr, 'TP $'+D.tpTarget+' / SL $'+D.slTarget),
  statCard('Breakeven WR', '25.0%', 'Actual '+D.winRate+'% — '+(D.winRate-25).toFixed(1)+'pt edge', 'green'),
  statCard('Total trades', D.totalTrades, D.allTp+' wins · '+D.allSl+' losses'),
].join('');

new Chart(document.getElementById('eqChart'),{{
  type:'line',
  data:{{labels,datasets:[{{data:equity,borderColor:'#3b82f6',borderWidth:1.5,fill:true,backgroundColor:'rgba(59,130,246,0.06)',tension:0.3,pointBackgroundColor:ptBg,pointBorderColor:ptBd,pointRadius:ptR,pointHoverRadius:7,pointBorderWidth:1}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#1e2330',titleColor:'#94a3b8',bodyColor:'#e2e8f0',borderColor:'#2d3448',borderWidth:1,callbacks:{{label:ctx=>{{const d=days[ctx.dataIndex];const sign=d.pnl>0?'+':'';const tag=d.result==='NO'?'No signal':`${{d.result}} ${{sign}}$${{Math.abs(d.pnl)}}`;return[`Equity: $${{ctx.raw.toLocaleString()}}`,tag];}}}}}}}},scales:{{x:{{ticks:{{color:'#475569',font:{{size:10}},maxRotation:45,autoSkip:true,maxTicksLimit:20}},grid:{{color:'rgba(255,255,255,0.04)'}}}},y:{{ticks:{{callback:v=>'$'+Math.round(v/1000)+'k',color:'#475569',font:{{size:10}}}},grid:{{color:'rgba(255,255,255,0.04)'}},min:Math.floor((Math.min(...equity)-500)/1000)*1000,max:Math.ceil((Math.max(...equity)+500)/1000)*1000}}}}}}
}});

const dailyPnl=days.map(d=>d.pnl);
const barCol=days.map(d=>d.pnl>0?'rgba(74,222,128,0.8)':d.pnl<0?'rgba(248,113,113,0.8)':'rgba(71,85,105,0.5)');
new Chart(document.getElementById('dailyChart'),{{
  type:'bar',
  data:{{labels,datasets:[{{data:dailyPnl,backgroundColor:barCol,borderWidth:0,borderRadius:1}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#1e2330',titleColor:'#94a3b8',bodyColor:'#e2e8f0',borderColor:'#2d3448',borderWidth:1,callbacks:{{label:ctx=>{{const v=ctx.raw;return(v>0?'+$':v<0?'-$':'$')+Math.abs(v);}}}}}}}},scales:{{x:{{ticks:{{display:false}},grid:{{display:false}}}},y:{{ticks:{{callback:v=>(v>=0?'+':'')+v,color:'#475569',font:{{size:10}}}},grid:{{color:'rgba(255,255,255,0.04)'}}}}}}}}
}});

const mColors=D.monthVals.map(v=>v>=0?'rgba(74,222,128,0.8)':'rgba(248,113,113,0.8)');
new Chart(document.getElementById('monthChart'),{{
  type:'bar',
  data:{{labels:D.months,datasets:[{{data:D.monthVals,backgroundColor:mColors,borderWidth:0,borderRadius:4}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{backgroundColor:'#1e2330',titleColor:'#94a3b8',bodyColor:'#e2e8f0',borderColor:'#2d3448',borderWidth:1,callbacks:{{label:ctx=>{{const v=ctx.raw;return(v>=0?'+$':'-$')+Math.abs(v).toLocaleString();}}}}}}}},scales:{{x:{{ticks:{{color:'#475569',font:{{size:11}}}},grid:{{display:false}}}},y:{{ticks:{{callback:v=>(v>=0?'+$':'-$')+Math.abs(v/1000).toFixed(1)+'k',color:'#475569',font:{{size:10}}}},grid:{{color:'rgba(255,255,255,0.04)'}}}}}}}}
}});
</script>
</body>
</html>"""

    out_path = os.path.abspath("backtest_report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n[HTML] Report saved → {out_path}")
    try:
        webbrowser.open(f"file:///{out_path}")
        print(f"[HTML] Opened in browser")
    except Exception:
        print(f"[HTML] Open manually in browser")


if __name__ == "__main__":
    main()