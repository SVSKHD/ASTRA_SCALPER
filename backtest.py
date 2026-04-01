from __future__ import annotations

# =============================================================================
# BACKTEST — fetch real MT5 M5 data, replay strategy logic bar by bar
#
# Usage:
#   python backtest.py --capital 50000 --lot 2.5
#   python backtest.py --capital 10000 --lot 0.5 --months 1
#   python backtest.py --capital 100000 --lot 5.0 --months 2 --symbol XAUUSD
#   python backtest.py --help
#
# Intrabar execution model (FIXED):
#   Each M5 bar is evaluated in this order:
#   1. If position open → check bar HIGH/LOW for SL or TP hit
#   2. If no position → check bar HIGH/LOW for entry cross
#   3. If entry fires on this bar → immediately check same bar for SL/TP
#      (price can enter AND exit within the same 5-min bar)
#   4. Overshoot filter: if bar closes too far past entry, reject stale signal
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
    time:  datetime
    open:  float
    high:  float
    low:   float
    close: float
    hhmm:  str = ""

    def __post_init__(self):
        self.hhmm = self.time.strftime("%H:%M")


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
    outcome:     str   = ""      # "TP" | "SL" | "FORCE_CLOSE"
    gross_pnl:   float = 0.0
    spread_cost: float = 0.0
    net_pnl:     float = 0.0


@dataclass
class DayReport:
    date:        str
    start_price: float
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

def fetch_bars(symbol: str, months: int, server_utc_offset: int = 2) -> list[Bar]:
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    to_dt   = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=months * 31)

    print(f"[BACKTEST] Fetching M5: {symbol} | {from_dt.date()} → {to_dt.date()}")
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, from_dt, to_dt)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No M5 data for {symbol}. Check MT5 connection.")

    bars = [
        Bar(
            time  = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            open  = float(r["open"]),
            high  = float(r["high"]),
            low   = float(r["low"]),
            close = float(r["close"]),
        )
        for r in rates
    ]
    print(f"[BACKTEST] Fetched {len(bars):,} bars")
    return bars


def group_by_day(bars: list[Bar], server_utc_offset: int = 2) -> dict[str, list[Bar]]:
    offset  = timedelta(hours=server_utc_offset)
    day_map: dict[str, list[Bar]] = defaultdict(list)
    for bar in bars:
        key = (bar.time + offset).strftime("%Y-%m-%d")
        day_map[key].append(bar)
    return dict(sorted(day_map.items()))


# =============================================================================
# INTRABAR EXECUTION ENGINE (FIXED)
# =============================================================================

SPREAD_PIP = 0.35   # typical XAUUSD spread


def _spread_cost(lot_size: float) -> float:
    return round(SPREAD_PIP * 100.0 * lot_size, 2)


def _pnl(direction: Direction, entry: float, exit_p: float, lot_size: float) -> float:
    ppl = lot_size * 100.0
    return round(
        (exit_p - entry) * ppl if direction == "LONG"
        else (entry - exit_p) * ppl,
        2
    )


def _check_exit_on_bar(
    direction:    Direction,
    entry:        float,
    tp:           float,
    sl:           float,
    bar:          Bar,
    entry_on_bar: bool = False,
) -> tuple[str, float] | None:
    """
    Check if TP or SL is hit within a bar using high/low.
    Returns (outcome, exit_price) or None if neither hit.

    Conflict resolution (both TP and SL reachable on same bar):
    - If this is the entry bar (entry_on_bar=True):
        Use bar.close as tiebreaker — the close tells us which direction
        the bar resolved toward after entry fired.
        close >= tp → TP (price continued past entry toward TP)
        close <= sl → SL (price reversed through SL)
        otherwise   → SL (conservative)
    - If this is a subsequent bar (entry_on_bar=False):
        SL wins — worst-case assumption.
    """
    if direction == "LONG":
        sl_hit = bar.low  <= sl
        tp_hit = bar.high >= tp
        if sl_hit and tp_hit:
            if entry_on_bar:
                if bar.close >= tp:
                    return "TP", tp
                return "SL", sl
            return "SL", sl   # subsequent bar: worst case
        if sl_hit:
            return "SL", sl
        if tp_hit:
            return "TP", tp
    else:
        sl_hit = bar.high >= sl
        tp_hit = bar.low  <= tp
        if sl_hit and tp_hit:
            if entry_on_bar:
                if bar.close <= tp:
                    return "TP", tp
                return "SL", sl
            return "SL", sl
        if sl_hit:
            return "SL", sl
        if tp_hit:
            return "TP", tp
    return None


def _check_entry_on_bar(
    direction:  Direction,
    entry:      float,
    bar:        Bar,
    overshoot:  float,
) -> bool:
    """
    Check if bar crosses the entry level.
    Uses bar HIGH for LONG entries, bar LOW for SHORT entries.
    Overshoot filter: reject if bar close is more than overshoot pips past entry
    (proxy for a stale fill on fast bars).
    """
    if direction == "LONG":
        if bar.high < entry:
            return False
        # Overshoot: bar closed too far above entry — stale fill
        if bar.close - entry > overshoot:
            return False
        return True
    else:
        if bar.low > entry:
            return False
        if entry - bar.close > overshoot:
            return False
        return True


def run_day(date: str, bars: list[Bar], cfg: StrategyConfig) -> DayReport:
    report          = DayReport(date=date, start_price=0.0)
    already_traded: set[Direction] = set()
    trade_count     = 0
    realized_pnl    = 0.0
    open_trade: TradeRecord | None = None

    # ── LOCK START PRICE ────────────────────────────────────────────────────
    start_bar = next(
        (b for b in bars if b.hhmm >= cfg.session_start_hhmm), None
    )
    if start_bar is None:
        report.no_trigger = True
        return report

    start_price       = start_bar.open
    report.start_price = start_price
    levels            = compute_levels(start_price, cfg)

    # ── BAR REPLAY ──────────────────────────────────────────────────────────
    for bar in bars:

        # Skip pre-session bars
        if bar.hhmm < cfg.session_start_hhmm:
            continue

        # Force-close time reached
        if bar.hhmm >= cfg.force_close_hhmm:
            if open_trade is not None:
                ep    = bar.open
                gross = _pnl(open_trade.direction, open_trade.entry_price, ep, cfg.lot_size)
                sp    = _spread_cost(cfg.lot_size)
                realized_pnl        += gross
                open_trade.exit_price = ep
                open_trade.exit_bar   = bar.hhmm
                open_trade.outcome    = "FORCE_CLOSE"
                open_trade.gross_pnl  = gross
                open_trade.spread_cost= sp
                open_trade.net_pnl    = round(gross - sp, 2)
                open_trade = None
            break

        # ── STEP 1: Resolve open position on this bar ────────────────────
        if open_trade is not None:
            result = _check_exit_on_bar(
                open_trade.direction,
                open_trade.entry_price,
                open_trade.tp_price,
                open_trade.sl_price,
                bar,
                entry_on_bar=False,   # opened on previous bar → SL wins conflict
            )
            if result is not None:
                outcome, exit_price = result
                gross = _pnl(open_trade.direction, open_trade.entry_price, exit_price, cfg.lot_size)
                sp    = _spread_cost(cfg.lot_size)
                realized_pnl         += gross
                open_trade.exit_price  = exit_price
                open_trade.exit_bar    = bar.hhmm
                open_trade.outcome     = outcome
                open_trade.gross_pnl   = gross
                open_trade.spread_cost = sp
                open_trade.net_pnl     = round(gross - sp, 2)
                open_trade = None

        # ── STEP 2: Check daily gates ────────────────────────────────────
        if is_daily_profit_hit(realized_pnl, cfg):
            report.hit_profit = True
            break
        if is_daily_limit_breached(realized_pnl, cfg):
            report.hit_loss = True
            break
        if trade_count >= cfg.max_trades_per_day:
            break
        if open_trade is not None:
            continue

        # ── STEP 3: Check for new entry (both directions if mode=both) ───
        mode = cfg.direction_mode
        if mode == "first_only" and already_traded:
            continue

        directions_to_check: list[Direction] = []
        if "LONG" not in already_traded:
            directions_to_check.append("LONG")
        if "SHORT" not in already_traded:
            directions_to_check.append("SHORT")

        for direction in directions_to_check:
            entry  = levels.long_entry  if direction == "LONG"  else levels.short_entry
            tp     = levels.long_tp     if direction == "LONG"  else levels.short_tp
            sl     = levels.long_sl     if direction == "LONG"  else levels.short_sl

            if not _check_entry_on_bar(direction, entry, bar, cfg.max_entry_overshoot_pips):
                continue

            # Risk gate
            snap = RiskSnapshot(
                realized_pnl        = realized_pnl,
                open_pnl            = 0.0,
                trade_count         = trade_count,
                open_position_count = 0,
            )
            allowed, _ = can_place_trade(snap, cfg)
            if not allowed:
                continue

            # Entry fires — record trade
            trade = TradeRecord(
                day         = date,
                direction   = direction,
                entry_price = entry,
                tp_price    = tp,
                sl_price    = sl,
                entry_bar   = bar.hhmm,
            )
            report.trades.append(trade)
            already_traded.add(direction)
            trade_count += 1

            # ── STEP 4: Check same-bar TP/SL immediately ────────────────
            # Price may have entered AND exited within this same M5 bar.
            # Pass entry_on_bar=True so close price is used as tiebreaker.
            result = _check_exit_on_bar(direction, entry, tp, sl, bar, entry_on_bar=True)
            if result is not None:
                outcome, exit_price = result
                gross = _pnl(direction, entry, exit_price, cfg.lot_size)
                sp    = _spread_cost(cfg.lot_size)
                realized_pnl     += gross
                trade.exit_price  = exit_price
                trade.exit_bar    = bar.hhmm
                trade.outcome     = outcome
                trade.gross_pnl   = gross
                trade.spread_cost = sp
                trade.net_pnl     = round(gross - sp, 2)
                open_trade        = None
            else:
                open_trade = trade

            # In first_only mode, stop after first entry
            if mode == "first_only":
                break

    # ── EOD: resolve any still-open position ────────────────────────────
    if open_trade is not None and bars:
        ep    = bars[-1].close
        gross = _pnl(open_trade.direction, open_trade.entry_price, ep, cfg.lot_size)
        sp    = _spread_cost(cfg.lot_size)
        realized_pnl         += gross
        open_trade.exit_price  = ep
        open_trade.exit_bar    = "EOD"
        open_trade.outcome     = "FORCE_CLOSE"
        open_trade.gross_pnl   = gross
        open_trade.spread_cost = sp
        open_trade.net_pnl     = round(gross - sp, 2)

    # Day totals
    report.day_gross  = round(sum(t.gross_pnl   for t in report.trades), 2)
    report.day_spread = round(sum(t.spread_cost  for t in report.trades), 2)
    report.day_net    = round(report.day_gross - report.day_spread, 2)

    if not report.trades:
        report.no_trigger = True

    return report


# =============================================================================
# REPORT PRINTER
# =============================================================================

W = 76

def print_report(
    reports: list[DayReport],
    cfg:     StrategyConfig,
    months:  int,
    symbol:  str,
):
    total_gross  = sum(r.day_gross  for r in reports)
    total_spread = sum(r.day_spread for r in reports)
    total_net    = sum(r.day_net    for r in reports)
    total_trades = sum(len(r.trades) for r in reports)

    all_tp = sum(1 for r in reports for t in r.trades if t.outcome == "TP")
    all_sl = sum(1 for r in reports for t in r.trades if t.outcome == "SL")
    all_fc = sum(1 for r in reports for t in r.trades if t.outcome == "FORCE_CLOSE")

    win_days     = [r for r in reports if r.day_net > 0]
    loss_days    = [r for r in reports if r.day_net < 0]
    no_trig_days = [r for r in reports if r.no_trigger]
    profit_days  = [r for r in reports if r.hit_profit]
    loss_lim_days= [r for r in reports if r.hit_loss]

    win_rate    = (all_tp / total_trades * 100) if total_trades else 0
    roi         = (total_net / cfg.account_size * 100) if cfg.account_size else 0
    active_days = len(reports) - len(no_trig_days)
    avg_per_day = (total_net / active_days) if active_days else 0

    bar = "═" * W
    thn = "─" * W

    print()
    print(f"╔{bar}╗")
    print(f"║{'BACKTEST REPORT — XAUUSD THRESHOLD STRATEGY':^{W}}║")
    print(f"╠{bar}╣")
    print(f"║  Symbol: {symbol}  │  Period: {months}m  │  Account: ${cfg.account_size:,.0f}  │  Lot: {cfg.lot_size}  │  Entry: 1.1×={cfg.entry_offset}pip  Exit: 1.2×={cfg.exit_offset}pip".ljust(W) + "  ║")
    print(f"╠{bar}╣")
    print(f"║{'DAY-BY-DAY':^{W}}║")
    print(f"╠{thn}╣")

    for r in reports:
        if r.no_trigger:
            print(f"║  {r.date}  S={r.start_price:.2f}  No signal triggered".ljust(W) + "  ║")
            continue
        for i, t in enumerate(r.trades):
            sym   = "✅" if t.outcome=="TP" else "❌" if t.outcome=="SL" else "⚠️"
            stop  = " 🎯PROFIT_STOP" if (r.hit_profit and i==len(r.trades)-1) else \
                    " ⛔LOSS_STOP"   if (r.hit_loss   and i==len(r.trades)-1) else ""
            net_s = f"+${t.net_pnl:,.0f}" if t.net_pnl>=0 else f"-${abs(t.net_pnl):,.0f}"
            print(
                f"║  {r.date if i==0 else ' '*10}  "
                f"S={r.start_price:.1f}  {t.direction:<5}  "
                f"@{t.entry_price:.1f}→{t.exit_price:.1f}  "
                f"{sym}{t.outcome:<11}  "
                f"gross=${t.gross_pnl:+,.0f}  net={net_s}{stop}".ljust(W) + "  ║"
            )
        day_s = f"+${r.day_net:,.0f}" if r.day_net>=0 else f"-${abs(r.day_net):,.0f}"
        print(f"║{'':>12}  DAY → gross=${r.day_gross:+,.0f}  spread=-${r.day_spread:,.0f}  net={day_s}".ljust(W) + "  ║")
        print(f"╠{thn}╣")

    def kv(l, v):
        return f"║  {l:<32}{str(v):<{W-36}}  ║"

    print(f"╠{bar}╣")
    print(f"║{'MONTHLY SUMMARY':^{W}}║")
    print(f"╠{bar}╣")
    print(kv("Trading days:", len(reports)))
    print(kv("Active days (had trades):", f"{active_days}  ({len(win_days)} win / {len(loss_days)} loss)"))
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
    print(kv("Total spread cost:", f"-${total_spread:,.2f}"))
    print(kv("NET P&L:", f"${total_net:+,.2f}"))
    print(kv("Avg net / active day:", f"${avg_per_day:+,.2f}"))
    print(kv("Monthly ROI:", f"{roi:.2f}%"))
    print(f"╠{bar}╣")
    print(f"║{'INCOME PROJECTION':^{W}}║")
    print(f"╠{bar}╣")
    proj_22  = avg_per_day * 22
    proj_yr  = proj_22 * 12
    proj_roi = (proj_yr / cfg.account_size * 100) if cfg.account_size else 0
    print(kv("Avg net / active day:", f"${avg_per_day:+,.2f}"))
    print(kv("Projected 22-day month:", f"${proj_22:+,.2f}"))
    print(kv("Projected annual:", f"${proj_yr:+,.2f}"))
    print(kv("Projected annual ROI:", f"{proj_roi:.1f}%"))
    print(f"╚{bar}╝")
    print()


# =============================================================================
# CLI
# =============================================================================

def _make_cfg(args) -> StrategyConfig:
    cfg = StrategyConfig(
        account_size             = args.capital,
        symbol                   = args.symbol,
        daily_profit_target_usd  = args.profit_target,
        session_start_hhmm       = args.session_start,
        session_end_hhmm         = args.session_end,
        max_entry_overshoot_pips = args.overshoot,
    )
    # Explicit lot override from CLI
    cfg.__dict__['_lot_override'] = args.lot
    original = type(cfg).lot_size.fget
    type(cfg).lot_size = property(
        lambda self: self.__dict__.get('_lot_override') or original(self)
    )
    return cfg


def parse_args():
    p = argparse.ArgumentParser(
        description = "XAUUSD Threshold Strategy Backtest",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
Examples:
  python backtest.py --capital 50000 --lot 2.5
  python backtest.py --capital 10000 --lot 0.5 --months 1
  python backtest.py --capital 100000 --lot 5.0 --months 2
  python backtest.py --capital 20000 --lot 1.0 --symbol XAUUSD --months 3
        """
    )
    p.add_argument("--capital",       type=float, required=True)
    p.add_argument("--lot",           type=float, required=True)
    p.add_argument("--months",        type=int,   default=1)
    p.add_argument("--symbol",        type=str,   default="XAUUSD")
    p.add_argument("--profit-target", type=float, default=150.0)
    p.add_argument("--session-start", type=str,   default="08:00")
    p.add_argument("--session-end",   type=str,   default="20:00")
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
        bars = fetch_bars(args.symbol, args.months, cfg.server_utc_offset_hours)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    day_map = group_by_day(bars, cfg.server_utc_offset_hours)
    print(f"[BACKTEST] {len(day_map)} trading days\n")

    reports: list[DayReport] = []
    for date, day_bars in day_map.items():
        if args.verbose:
            print(f"[BACKTEST] {date} ({len(day_bars)} bars)...")
        reports.append(run_day(date, day_bars, cfg))

    print_report(reports, cfg, args.months, args.symbol)


if __name__ == "__main__":
    main()
