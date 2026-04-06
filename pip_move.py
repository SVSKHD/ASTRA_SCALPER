from __future__ import annotations

# =============================================================================
# PIP MOVE ANALYSIS — how far does price move from 00:00 UTC start price
#                     in a single direction before reversing?
#
# WHAT THIS ANSWERS:
#   "If I lock start price at 00:00 UTC, how many pips does gold typically
#    move in one direction before pulling back?"
#
# KEY OUTPUTS:
#   1. Daily max UP move and max DOWN move from start price
#   2. Which direction fired FIRST each day (and how far it went)
#   3. Percentile table — tells you what threshold to set
#   4. "First breakout" analysis — what % of days hit 15/19/25/30 pips
#      in one direction before the other direction hit that level
#   5. HTML report with charts
#
# USAGE:
#   python pip_move_analysis.py --months 10
#   python pip_move_analysis.py --from-date 2025-06-01 --to-date 2026-04-05
#   python pip_move_analysis.py --months 3 --session-start 01:00 --session-end 20:00
# =============================================================================

import argparse
import json
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from statistics import mean, median, stdev

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


# =============================================================================
# DATA FETCH
# =============================================================================

def fetch_bars(symbol: str, months: int) -> list[dict]:
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    to_dt   = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=months * 31)
    print(f"[Analysis] Fetching M5: {symbol} | {from_dt.date()} → {to_dt.date()}")
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, from_dt, to_dt)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No M5 data for {symbol}.")
    bars = [
        {
            "time": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            "open":  float(r["open"]),
            "high":  float(r["high"]),
            "low":   float(r["low"]),
            "close": float(r["close"]),
        }
        for r in rates
    ]
    print(f"[Analysis] Fetched {len(bars):,} bars")
    return bars


def fetch_bars_range(symbol: str, from_dt: datetime, to_dt: datetime) -> list[dict]:
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    print(f"[Analysis] Fetching M5: {symbol} | {from_dt.date()} → {to_dt.date()}")
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, from_dt, to_dt)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No M5 data for {symbol}.")
    bars = [
        {
            "time": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            "open":  float(r["open"]),
            "high":  float(r["high"]),
            "low":   float(r["low"]),
            "close": float(r["close"]),
        }
        for r in rates
    ]
    print(f"[Analysis] Fetched {len(bars):,} bars")
    return bars


def group_by_day(bars: list[dict]) -> dict[str, list[dict]]:
    day_map: dict[str, list[dict]] = defaultdict(list)
    for bar in bars:
        key = bar["time"].strftime("%Y-%m-%d")
        day_map[key].append(bar)
    return dict(sorted(day_map.items()))


# =============================================================================
# START PRICE FROM DAY FILES
# =============================================================================

def load_start_from_day_file(date: str, data_dir: str, symbol: str) -> tuple[float, str] | None:
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
        lock_hhmm = lock_iso[11:16] if len(lock_iso) >= 16 else "00:00"
        return float(price), lock_hhmm
    except Exception:
        return None


# =============================================================================
# SINGLE-DIRECTION MOVE ANALYSIS
# =============================================================================

def analyze_day(
    date:         str,
    bars:         list[dict],
    start_price:  float,
    session_start: str = "00:00",
    session_end:   str = "23:00",
) -> dict | None:
    """
    For a single day, compute:
    - max_up_pips:   maximum move above start price (any time during session)
    - max_down_pips: maximum move below start price (any time during session)
    - first_dir:     which direction hit 5 pips first ("LONG" | "SHORT" | "NONE")
    - first_pip_sequence: list of (hhmm, up_pips, down_pips) sampled at each bar
    - breakout_times: dict mapping threshold → (direction, hhmm) of first clean breakout
    """
    if not bars or start_price <= 0:
        return None

    session_bars = [
        b for b in bars
        if session_start <= b["time"].strftime("%H:%M") <= session_end
    ]
    if not session_bars:
        return None

    max_up   = 0.0  # pips above start
    max_down = 0.0  # pips below start (positive number)
    first_5_dir  = "NONE"
    first_5_time = ""

    # Track the "single direction" move: from start, how far can price go
    # in one direction before the other side gets touched equally?
    # We also track the "run" — longest uninterrupted move in one direction.

    bar_sequence = []  # (hhmm, up, down) per bar
    breakout_first: dict[int, tuple[str, str]] = {}  # threshold_pips → (direction, hhmm)

    THRESHOLDS = [5, 8, 10, 12, 15, 19, 20, 25, 30, 35, 40, 50]

    pending_thresholds = set(THRESHOLDS)

    for bar in session_bars:
        hhmm  = bar["time"].strftime("%H:%M")
        up    = round(bar["high"] - start_price, 3)    # pips above start
        down  = round(start_price - bar["low"], 3)     # pips below start

        # Use intrabar high/low for max move tracking
        max_up   = max(max_up,   up)
        max_down = max(max_down, down)

        bar_sequence.append({
            "hhmm":    hhmm,
            "up":      round(up,   2),
            "down":    round(down, 2),
            "max_up":  round(max_up,   2),
            "max_down": round(max_down, 2),
        })

        # First direction to hit 5 pips
        if first_5_dir == "NONE":
            if up >= 5 and down < 5:
                first_5_dir  = "LONG"
                first_5_time = hhmm
            elif down >= 5 and up < 5:
                first_5_dir  = "SHORT"
                first_5_time = hhmm
            elif up >= 5 and down >= 5:
                first_5_dir  = "BOTH"
                first_5_time = hhmm

        # First clean breakout per threshold
        for t in list(pending_thresholds):
            if up >= t and down < t:
                breakout_first[t] = ("LONG",  hhmm)
                pending_thresholds.discard(t)
            elif down >= t and up < t:
                breakout_first[t] = ("SHORT", hhmm)
                pending_thresholds.discard(t)

    return {
        "date":         date,
        "start_price":  start_price,
        "max_up":       round(max_up,   2),
        "max_down":     round(max_down, 2),
        "day_range":    round(max_up + max_down, 2),
        "first_5_dir":  first_5_dir,
        "first_5_time": first_5_time,
        "dominant":     "LONG" if max_up > max_down else "SHORT" if max_down > max_up else "BOTH",
        "dominant_pips": round(max(max_up, max_down), 2),
        "losing_side":  round(min(max_up, max_down), 2),
        "breakout_first": breakout_first,
        "bar_sequence": bar_sequence,
    }


# =============================================================================
# AGGREGATE STATS
# =============================================================================

def percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = (pct / 100) * (len(sorted_data) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_data) - 1)
    return round(sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (idx - lo), 2)


def build_stats(days: list[dict]) -> dict:
    if not days:
        return {}

    up_moves   = [d["max_up"]   for d in days]
    down_moves = [d["max_down"] for d in days]
    dom_moves  = [d["dominant_pips"] for d in days]
    losing     = [d["losing_side"]   for d in days]
    ranges     = [d["day_range"]     for d in days]

    long_first  = sum(1 for d in days if d["first_5_dir"] == "LONG")
    short_first = sum(1 for d in days if d["first_5_dir"] == "SHORT")
    both_same   = sum(1 for d in days if d["first_5_dir"] == "BOTH")
    no_5pip     = sum(1 for d in days if d["first_5_dir"] == "NONE")
    dom_long    = sum(1 for d in days if d["dominant"]    == "LONG")
    dom_short   = sum(1 for d in days if d["dominant"]    == "SHORT")

    THRESHOLDS = [5, 8, 10, 12, 15, 19, 20, 25, 30, 35, 40, 50]
    breakout_stats = {}
    for t in THRESHOLDS:
        clean_long  = sum(1 for d in days if d["breakout_first"].get(t, ("",))[0] == "LONG")
        clean_short = sum(1 for d in days if d["breakout_first"].get(t, ("",))[0] == "SHORT")
        no_breakout = len(days) - clean_long - clean_short
        breakout_stats[t] = {
            "long":       clean_long,
            "short":      clean_short,
            "none":       no_breakout,
            "pct_any":    round((clean_long + clean_short) / len(days) * 100, 1),
            "pct_long":   round(clean_long  / len(days) * 100, 1),
            "pct_short":  round(clean_short / len(days) * 100, 1),
        }

    return {
        "n_days":     len(days),
        "up_moves":   up_moves,
        "down_moves": down_moves,
        "dom_moves":  dom_moves,
        "losing":     losing,
        "ranges":     ranges,
        # Percentiles
        "up_p25":    percentile(up_moves,   25),
        "up_p50":    percentile(up_moves,   50),
        "up_p75":    percentile(up_moves,   75),
        "up_p90":    percentile(up_moves,   90),
        "down_p25":  percentile(down_moves, 25),
        "down_p50":  percentile(down_moves, 50),
        "down_p75":  percentile(down_moves, 75),
        "down_p90":  percentile(down_moves, 90),
        "dom_p25":   percentile(dom_moves,  25),
        "dom_p50":   percentile(dom_moves,  50),
        "dom_p75":   percentile(dom_moves,  75),
        "dom_p90":   percentile(dom_moves,  90),
        "dom_mean":  round(mean(dom_moves), 2),
        "losing_p50": percentile(losing,   50),
        "range_p50":  percentile(ranges,   50),
        "range_p75":  percentile(ranges,   75),
        # Direction bias
        "long_first":  long_first,
        "short_first": short_first,
        "both_same":   both_same,
        "no_5pip":     no_5pip,
        "dom_long":    dom_long,
        "dom_short":   dom_short,
        # Breakout stats
        "breakout_stats": breakout_stats,
    }


# =============================================================================
# CONSOLE REPORT
# =============================================================================

def print_report(days: list[dict], stats: dict, symbol: str, session_start: str, session_end: str):
    n = stats["n_days"]
    sep = "═" * 72
    thn = "─" * 72

    print(f"\n╔{sep}╗")
    print(f"║{'PIP MOVE ANALYSIS — ' + symbol:^72}║")
    print(f"║{'From 00:00 UTC start price — single direction move':^72}║")
    print(f"╠{sep}╣")
    print(f"║  Days analysed   : {n}".ljust(73) + "║")
    print(f"║  Session         : {session_start} – {session_end} UTC".ljust(73) + "║")
    print(f"║  Period          : {days[0]['date']} → {days[-1]['date']}".ljust(73) + "║")
    print(f"╠{sep}╣")

    # ── MAX MOVE PERCENTILES ──────────────────────────────────────────────────
    print(f"║{'MAX MOVE FROM START PRICE (pips)':^72}║")
    print(f"╠{thn}╣")
    print(f"║  {'':30} {'P25':>8} {'P50':>8} {'P75':>8} {'P90':>8}  ║")
    print(f"║  {'UP move  (max high - start)':30} {stats['up_p25']:>8} {stats['up_p50']:>8} {stats['up_p75']:>8} {stats['up_p90']:>8}  ║")
    print(f"║  {'DOWN move (start - min low)':30} {stats['down_p25']:>8} {stats['down_p50']:>8} {stats['down_p75']:>8} {stats['down_p90']:>8}  ║")
    print(f"║  {'DOMINANT side (max of up/dn)':30} {stats['dom_p25']:>8} {stats['dom_p50']:>8} {stats['dom_p75']:>8} {stats['dom_p90']:>8}  ║")
    print(f"║  {'LOSING  side (min of up/dn)':30} {'':>8} {stats['losing_p50']:>8} {'':>8} {'':>8}  ║")
    print(f"║  {'FULL DAY RANGE (up + down)':30} {'':>8} {stats['range_p50']:>8} {stats['range_p75']:>8} {'':>8}  ║")
    print(f"╠{thn}╣")
    print(f"║  Average dominant move  : {stats['dom_mean']:.1f} pips".ljust(73) + "║")
    print(f"╠{sep}╣")

    # ── DIRECTION BIAS ────────────────────────────────────────────────────────
    print(f"║{'DIRECTION BIAS':^72}║")
    print(f"╠{thn}╣")
    print(f"║  First to hit 5 pips — LONG : {stats['long_first']:>3} days ({stats['long_first']/n*100:.1f}%)".ljust(73) + "║")
    print(f"║  First to hit 5 pips — SHORT: {stats['short_first']:>3} days ({stats['short_first']/n*100:.1f}%)".ljust(73) + "║")
    print(f"║  Both hit 5 pips same bar   : {stats['both_same']:>3} days ({stats['both_same']/n*100:.1f}%)".ljust(73) + "║")
    print(f"║  Never reached 5 pips either: {stats['no_5pip']:>3} days ({stats['no_5pip']/n*100:.1f}%)".ljust(73) + "║")
    print(f"║  {thn[:68]}  ║")
    print(f"║  Dominant direction — LONG : {stats['dom_long']:>3} days ({stats['dom_long']/n*100:.1f}%)".ljust(73) + "║")
    print(f"║  Dominant direction — SHORT: {stats['dom_short']:>3} days ({stats['dom_short']/n*100:.1f}%)".ljust(73) + "║")
    print(f"╠{sep}╣")

    # ── CLEAN BREAKOUT STATS ──────────────────────────────────────────────────
    print(f"║{'CLEAN SINGLE-DIRECTION BREAKOUT — THRESHOLD HIT BEFORE OPPOSITE SIDE':^72}║")
    print(f"╠{thn}╣")
    print(f"║  {'Threshold':>12}  {'LONG':>6}  {'SHORT':>6}  {'NONE':>6}  {'Any %':>6}  {'L%':>5}  {'S%':>5}  ║")
    print(f"║  {thn[:68]}  ║")

    bs = stats["breakout_stats"]
    for t in sorted(bs.keys()):
        b = bs[t]
        marker = " ◄ current" if t == 19 else ""
        print(
            f"║  {t:>10} pip  {b['long']:>6}  {b['short']:>6}  {b['none']:>6}  "
            f"{b['pct_any']:>5.1f}%  {b['pct_long']:>4.1f}%  {b['pct_short']:>4.1f}%{marker}".ljust(73) + "║"
        )

    print(f"╠{sep}╣")
    print(f"║  THRESHOLD RECOMMENDATION".ljust(73) + "║")
    print(f"╠{thn}╣")

    # Find threshold where clean breakout rate is ~50-60%
    best_t = None
    for t in sorted(bs.keys()):
        if bs[t]["pct_any"] >= 50:
            best_t = t
            break

    dom_p50 = stats["dom_p50"]
    dom_p75 = stats["dom_p75"]
    losing_p50 = stats["losing_p50"]

    print(f"║  Median dominant move  : {dom_p50} pips  → threshold should be < this".ljust(73) + "║")
    print(f"║  P75 dominant move     : {dom_p75} pips  → conservative threshold".ljust(73) + "║")
    print(f"║  Median losing side    : {losing_p50} pips  → SL must be INSIDE this".ljust(73) + "║")
    if best_t:
        print(f"║  50% clean breakout at : {best_t} pips  → minimum viable threshold".ljust(73) + "║")
    print(f"║".ljust(73) + "║")
    print(f"║  Rule: threshold = P50 dominant × 0.5–0.7  →  {round(dom_p50*0.5,1)}–{round(dom_p50*0.7,1)} pips".ljust(73) + "║")
    print(f"║  Rule: SL < P50 losing side ({losing_p50} pips) or you'll get stopped by noise".ljust(73) + "║")
    print(f"╠{sep}╣")

    # ── DAY BY DAY ────────────────────────────────────────────────────────────
    print(f"║{'DAY-BY-DAY SUMMARY':^72}║")
    print(f"╠{thn}╣")
    print(f"║  {'Date':10}  {'Start':>9}  {'Up↑':>6}  {'Down↓':>6}  {'Dom':>6}  {'Losing':>6}  {'First5':>6}  ║")
    print(f"║  {thn[:68]}  ║")
    for d in days:
        dom_tag = "↑LONG " if d["dominant"] == "LONG" else "↓SHORT"
        first   = d["first_5_dir"][:5].ljust(5) if d["first_5_dir"] != "NONE" else "NONE "
        print(
            f"║  {d['date']:10}  {d['start_price']:>9.3f}  "
            f"{d['max_up']:>6.1f}  {d['max_down']:>6.1f}  "
            f"{d['dominant_pips']:>6.1f}  {d['losing_side']:>6.1f}  "
            f"{first:>6}  ║"
        )
    print(f"╚{sep}╝")


# =============================================================================
# HTML REPORT
# =============================================================================

def generate_html(days: list[dict], stats: dict, symbol: str, args):
    import json as _json

    n = stats["n_days"]
    bs = stats["breakout_stats"]

    # Data for charts
    dates          = [d["date"][5:] for d in days]          # MM-DD
    up_vals        = [d["max_up"]   for d in days]
    down_vals      = [d["max_down"] for d in days]
    dom_vals       = [d["dominant_pips"] for d in days]
    losing_vals    = [d["losing_side"]   for d in days]
    dom_colors     = ["rgba(74,222,128,0.8)" if d["dominant"] == "LONG" else "rgba(248,113,113,0.8)" for d in days]

    thresh_labels  = sorted(bs.keys())
    thresh_any_pct = [bs[t]["pct_any"]   for t in thresh_labels]
    thresh_l_pct   = [bs[t]["pct_long"]  for t in thresh_labels]
    thresh_s_pct   = [bs[t]["pct_short"] for t in thresh_labels]

    data_json = _json.dumps({
        "dates":        dates,
        "up":           up_vals,
        "down":         down_vals,
        "dom":          dom_vals,
        "losing":       losing_vals,
        "domColors":    dom_colors,
        "threshLabels": thresh_labels,
        "threshAny":    thresh_any_pct,
        "threshLong":   thresh_l_pct,
        "threshShort":  thresh_s_pct,
        "domP25":  stats["dom_p25"],
        "domP50":  stats["dom_p50"],
        "domP75":  stats["dom_p75"],
        "domMean": stats["dom_mean"],
        "losingP50": stats["losing_p50"],
        "upP50":   stats["up_p50"],
        "downP50": stats["down_p50"],
        "nDays":   n,
        "symbol":  symbol,
        "period":  f"{days[0]['date']} → {days[-1]['date']}",
        "longFirst":  stats["long_first"],
        "shortFirst": stats["short_first"],
        "domLong":    stats["dom_long"],
        "domShort":   stats["dom_short"],
    })

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pip Move Analysis — {symbol}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;padding:24px;min-height:100vh}}
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
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:8px 10px;color:#64748b;font-weight:500;border-bottom:1px solid #2d3448}}
td{{padding:7px 10px;border-bottom:1px solid #1e2330}}
tr:hover td{{background:#253045}}
.pill{{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}}
.pill-green{{background:#14532d;color:#86efac}}
.pill-red{{background:#4c0519;color:#fca5a5}}
.pill-amber{{background:#451a03;color:#fcd34d}}
.badge{{font-size:10px;padding:2px 7px;border-radius:4px;background:#7c3aed;color:#e9d5ff;margin-left:6px}}
@media(max-width:700px){{.grid4{{grid-template-columns:1fr 1fr}}.grid2{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>Pip Move Analysis — {symbol} &nbsp;<span style="font-size:13px;color:#475569;font-weight:400" id="periodLabel"></span></h1>
<div class="grid4" id="stats"></div>
<div class="grid4" id="stats2"></div>

<h2>Daily pip move — up and down from 00:00 UTC start price</h2>
<div class="chart-wrap">
  <div style="display:flex;gap:16px;font-size:12px;color:#94a3b8;margin-bottom:12px;flex-wrap:wrap">
    <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#4ade80;margin-right:5px"></span>LONG dominant</span>
    <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#f87171;margin-right:5px"></span>SHORT dominant</span>
    <span style="color:#60a5fa">— P50 ({stats['dom_p50']} pips)</span>
    <span style="color:#fbbf24">— P75 ({stats['dom_p75']} pips)</span>
  </div>
  <div style="position:relative;height:280px"><canvas id="moveChart"></canvas></div>
</div>

<div class="grid2">
  <div>
    <h2>Clean breakout % by threshold (one side before the other)</h2>
    <div class="chart-wrap"><div style="position:relative;height:220px"><canvas id="threshChart"></canvas></div></div>
  </div>
  <div>
    <h2>Up vs down move distribution per day</h2>
    <div class="chart-wrap"><div style="position:relative;height:220px"><canvas id="updownChart"></canvas></div></div>
  </div>
</div>

<h2>Threshold recommendation table</h2>
<div class="card" style="padding:0;overflow:hidden">
<table>
<thead><tr>
  <th>Threshold (pips)</th>
  <th>Clean LONG</th>
  <th>Clean SHORT</th>
  <th>No breakout</th>
  <th>Any breakout %</th>
  <th>Verdict</th>
</tr></thead>
<tbody id="threshTable"></tbody>
</table>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D = {data_json};
document.getElementById('periodLabel').textContent = D.period;

function card(label, val, sub, cls=''){{
  return `<div class="card"><div class="label">${{label}}</div><div class="val ${{cls}}">${{val}}</div><div class="sub">${{sub}}</div></div>`;
}}
document.getElementById('stats').innerHTML = [
  card('Median dominant move', D.domP50+' pips', 'Price typically runs this far in winner direction', 'amber'),
  card('P75 dominant move',    D.domP75+' pips', '75% of days move at least this far', 'blue'),
  card('Mean dominant move',   D.domMean+' pips', 'Average winning side distance', ''),
  card('Median losing side',   D.losingP50+' pips', 'Opposite side — SL must be < this', 'red'),
].join('');
document.getElementById('stats2').innerHTML = [
  card('Long dominant days',  D.domLong+' / '+D.nDays, (D.domLong/D.nDays*100).toFixed(1)+'% of days', 'green'),
  card('Short dominant days', D.domShort+' / '+D.nDays, (D.domShort/D.nDays*100).toFixed(1)+'% of days', 'red'),
  card('Long moved first',    D.longFirst+' days', (D.longFirst/D.nDays*100).toFixed(1)+'% hit 5 pips up first', 'green'),
  card('Short moved first',   D.shortFirst+' days', (D.shortFirst/D.nDays*100).toFixed(1)+'% hit 5 pips down first', 'red'),
].join('');

// Move chart
const moveCtx = document.getElementById('moveChart');
new Chart(moveCtx, {{
  type: 'bar',
  data: {{
    labels: D.dates,
    datasets: [
      {{ label:'Dominant side', data:D.dom, backgroundColor:D.domColors, borderWidth:0, borderRadius:2 }},
      {{ label:'Losing side',   data:D.losing, backgroundColor:'rgba(71,85,105,0.5)', borderWidth:0, borderRadius:2 }},
    ]
  }},
  options: {{
    responsive:true, maintainAspectRatio:false,
    plugins:{{ legend:{{display:false}}, tooltip:{{
      backgroundColor:'#1e2330', titleColor:'#94a3b8', bodyColor:'#e2e8f0',
      borderColor:'#2d3448', borderWidth:1,
      callbacks:{{ label: ctx => ctx.dataset.label+': '+ctx.raw+' pips' }}
    }}}},
    scales:{{
      x:{{ stacked:true, ticks:{{display:false}}, grid:{{display:false}} }},
      y:{{ stacked:false,
        ticks:{{callback:v=>v+' pips', color:'#475569', font:{{size:10}}}},
        grid:{{color:'rgba(255,255,255,0.04)'}},
      }},
    }},
    annotation: {{ annotations: {{
      p50: {{ type:'line', yMin:D.domP50, yMax:D.domP50, borderColor:'#60a5fa', borderWidth:1.5, borderDash:[4,4] }},
      p75: {{ type:'line', yMin:D.domP75, yMax:D.domP75, borderColor:'#fbbf24', borderWidth:1.5, borderDash:[4,4] }},
    }} }}
  }}
}});

// Threshold chart
new Chart(document.getElementById('threshChart'), {{
  type:'bar',
  data:{{
    labels: D.threshLabels.map(t=>t+' pip'),
    datasets:[
      {{ label:'LONG clean %',  data:D.threshLong,  backgroundColor:'rgba(74,222,128,0.8)', borderWidth:0, borderRadius:2 }},
      {{ label:'SHORT clean %', data:D.threshShort, backgroundColor:'rgba(248,113,113,0.8)', borderWidth:0, borderRadius:2 }},
    ]
  }},
  options:{{
    responsive:true, maintainAspectRatio:false,
    plugins:{{ legend:{{display:true, labels:{{color:'#94a3b8',font:{{size:11}}}}}} ,
      tooltip:{{ backgroundColor:'#1e2330', titleColor:'#94a3b8', bodyColor:'#e2e8f0', borderColor:'#2d3448', borderWidth:1 }}
    }},
    scales:{{
      x:{{ stacked:true, ticks:{{color:'#475569',font:{{size:10}}}}, grid:{{display:false}} }},
      y:{{ stacked:true,
        ticks:{{callback:v=>v+'%', color:'#475569', font:{{size:10}}}},
        grid:{{color:'rgba(255,255,255,0.04)'}},
        max:100,
      }}
    }}
  }}
}});

// Up/down scatter
new Chart(document.getElementById('updownChart'), {{
  type:'scatter',
  data:{{
    datasets:[{{
      label:'Days', 
      data: D.up.map((u,i)=>({{{{"x":u,"y":D.down[i]}}}})),
      backgroundColor: D.domColors,
      pointRadius:4, pointHoverRadius:6,
    }}]
  }},
  options:{{
    responsive:true, maintainAspectRatio:false,
    plugins:{{ legend:{{display:false}},
      tooltip:{{ backgroundColor:'#1e2330', titleColor:'#94a3b8', bodyColor:'#e2e8f0', borderColor:'#2d3448', borderWidth:1,
        callbacks:{{ label: ctx => 'Up:'+ctx.raw.x+' Down:'+ctx.raw.y+' pips' }}
      }}
    }},
    scales:{{
      x:{{ title:{{display:true,text:'Up pips',color:'#64748b',font:{{size:10}}}},
           ticks:{{color:'#475569',font:{{size:10}}}}, grid:{{color:'rgba(255,255,255,0.04)'}} }},
      y:{{ title:{{display:true,text:'Down pips',color:'#64748b',font:{{size:10}}}},
           ticks:{{color:'#475569',font:{{size:10}}}}, grid:{{color:'rgba(255,255,255,0.04)'}} }},
    }}
  }}
}});

// Threshold table
const tbody = document.getElementById('threshTable');
D.threshLabels.forEach((t,i) => {{
  const any = D.threshAny[i];
  const l   = D.threshLong[i];
  const s   = D.threshShort[i];
  const none = (100 - any).toFixed(1);
  let verdict = '';
  if (any >= 70) verdict = '<span class="pill pill-green">Strong</span>';
  else if (any >= 50) verdict = '<span class="pill pill-amber">Viable</span>';
  else verdict = '<span class="pill pill-red">Weak</span>';
  if (t == 19) verdict += ' <span class="badge">current</span>';
  tbody.innerHTML += `<tr>
    <td><b>${{t}}</b></td>
    <td>${{l.toFixed(1)}}%</td>
    <td>${{s.toFixed(1)}}%</td>
    <td>${{none}}%</td>
    <td><b>${{any.toFixed(1)}}%</b></td>
    <td>${{verdict}}</td>
  </tr>`;
}});
</script>
</body>
</html>"""

    out_path = os.path.abspath("pip_move_analysis.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n[HTML] Saved → {out_path}")
    try:
        webbrowser.open(f"file:///{out_path}")
    except Exception:
        pass


# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Pip move analysis from 00:00 UTC start price",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pip_move_analysis.py --months 10
  python pip_move_analysis.py --from-date 2025-06-01 --to-date 2026-04-05
  python pip_move_analysis.py --months 3 --session-start 01:00 --session-end 20:00
  python pip_move_analysis.py --months 10 --symbol XAUUSD --data-dir data
        """
    )
    p.add_argument("--months",        type=int,   default=3)
    p.add_argument("--symbol",        type=str,   default="XAUUSD")
    p.add_argument("--from-date",     type=str,   default="")
    p.add_argument("--to-date",       type=str,   default="")
    p.add_argument("--session-start", type=str,   default="00:00",
                   help="Only count moves within this UTC session window (default 00:00)")
    p.add_argument("--session-end",   type=str,   default="23:00",
                   help="Session end UTC (default 23:00)")
    p.add_argument("--data-dir",      type=str,   default="",
                   help="data/ folder with day JSON files for exact start prices")
    p.add_argument("--no-html",       action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    if not MT5_AVAILABLE:
        print("❌ pip install MetaTrader5")
        sys.exit(1)

    # ── Fetch bars ────────────────────────────────────────────────────────────
    from_date = args.from_date.strip()
    to_date   = args.to_date.strip()

    try:
        if from_date or to_date:
            to_dt   = (datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                       if to_date else datetime.now(timezone.utc))
            from_dt = (datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                       if from_date else to_dt - timedelta(days=args.months * 31))
            bars = fetch_bars_range(args.symbol, from_dt, to_dt)
        else:
            bars = fetch_bars(args.symbol, args.months)
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    day_map = group_by_day(bars)
    print(f"[Analysis] {len(day_map)} trading days\n")

    # ── Analyse each day ──────────────────────────────────────────────────────
    days_out = []
    skipped  = 0

    for date, day_bars in day_map.items():
        # Get start price
        start_price = 0.0
        lock_hhmm   = "00:00"

        if args.data_dir:
            result = load_start_from_day_file(date, args.data_dir, args.symbol)
            if result:
                start_price, lock_hhmm = result

        if start_price == 0.0:
            # Fallback: first bar at or after session_start
            for b in day_bars:
                hhmm = b["time"].strftime("%H:%M")
                if hhmm >= args.session_start:
                    start_price = b["open"]
                    lock_hhmm   = hhmm
                    break

        if start_price == 0.0:
            skipped += 1
            continue

        result = analyze_day(
            date         = date,
            bars         = day_bars,
            start_price  = start_price,
            session_start = args.session_start,
            session_end  = args.session_end,
        )
        if result:
            result["lock_hhmm"] = lock_hhmm
            days_out.append(result)
        else:
            skipped += 1

    if skipped:
        print(f"[Analysis] Skipped {skipped} days (no data / no start price)")

    if not days_out:
        print("❌ No days to analyse.")
        sys.exit(1)

    # ── Stats + report ────────────────────────────────────────────────────────
    stats = build_stats(days_out)
    print_report(days_out, stats, args.symbol, args.session_start, args.session_end)

    if not args.no_html:
        generate_html(days_out, stats, args.symbol, args)


if __name__ == "__main__":
    main()