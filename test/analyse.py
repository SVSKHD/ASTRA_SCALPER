"""
XAUUSD Hedge Recovery — Real Data Analyser
==========================================
Accepts any of these formats:
  1. MT5 exported CSV     (OHLCV M1/M5)
  2. Trade history CSV    (from MT5 terminal export)
  3. Tick data CSV        (date, time, bid, ask)

Run:
    python analyse_real_data.py --file your_data.csv --type ohlcv
    python analyse_real_data.py --file your_data.csv --type trades
    python analyse_real_data.py --file your_data.csv --type tick

Outputs:
    real_trade_log.csv
    real_monthly_summary.csv
    real_yearly_summary.csv
    real_analysis_report.txt
"""

import csv
import sys
import os
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

# ── STRATEGY CONFIG (match your bot) ────────────────────────────────────────
HEDGE_TRIGGER_PIPS  = -5        # floating loss pips to open hedge
COMBINED_TARGET     = 30        # $ combined profit → close both
EMERGENCY_STOP      = -150      # $ combined loss  → force close
PRIMARY_LOTS        = 2.0
HEDGE_LOTS          = 3.0
PIP_VALUE           = 10        # $ per pip per lot (XAUUSD)
TP_PIPS             = 10
SL_PIPS             = 5
PROP_SPLIT          = 0.80
SPREAD_PIPS         = 0.3       # avg spread on your broker

PRIMARY_PIP_VAL     = PRIMARY_LOTS * PIP_VALUE   # $20/pip
HEDGE_PIP_VAL       = HEDGE_LOTS   * PIP_VALUE   # $30/pip

# ── COLUMN AUTO-DETECT ───────────────────────────────────────────────────────
MT5_OHLCV_COLS   = ["date", "time", "open", "high", "low", "close", "volume"]
MT5_TRADE_COLS   = ["ticket", "open_time", "type", "lots", "symbol",
                    "open_price", "sl", "tp", "close_time", "close_price",
                    "profit"]
TICK_COLS        = ["date", "time", "bid", "ask"]


def detect_format(headers):
    h = [c.strip().lower() for c in headers]
    if "ticket" in h or "open_price" in h:
        return "trades"
    if "bid" in h or "ask" in h:
        return "tick"
    if "open" in h or "close" in h:
        return "ohlcv"
    return None


def parse_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except:
        return None


def parse_datetime(date_str, time_str=""):
    combined = f"{date_str} {time_str}".strip()
    formats = [
        "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
        "%Y.%m.%d", "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(combined, fmt)
        except:
            continue
    return None


# ── LOADERS ──────────────────────────────────────────────────────────────────
def load_ohlcv(filepath):
    """Load MT5 OHLCV M1/M5 bars → list of bar dicts"""
    bars = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        reader = csv.DictReader(f, dialect=dialect)
        headers = reader.fieldnames or []
        print(f"  Detected columns: {headers}")
        for row in reader:
            h = {k.strip().lower(): v for k, v in row.items()}
            date_str = h.get("date") or h.get("<date>") or ""
            time_str = h.get("time") or h.get("<time>") or ""
            dt = parse_datetime(date_str, time_str)
            if not dt:
                continue
            o = parse_float(h.get("open") or h.get("<open>"))
            c = parse_float(h.get("close") or h.get("<close>"))
            hi = parse_float(h.get("high") or h.get("<high>"))
            lo = parse_float(h.get("low") or h.get("<low>"))
            if None in (o, c, hi, lo):
                continue
            bars.append({"dt": dt, "open": o, "high": hi, "low": lo, "close": c})
    print(f"  Loaded {len(bars)} bars")
    return bars


def load_tick(filepath):
    """Load tick data → list of tick dicts"""
    ticks = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            h = {k.strip().lower(): v for k, v in row.items()}
            dt = parse_datetime(h.get("date",""), h.get("time",""))
            bid = parse_float(h.get("bid"))
            ask = parse_float(h.get("ask"))
            if not dt or bid is None:
                continue
            mid = bid if ask is None else (bid + ask) / 2
            ticks.append({"dt": dt, "price": mid})
    print(f"  Loaded {len(ticks)} ticks")
    return ticks


def load_trades(filepath):
    """Load MT5 exported trade history → list of closed trade dicts"""
    trades = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            h = {k.strip().lower().replace(" ","_"): v for k, v in row.items()}
            open_dt  = parse_datetime(h.get("open_time",""))
            close_dt = parse_datetime(h.get("close_time",""))
            profit   = parse_float(h.get("profit") or h.get("pnl") or "0")
            op = parse_float(h.get("open_price") or h.get("price") or "0")
            cp = parse_float(h.get("close_price") or h.get("close") or "0")
            if not open_dt:
                continue
            trades.append({
                "open_dt":    open_dt,
                "close_dt":   close_dt or open_dt,
                "profit":     profit or 0,
                "open_price": op or 0,
                "close_price":cp or 0,
                "lots":       parse_float(h.get("lots") or h.get("volume") or "2") or 2.0,
                "type":       h.get("type","buy").lower()
            })
    print(f"  Loaded {len(trades)} trades")
    return trades


# ── CORE SIMULATION ENGINE ───────────────────────────────────────────────────
def simulate_hedge_on_bars(bars):
    """
    Walk bar by bar. On each bar treat open as a potential signal entry.
    Simulate TP/SL/hedge logic and return trade records.
    """
    trade_log = []
    trade_id  = 1
    i = 0

    while i < len(bars) - TP_PIPS * 3:
        bar = bars[i]
        entry_price = bar["open"]
        entry_dt    = bar["dt"]

        # Skip if outside normal session (8:00–20:00 UTC)
        if entry_dt.hour < 8 or entry_dt.hour >= 20:
            i += 1
            continue

        # Direction: buy if close > open (bullish bar), else sell
        direction = "BUY" if bar["close"] >= bar["open"] else "SELL"

        tp_price = (entry_price + TP_PIPS) if direction == "BUY" else (entry_price - TP_PIPS)
        sl_price = (entry_price - SL_PIPS) if direction == "BUY" else (entry_price + SL_PIPS)

        primary_pnl   = None
        hedge_pnl     = 0
        hedge_open    = False
        hedge_entry   = None
        scenario      = "clean_win"
        exit_dt       = entry_dt
        bars_to_exit  = 0

        for j in range(i + 1, min(i + 120, len(bars))):
            future = bars[j]
            hi, lo = future["high"], future["low"]
            bars_to_exit = j - i

            # Check if primary hit TP
            if direction == "BUY" and hi >= tp_price:
                primary_pnl = TP_PIPS * PRIMARY_PIP_VAL
                scenario    = "clean_win"
                exit_dt     = future["dt"]
                break

            if direction == "SELL" and lo <= tp_price:
                primary_pnl = TP_PIPS * PRIMARY_PIP_VAL
                scenario    = "clean_win"
                exit_dt     = future["dt"]
                break

            # Check hedge trigger
            current_price = future["close"]
            float_pips = (current_price - entry_price) if direction == "BUY" else (entry_price - current_price)

            if not hedge_open and float_pips <= HEDGE_TRIGGER_PIPS:
                hedge_open  = True
                hedge_entry = current_price
                hedge_dir   = "SELL" if direction == "BUY" else "BUY"

            # If hedge open, track combined P&L
            if hedge_open:
                p_pnl = float_pips * PRIMARY_PIP_VAL
                h_pips = (hedge_entry - current_price) if hedge_dir == "SELL" else (current_price - hedge_entry)
                h_pnl  = h_pips * HEDGE_PIP_VAL
                combined = p_pnl + h_pnl

                if combined >= COMBINED_TARGET:
                    primary_pnl = p_pnl
                    hedge_pnl   = h_pnl
                    exit_dt     = future["dt"]
                    # classify scenario
                    if h_pips > 0 and p_pnl < 0:
                        scenario = "hedge_keeps_falling"
                    elif p_pnl > 0:
                        scenario = "hedge_reversal_tp"
                    else:
                        scenario = "hedge_chop_recovery"
                    break

                if combined <= EMERGENCY_STOP:
                    primary_pnl = p_pnl
                    hedge_pnl   = h_pnl
                    scenario    = "emergency_stop"
                    exit_dt     = future["dt"]
                    break

            # Primary SL hit (no hedge)
            if not hedge_open:
                if direction == "BUY" and lo <= sl_price:
                    primary_pnl = -SL_PIPS * PRIMARY_PIP_VAL
                    scenario    = "sl_hit"
                    exit_dt     = future["dt"]
                    break
                if direction == "SELL" and hi >= sl_price:
                    primary_pnl = -SL_PIPS * PRIMARY_PIP_VAL
                    scenario    = "sl_hit"
                    exit_dt     = future["dt"]
                    break

        if primary_pnl is None:
            i += 1
            continue

        spread_cost  = 2 * SPREAD_PIPS * PRIMARY_PIP_VAL if hedge_open else SPREAD_PIPS * PRIMARY_PIP_VAL
        combined_pnl = primary_pnl + hedge_pnl - spread_cost

        trade_log.append({
            "trade_id":        trade_id,
            "entry_date":      entry_dt.strftime("%Y-%m-%d"),
            "entry_time":      entry_dt.strftime("%H:%M"),
            "exit_date":       exit_dt.strftime("%Y-%m-%d"),
            "exit_time":       exit_dt.strftime("%H:%M"),
            "month":           entry_dt.month,
            "year":            entry_dt.year,
            "direction":       direction,
            "entry_price":     round(entry_price, 3),
            "scenario":        scenario,
            "hedge_triggered": hedge_open,
            "hedge_entry":     round(hedge_entry, 3) if hedge_entry else "",
            "primary_lots":    PRIMARY_LOTS,
            "hedge_lots":      HEDGE_LOTS if hedge_open else 0,
            "primary_pnl":     round(primary_pnl, 2),
            "hedge_pnl":       round(hedge_pnl, 2),
            "combined_pnl":    round(combined_pnl, 2),
            "your_cut_80pct":  round(combined_pnl * PROP_SPLIT, 2),
            "bars_to_exit":    bars_to_exit,
        })

        trade_id += 1
        i += max(bars_to_exit, 5)

    return trade_log


def simulate_from_trades(trades):
    """Analyse real closed trades from MT5 export."""
    trade_log = []
    for idx, t in enumerate(trades):
        profit      = t["profit"]
        direction   = "BUY" if t["type"] in ("buy","0") else "SELL"
        pip_move    = (t["close_price"] - t["open_price"]) if direction == "BUY" else (t["open_price"] - t["close_price"])
        hedge_open  = pip_move < HEDGE_TRIGGER_PIPS

        if profit > 0:
            scenario = "clean_win"
        elif profit > EMERGENCY_STOP:
            scenario = "hedge_chop_recovery" if hedge_open else "sl_hit"
        else:
            scenario = "emergency_stop"

        trade_log.append({
            "trade_id":        idx + 1,
            "entry_date":      t["open_dt"].strftime("%Y-%m-%d"),
            "entry_time":      t["open_dt"].strftime("%H:%M"),
            "exit_date":       t["close_dt"].strftime("%Y-%m-%d"),
            "exit_time":       t["close_dt"].strftime("%H:%M"),
            "month":           t["open_dt"].month,
            "year":            t["open_dt"].year,
            "direction":       direction,
            "entry_price":     round(t["open_price"], 3),
            "scenario":        scenario,
            "hedge_triggered": hedge_open,
            "hedge_entry":     "",
            "primary_lots":    t["lots"],
            "hedge_lots":      HEDGE_LOTS if hedge_open else 0,
            "primary_pnl":     round(profit, 2),
            "hedge_pnl":       0,
            "combined_pnl":    round(profit, 2),
            "your_cut_80pct":  round(profit * PROP_SPLIT, 2),
            "bars_to_exit":    int((t["close_dt"] - t["open_dt"]).total_seconds() / 300),
        })
    return trade_log


# ── ANALYSIS ─────────────────────────────────────────────────────────────────
def build_monthly(trade_log):
    monthly = defaultdict(lambda: {
        "year": 0, "month": 0, "total_trades": 0,
        "clean_wins": 0, "hedge_trades": 0, "sl_hits": 0,
        "emergency_stops": 0, "gross_pnl": 0, "your_cut": 0,
        "best_trade": -9e9, "worst_trade": 9e9,
        "avg_bars_to_exit": 0, "_bars_sum": 0
    })
    for r in trade_log:
        k = f"{r['year']}-{r['month']:02d}"
        m = monthly[k]
        m["year"]  = r["year"]
        m["month"] = r["month"]
        m["total_trades"]  += 1
        m["gross_pnl"]     += r["combined_pnl"]
        m["your_cut"]      += r["your_cut_80pct"]
        m["best_trade"]     = max(m["best_trade"], r["combined_pnl"])
        m["worst_trade"]    = min(m["worst_trade"], r["combined_pnl"])
        m["_bars_sum"]     += r["bars_to_exit"]
        if r["scenario"] == "clean_win":      m["clean_wins"]     += 1
        if r["hedge_triggered"]:              m["hedge_trades"]   += 1
        if r["scenario"] == "sl_hit":         m["sl_hits"]        += 1
        if r["scenario"] == "emergency_stop": m["emergency_stops"]+= 1

    result = []
    for k in sorted(monthly.keys()):
        m = monthly[k]
        n = m["total_trades"] or 1
        m["win_rate_pct"]      = round(m["clean_wins"]     / n * 100, 1)
        m["hedge_rate_pct"]    = round(m["hedge_trades"]   / n * 100, 1)
        m["emergency_pct"]     = round(m["emergency_stops"]/ n * 100, 1)
        m["avg_bars_to_exit"]  = round(m["_bars_sum"] / n, 1)
        m["gross_pnl"]         = round(m["gross_pnl"], 2)
        m["your_cut"]          = round(m["your_cut"], 2)
        m["best_trade"]        = round(m["best_trade"], 2)
        m["worst_trade"]       = round(m["worst_trade"], 2)
        del m["_bars_sum"]
        result.append(m)
    return result


def build_yearly(trade_log, monthly):
    total  = len(trade_log)
    if total == 0:
        print("  No trades to analyse.")
        return []
    gross  = sum(r["combined_pnl"]   for r in trade_log)
    cut    = sum(r["your_cut_80pct"] for r in trade_log)
    hedges = sum(1 for r in trade_log if r["hedge_triggered"])
    emerg  = sum(1 for r in trade_log if r["scenario"] == "emergency_stop")
    wins   = sum(1 for r in trade_log if r["combined_pnl"] > 0)
    months = len(monthly)

    yearly = [{
        "total_trades":          total,
        "winning_trades":        wins,
        "overall_win_rate_pct":  round(wins / total * 100, 1),
        "hedge_triggered_count": hedges,
        "hedge_rate_pct":        round(hedges / total * 100, 1),
        "emergency_stops":       emerg,
        "emergency_rate_pct":    round(emerg / total * 100, 1),
        "gross_pnl_usd":         round(gross, 2),
        "your_cut_80pct_usd":    round(cut, 2),
        "avg_monthly_gross":     round(gross / months, 2) if months else 0,
        "avg_monthly_cut":       round(cut  / months, 2) if months else 0,
        "best_month_cut":        round(max(m["your_cut"] for m in monthly), 2),
        "worst_month_cut":       round(min(m["your_cut"] for m in monthly), 2),
        "best_single_trade":     round(max(r["combined_pnl"] for r in trade_log), 2),
        "worst_single_trade":    round(min(r["combined_pnl"] for r in trade_log), 2),
        "config_hedge_trigger":  f"{HEDGE_TRIGGER_PIPS} pips",
        "config_combined_target":f"${COMBINED_TARGET}",
        "config_emergency_stop": f"${EMERGENCY_STOP}",
        "config_primary_lots":   PRIMARY_LOTS,
        "config_hedge_lots":     HEDGE_LOTS,
        "config_prop_split":     f"{int(PROP_SPLIT*100)}%",
    }]
    return yearly


def print_report(trade_log, monthly, yearly):
    if not yearly:
        return
    y = yearly[0]
    sep = "─" * 62
    print(f"\n{sep}")
    print(f"  REAL DATA ANALYSIS REPORT")
    print(sep)
    print(f"  Total trades         : {y['total_trades']}")
    print(f"  Win rate             : {y['overall_win_rate_pct']}%")
    print(f"  Hedge triggered      : {y['hedge_triggered_count']} ({y['hedge_rate_pct']}%)")
    print(f"  Emergency stops      : {y['emergency_stops']} ({y['emergency_rate_pct']}%)")
    print(f"  Gross P&L            : ${y['gross_pnl_usd']:,.2f}")
    print(f"  Your cut (80%)       : ${y['your_cut_80pct_usd']:,.2f}")
    print(f"  Avg monthly cut      : ${y['avg_monthly_cut']:,.2f}")
    print(f"  Best month           : ${y['best_month_cut']:,.2f}")
    print(f"  Worst month          : ${y['worst_month_cut']:,.2f}")
    print(f"  Best single trade    : ${y['best_single_trade']:,.2f}")
    print(f"  Worst single trade   : ${y['worst_single_trade']:,.2f}")
    print(sep)
    print(f"  MONTHLY BREAKDOWN")
    print(sep)
    print(f"  {'Month':<10} {'Trades':>7} {'WinRate':>8} {'HedgeRate':>10} {'Gross':>10} {'Your Cut':>10}")
    print(f"  {'-'*58}")
    for m in monthly:
        label = f"{m['year']}-{m['month']:02d}"
        print(f"  {label:<10} {m['total_trades']:>7} {m['win_rate_pct']:>7}% "
              f"{m['hedge_rate_pct']:>9}% {m['gross_pnl']:>10,.2f} {m['your_cut']:>10,.2f}")
    print(sep)


def write_report_txt(trade_log, monthly, yearly, out_dir):
    if not yearly:
        return
    y = yearly[0]
    lines = []
    lines.append("XAUUSD HEDGE RECOVERY — REAL DATA ANALYSIS REPORT")
    lines.append("=" * 62)
    lines.append(f"Total trades         : {y['total_trades']}")
    lines.append(f"Win rate             : {y['overall_win_rate_pct']}%")
    lines.append(f"Hedge triggered      : {y['hedge_triggered_count']} ({y['hedge_rate_pct']}%)")
    lines.append(f"Emergency stops      : {y['emergency_stops']} ({y['emergency_rate_pct']}%)")
    lines.append(f"Gross P&L            : ${y['gross_pnl_usd']:,.2f}")
    lines.append(f"Your cut (80%)       : ${y['your_cut_80pct_usd']:,.2f}")
    lines.append(f"Avg monthly cut      : ${y['avg_monthly_cut']:,.2f}")
    lines.append(f"Best month cut       : ${y['best_month_cut']:,.2f}")
    lines.append(f"Worst month cut      : ${y['worst_month_cut']:,.2f}")
    lines.append("")
    lines.append("MONTHLY BREAKDOWN")
    lines.append("-" * 62)
    for m in monthly:
        label = f"{m['year']}-{m['month']:02d}"
        lines.append(f"{label}  trades={m['total_trades']}  win={m['win_rate_pct']}%  "
                     f"hedge={m['hedge_rate_pct']}%  gross=${m['gross_pnl']:,.2f}  cut=${m['your_cut']:,.2f}")
    lines.append("")
    lines.append("CONFIG USED")
    lines.append(f"  Hedge trigger  : {y['config_hedge_trigger']}")
    lines.append(f"  Target         : {y['config_combined_target']}")
    lines.append(f"  Emergency stop : {y['config_emergency_stop']}")
    lines.append(f"  Primary lots   : {y['config_primary_lots']}")
    lines.append(f"  Hedge lots     : {y['config_hedge_lots']}")
    lines.append(f"  Prop split     : {y['config_prop_split']}")

    path = os.path.join(out_dir, "real_analysis_report.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def write_csvs(trade_log, monthly, yearly, out_dir):
    def w(name, rows):
        if not rows:
            return
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"  Wrote: {path}")

    w("real_trade_log.csv",      trade_log)
    w("real_monthly_summary.csv", monthly)
    w("real_yearly_summary.csv",  yearly)


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="XAUUSD Hedge Recovery Real Data Analyser")
    parser.add_argument("--file",   required=True, help="Path to your CSV data file")
    parser.add_argument("--type",   choices=["ohlcv","trades","tick","auto"], default="auto",
                        help="Data format: ohlcv | trades | tick | auto (default: auto)")
    parser.add_argument("--outdir", default=".", help="Output directory for CSVs")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"  File not found: {args.file}")
        sys.exit(1)

    os.makedirs(args.outdir, exist_ok=True)

    print(f"\n  Loading: {args.file}")

    # Auto-detect format
    fmt = args.type
    if fmt == "auto":
        with open(args.file, newline="", encoding="utf-8-sig") as f:
            sample = f.read(1024)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
                reader = csv.reader(f, dialect=dialect)
                headers = next(reader)
            except:
                headers = []
        fmt = detect_format(headers)
        print(f"  Auto-detected format: {fmt}")

    if fmt == "ohlcv":
        bars      = load_ohlcv(args.file)
        trade_log = simulate_hedge_on_bars(bars)
    elif fmt == "tick":
        ticks     = load_tick(args.file)
        # Convert ticks to 5-min bars
        bars      = ticks_to_bars(ticks, interval_minutes=5)
        trade_log = simulate_hedge_on_bars(bars)
    elif fmt == "trades":
        raw_trades = load_trades(args.file)
        trade_log  = simulate_from_trades(raw_trades)
    else:
        print(f"  Could not detect format. Use --type ohlcv|trades|tick")
        sys.exit(1)

    if not trade_log:
        print("  No trades generated from data. Check format or date range.")
        sys.exit(1)

    print(f"  Simulated {len(trade_log)} trades")

    monthly = build_monthly(trade_log)
    yearly  = build_yearly(trade_log, monthly)

    print_report(trade_log, monthly, yearly)
    write_csvs(trade_log, monthly, yearly, args.outdir)
    write_report_txt(trade_log, monthly, yearly, args.outdir)


def ticks_to_bars(ticks, interval_minutes=5):
    """Aggregate ticks into OHLCV bars."""
    if not ticks:
        return []
    bars  = []
    start = ticks[0]["dt"]
    delta = timedelta(minutes=interval_minutes)
    bucket_end = start + delta
    o = h = l = c = ticks[0]["price"]

    for tick in ticks:
        if tick["dt"] >= bucket_end:
            bars.append({"dt": start, "open": o, "high": h, "low": l, "close": c})
            start = bucket_end
            bucket_end = start + delta
            o = h = l = c = tick["price"]
        else:
            h = max(h, tick["price"])
            l = min(l, tick["price"])
            c = tick["price"]

    if o is not None:
        bars.append({"dt": start, "open": o, "high": h, "low": l, "close": c})

    print(f"  Aggregated {len(ticks)} ticks → {len(bars)} bars ({interval_minutes}m)")
    return bars


if __name__ == "__main__":
    main()