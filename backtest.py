from __future__ import annotations

# =============================================================================
# BACKTEST — fetch real MT5 M5 data, replay strategy logic bar by bar
#
# TIMING (confirmed from 2026-04-01.json + pricing/settings.py):
#   - MT5 UI runs UTC  →  date_mt5 = UTC date  →  day boundary = UTC midnight
#   - lock_hhmm_mt5 = "00:00" UTC  →  first bar of UTC day
#   - Server UTC+03 is display only — NOT used for timing
#
# DOLLAR TARGETS (key CLI args):
#   --sl-target 100    SL = $100 per trade  → derives lot size
#   --tp-target 150    TP = $150 per trade  → derives exit pip level
#   --daily-loss 100   Stop after $100 daily loss (= 1 SL hit)
#   --daily-profit 150 Stop after $150 daily profit (= 1 TP hit)
#
# Usage:
#   python backtest.py --capital 50000 --sl-target 100 --tp-target 150
#   python backtest.py --capital 50000 --sl-target 100 --tp-target 200
#   python backtest.py --capital 50000 --sl-target 100 --tp-target 150 --months 2
# =============================================================================

import argparse
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


@dataclass
class DayReport:
    date:        str
    start_price: float
    lock_utc:    str = ""
    trades:      list[TradeRecord] = field(default_factory=list)
    day_gross:   float = 0.0
    day_net:     float = 0.0
    day_spread:  float = 0.0
    hit_profit:  bool  = False
    hit_loss:    bool  = False
    no_trigger:  bool  = False


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
    """Group by UTC date — matches date_mt5 in real bot JSON."""
    day_map: dict[str, list[Bar]] = defaultdict(list)
    for bar in bars:
        key = bar.time_utc.strftime("%Y-%m-%d")
        day_map[key].append(bar)
    return dict(sorted(day_map.items()))


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
    Conflict resolution: entry_on_bar=True uses close as tiebreaker.
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


def _check_entry_on_bar(
    direction: Direction, entry: float, bar: Bar, overshoot: float,
) -> bool:
    """
    TICK-TOUCH entry (original, Mode A):
    Bar high/low just needs to cross entry level.
    Fires on the crossing bar itself — vulnerable to same-bar SL.
    """
    if direction == "LONG":
        return bar.high >= entry and (bar.close - entry) <= overshoot
    else:
        return bar.low <= entry and (entry - bar.close) <= overshoot


def _check_entry_close_confirm(
    direction: Direction, entry: float, bar: Bar,
) -> bool:
    """
    CLOSE-CONFIRMATION entry (Mode B — --close-confirm):
    Bar must CLOSE at or beyond the entry level.
    Order executes at NEXT bar open, eliminating same-bar SL hits.
    This is a real breakout confirmation, not a tick touch.

    LONG:  bar.close >= entry  (closed above trigger = real breakout)
    SHORT: bar.close <= entry  (closed below trigger = real breakdown)
    """
    if direction == "LONG":
        return bar.close >= entry
    else:
        return bar.close <= entry


def _trend_filter_ok(direction: Direction, bar: Bar, start_price: float) -> bool:
    """
    TREND FILTER (--trend-filter):
    Only take LONG trades if price is above start price (uptrend bias).
    Only take SHORT trades if price is below start price (downtrend bias).
    Uses bar open as proxy for current price direction.
    """
    if direction == "LONG":
        return bar.open >= start_price
    else:
        return bar.open <= start_price


# =============================================================================
# DAY SIMULATION
# =============================================================================

def run_day(
    date:         str,
    bars:         list[Bar],
    cfg:          StrategyConfig,
    close_confirm: bool = False,   # True = close-confirmation entry (next-bar open)
    trend_filter:  bool = False,   # True = only trade in direction of start→current
) -> DayReport:
    """
    Replay one UTC calendar day bar by bar.

    close_confirm=True (recommended):
        Entry fires when bar CLOSES past the entry level.
        Order is placed at next bar open.
        Eliminates same-bar SL hits caused by 2-pip SL being smaller than M5 bar range.

    trend_filter=True (optional additive):
        Only take LONG if bar.open > start_price (price is above day start).
        Only take SHORT if bar.open < start_price.
        Reduces false breakouts against the prevailing intraday direction.
    """
    report          = DayReport(date=date, start_price=0.0)
    already_traded: set[Direction] = set()
    trade_count     = 0
    realized_pnl    = 0.0
    open_trade: TradeRecord | None = None

    # Pending close-confirm signal: (direction, entry, tp, sl, signal_bar_hhmm)
    # Executed at next bar's open
    pending_entry: tuple | None = None

    # Lock start price at first bar >= session_start (UTC)
    start_bar = next((b for b in bars if b.utc_hhmm >= cfg.session_start_hhmm), None)
    if start_bar is None:
        report.no_trigger = True
        return report

    start_price        = start_bar.open
    report.start_price = start_price
    report.lock_utc    = start_bar.utc_hhmm
    levels             = compute_levels(start_price, cfg)

    bars_list = list(bars)

    for i, bar in enumerate(bars_list):
        if bar.utc_hhmm < cfg.session_start_hhmm:
            continue

        # Force close
        if bar.utc_hhmm >= cfg.force_close_hhmm:
            # Cancel pending close-confirm entry at force-close time
            pending_entry = None
            if open_trade is not None:
                ep    = bar.open
                gross = _pnl(open_trade.direction, open_trade.entry_price, ep, cfg.lot_size)
                sp    = _spread_cost(cfg.lot_size)
                realized_pnl          += gross
                open_trade.exit_price  = ep
                open_trade.exit_bar    = bar.utc_hhmm
                open_trade.outcome     = "FORCE_CLOSE"
                open_trade.gross_pnl   = gross
                open_trade.spread_cost = sp
                open_trade.net_pnl     = round(gross - sp, 2)
                open_trade = None
            break

        # ── CLOSE-CONFIRM: execute pending entry at this bar's open ─────────
        if close_confirm and pending_entry is not None:
            p_dir, p_entry, p_tp, p_sl, p_signal_bar = pending_entry
            pending_entry = None

            # Execute at this bar's open price
            exec_price = bar.open

            snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                trade_count=trade_count, open_position_count=0)
            allowed, _ = can_place_trade(snap, cfg)

            if allowed:
                trade = TradeRecord(
                    day=date, direction=p_dir,
                    entry_price=exec_price, tp_price=p_tp, sl_price=p_sl,
                    entry_bar=f"{p_signal_bar}→{bar.utc_hhmm}",
                )
                report.trades.append(trade)
                already_traded.add(p_dir)
                trade_count += 1
                open_trade = trade
                # No same-bar check here — entry is at bar open, SL check runs next iteration

        # Step 1: resolve open position on this bar
        if open_trade is not None:
            result = _check_exit_on_bar(
                open_trade.direction, open_trade.entry_price,
                open_trade.tp_price, open_trade.sl_price,
                bar, entry_on_bar=False,
            )
            if result is not None:
                outcome, exit_price = result
                gross = _pnl(open_trade.direction, open_trade.entry_price, exit_price, cfg.lot_size)
                sp    = _spread_cost(cfg.lot_size)
                realized_pnl          += gross
                open_trade.exit_price  = exit_price
                open_trade.exit_bar    = bar.utc_hhmm
                open_trade.outcome     = outcome
                open_trade.gross_pnl   = gross
                open_trade.spread_cost = sp
                open_trade.net_pnl     = round(gross - sp, 2)
                open_trade = None

        # Step 2: daily gates
        if is_daily_profit_hit(realized_pnl, cfg):
            report.hit_profit = True; break
        if is_daily_limit_breached(realized_pnl, cfg):
            report.hit_loss = True; break
        if trade_count >= cfg.max_trades_per_day:
            break
        if open_trade is not None or pending_entry is not None:
            continue

        # Step 3: entry signal
        mode = cfg.direction_mode
        if mode == "first_only" and already_traded:
            continue

        dirs: list[Direction] = []
        if "LONG"  not in already_traded: dirs.append("LONG")
        if "SHORT" not in already_traded: dirs.append("SHORT")

        for direction in dirs:
            entry = levels.long_entry  if direction == "LONG"  else levels.short_entry
            tp    = levels.long_tp     if direction == "LONG"  else levels.short_tp
            sl    = levels.long_sl     if direction == "LONG"  else levels.short_sl

            # Trend filter gate
            if trend_filter and not _trend_filter_ok(direction, bar, start_price):
                continue

            if close_confirm:
                # Close-confirmation: bar must CLOSE past entry level
                if not _check_entry_close_confirm(direction, entry, bar):
                    continue
                # Queue for next-bar execution
                pending_entry = (direction, entry, tp, sl, bar.utc_hhmm)
                break   # one pending at a time
            else:
                # Tick-touch: bar high/low crosses entry (original logic)
                if not _check_entry_on_bar(direction, entry, bar, cfg.max_entry_overshoot_pips):
                    continue

                snap = RiskSnapshot(realized_pnl=realized_pnl, open_pnl=0.0,
                                    trade_count=trade_count, open_position_count=0)
                allowed, _ = can_place_trade(snap, cfg)
                if not allowed:
                    continue

                trade = TradeRecord(day=date, direction=direction,
                                    entry_price=entry, tp_price=tp, sl_price=sl,
                                    entry_bar=bar.utc_hhmm)
                report.trades.append(trade)
                already_traded.add(direction)
                trade_count += 1

                # Step 4: same-bar exit (tick-touch mode only)
                result = _check_exit_on_bar(direction, entry, tp, sl, bar, entry_on_bar=True)
                if result is not None:
                    outcome, exit_price = result
                    gross = _pnl(direction, entry, exit_price, cfg.lot_size)
                    sp    = _spread_cost(cfg.lot_size)
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
        gross = _pnl(open_trade.direction, open_trade.entry_price, ep, cfg.lot_size)
        sp    = _spread_cost(cfg.lot_size)
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

W = 84

def print_report(reports: list[DayReport], cfg: StrategyConfig, months: int, symbol: str,
                 close_confirm: bool = False, trend_filter: bool = False):
    total_gross  = sum(r.day_gross  for r in reports)
    total_spread = sum(r.day_spread for r in reports)
    total_net    = sum(r.day_net    for r in reports)
    total_trades = sum(len(r.trades) for r in reports)

    all_tp = sum(1 for r in reports for t in r.trades if t.outcome == "TP")
    all_sl = sum(1 for r in reports for t in r.trades if t.outcome == "SL")
    all_fc = sum(1 for r in reports for t in r.trades if t.outcome == "FORCE_CLOSE")

    win_days      = [r for r in reports if r.day_net > 0]
    loss_days     = [r for r in reports if r.day_net < 0]
    no_trig_days  = [r for r in reports if r.no_trigger]
    profit_days   = [r for r in reports if r.hit_profit]
    loss_lim_days = [r for r in reports if r.hit_loss]

    win_rate    = (all_tp / total_trades * 100) if total_trades else 0
    roi         = (total_net / cfg.account_size * 100) if cfg.account_size else 0
    active_days = len(reports) - len(no_trig_days)
    avg_per_day = total_net / active_days if active_days else 0

    sep = "═" * W
    thn = "─" * W

    print(f"\n╔{sep}╗")
    print(f"║{'BACKTEST REPORT — XAUUSD THRESHOLD STRATEGY':^{W}}║")
    print(f"╠{sep}╣")
    print((f"║  {symbol}  {months}m  ${cfg.account_size:,.0f}  lot={cfg.lot_size}  "
           f"SL=${cfg.sl_dollar:.0f}/trade  TP=${cfg.tp_dollar:.0f}/trade  "
           f"R:R=1:{cfg.risk_reward:.1f}  "
           f"DailyLoss=-${cfg.max_daily_loss_usd:.0f}  DailyProfit=+${cfg.daily_profit_target_usd:.0f}").ljust(W+1) + "║")
    entry_mode = ("CLOSE-CONFIRM+NEXT-BAR" if close_confirm else "TICK-TOUCH⚠️ ")
    if trend_filter: entry_mode += "+TREND-FILTER"
    print(f"║  Entry mode: {entry_mode}".ljust(W+1) + "║")
    print(f"╠{sep}╣")
    print(f"║{'DAY-BY-DAY  (UTC — matches date_mt5 and lock_hhmm_mt5=00:00)':^{W}}║")
    print(f"╠{thn}╣")

    for r in reports:
        if r.no_trigger:
            lock = f"lock@{r.lock_utc}" if r.lock_utc else "no-bar"
            print(f"║  {r.date}  S={r.start_price:<9.3f}  {lock}  No signal triggered".ljust(W+1) + "║")
            continue
        for i, t in enumerate(r.trades):
            sym  = "✅" if t.outcome == "TP" else "❌" if t.outcome == "SL" else "⚠️ "
            stop = (" 🎯PROFIT" if (r.hit_profit and i == len(r.trades)-1)
                    else " ⛔LOSS"   if (r.hit_loss   and i == len(r.trades)-1) else "")
            net_s = f"+${t.net_pnl:,.0f}" if t.net_pnl >= 0 else f"-${abs(t.net_pnl):,.0f}"
            grs_s = f"+${t.gross_pnl:,.0f}" if t.gross_pnl >= 0 else f"-${abs(t.gross_pnl):,.0f}"
            lock_s = f"lock@{r.lock_utc}" if i == 0 else " " * 9
            print(
                f"║  {r.date if i==0 else ' '*10}  "
                f"S={r.start_price:<9.3f}  {lock_s}  "
                f"{t.direction:<5}  {t.entry_bar}→{t.exit_bar}  "
                f"@{t.entry_price:.3f}→{t.exit_price:.3f}  "
                f"{sym}{t.outcome:<11}  {grs_s}  net={net_s}{stop}".ljust(W+1) + "║"
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
    print(f"[INFO] SL=${cfg.sl_dollar:.0f}/trade  TP=${cfg.tp_dollar:.0f}/trade  "
          f"R:R=1:{cfg.risk_reward:.1f}  Breakeven WR={cfg.breakeven_win_rate*100:.1f}%")
    print(f"[INFO] DailyLoss cap=-${cfg.max_daily_loss_usd:.0f}  "
          f"DailyProfit stop=+${cfg.daily_profit_target_usd:.0f}")
    print(f"[INFO] Win rate needed to profit: >{cfg.breakeven_win_rate*100:.1f}%  "
          f"(actual: {win_rate:.1f}%)")
    print()


# =============================================================================
# CLI
# =============================================================================

def _make_cfg(args) -> StrategyConfig:
    """Build StrategyConfig entirely from CLI dollar targets — no lot arg needed."""
    # Derive exit_multiplier from tp_target and sl_target
    # tp_pips / sl_pips = tp_target / sl_target
    # tp_pips = (tp_target / sl_target) × sl_pips
    # sl_pips = entry_offset - breakout_offset = (1.1 - 1.0) × threshold = 2.0
    sl_pips = (1.1 - 1.0) * 20.0   # = 2.0
    tp_pips = (args.tp_target / args.sl_target) * sl_pips
    # exit_offset = entry_offset + tp_pips = 22 + tp_pips
    exit_offset = 22.0 + tp_pips
    exit_multiplier = round(exit_offset / 20.0, 4)

    cfg = StrategyConfig(
        account_size             = args.capital,
        symbol                   = args.symbol,
        sl_dollar_target         = args.sl_target,
        tp_dollar_target         = args.tp_target,
        exit_multiplier          = exit_multiplier,
        daily_profit_target_usd  = args.daily_profit,
        max_daily_loss_usd       = args.daily_loss,
        session_start_hhmm       = args.session_start,
        session_end_hhmm         = args.session_end,
        force_close_hhmm         = args.force_close,
        max_entry_overshoot_pips = args.overshoot,
        server_utc_offset_hours  = 0,
    )
    return cfg


def parse_args():
    p = argparse.ArgumentParser(
        description     = "XAUUSD Threshold Strategy Backtest",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
THE PROBLEM WITH DEFAULT (tick-touch) MODE:
  2-pip SL < typical M5 bar range on gold (5-20 pips).
  Entry fires on bar touch, same bar immediately hits SL.
  Result: 0% win rate regardless of lot/target size.

SOLUTION — use --close-confirm (strongly recommended):
  Bar must CLOSE past entry level → order at NEXT bar open.
  Same-bar SL eliminated. Win rate reflects real breakouts.

Examples:
  # Baseline (broken — same-bar SL)
  python backtest.py --capital 50000 --sl-target 100 --tp-target 200

  # Fixed: close confirmation only
  python backtest.py --capital 50000 --sl-target 100 --tp-target 200 --close-confirm

  # Fixed + trend filter (strongest signal quality)
  python backtest.py --capital 50000 --sl-target 100 --tp-target 200 --close-confirm --trend-filter

  # Compare all 3 modes:
  python backtest.py --capital 50000 --sl-target 100 --tp-target 150 --close-confirm
  python backtest.py --capital 50000 --sl-target 100 --tp-target 150 --close-confirm --trend-filter
        """
    )
    p.add_argument("--capital",       type=float, required=True,
                   help="Account size USD")
    p.add_argument("--sl-target",     type=float, default=100.0,
                   help="SL per trade in USD (default: 100)")
    p.add_argument("--tp-target",     type=float, default=150.0,
                   help="TP per trade in USD (default: 150)")
    p.add_argument("--daily-loss",    type=float, default=100.0,
                   help="Daily loss limit USD (default: 100 = 1 SL then stop)")
    p.add_argument("--daily-profit",  type=float, default=150.0,
                   help="Daily profit stop USD (default: 150 = 1 TP then stop)")
    p.add_argument("--months",        type=int,   default=1)
    p.add_argument("--symbol",        type=str,   default="XAUUSD")
    p.add_argument("--session-start", type=str,   default="00:00")
    p.add_argument("--session-end",   type=str,   default="23:00")
    p.add_argument("--force-close",   type=str,   default="23:30")
    p.add_argument("--close-confirm", action="store_true",
                   help="Entry only on bar CLOSE past level. Execute at next bar open. "
                        "Eliminates same-bar SL hits. RECOMMENDED.")
    p.add_argument("--trend-filter",  action="store_true",
                   help="Only LONG if price above start, only SHORT if below start. "
                        "Filters entries against intraday trend.")
    p.add_argument("--overshoot",     type=float, default=3.0)
    p.add_argument("--verbose",       action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    if not MT5_AVAILABLE:
        print("❌ pip install MetaTrader5")
        sys.exit(1)

    cfg = _make_cfg(args)
    print(cfg.summary())

    try:
        bars = fetch_bars(args.symbol, args.months)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    day_map = group_by_day(bars)
    print(f"[BACKTEST] {len(day_map)} UTC trading days\n")

    reports: list[DayReport] = []
    for date, day_bars in day_map.items():
        if args.verbose:
            fb = day_bars[0]
            print(f"[BACKTEST] {date}  bars={len(day_bars)}  "
                  f"first={fb.utc_hhmm}UTC  open={fb.open:.3f}")
        reports.append(run_day(
            date, day_bars, cfg,
            close_confirm = args.close_confirm,
            trend_filter  = args.trend_filter,
        ))

    print_report(reports, cfg, args.months, args.symbol,
                 close_confirm=args.close_confirm,
                 trend_filter=args.trend_filter)


if __name__ == "__main__":
    main()
