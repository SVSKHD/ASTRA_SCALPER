"""
XAUUSD Realistic Market Data Generator
======================================
Generates realistic XAUUSD price data with:
  - Session-based volatility (London / NY / Asian)
  - Trending regimes (bull/bear/range)
  - News spikes (NFP, CPI, FOMC style)
  - Momentum + mean reversion mix
  - Realistic spread behaviour
  - Weekend gaps

Outputs:
  ticks.csv        — date, time, bid, ask
  XAUUSD_M5.csv   — date, time, open, high, low, close, volume
  XAUUSD_M1.csv   — date, time, open, high, low, close, volume

Run:
  python data_generator.py
  python data_generator.py --months 6 --start 2025-01-02 --seed 42
"""

import csv
import math
import random
import argparse
from datetime import datetime, timedelta

# ── CONFIG ───────────────────────────────────────────────────────────────────
DEFAULT_MONTHS    = 8
DEFAULT_START     = "2025-01-02"
DEFAULT_SEED      = 42
BASE_PRICE        = 2020.0

# Session hours (UTC)
SESSIONS = {
    "asian":  (0,  7,  0.25),   # (start_hr, end_hr, volatility_mult)
    "london": (7,  13, 1.0),
    "ny":     (13, 20, 0.85),
    "closed": (20, 24, 0.08),
}

# Regime config
REGIMES = {
    "bull":  {"drift": +0.004, "vol": 0.18, "weight": 0.35},
    "bear":  {"drift": -0.003, "vol": 0.20, "weight": 0.25},
    "range": {"drift":  0.000, "vol": 0.10, "weight": 0.40},
}

# News event simulation (fires ~2x/month, intraday spike)
NEWS_SPIKE_PIPS   = (15, 60)    # pip range for news candle
NEWS_REVERSAL_PCT = 0.55        # % of spike that reverts after


# ── HELPERS ──────────────────────────────────────────────────────────────────
def session_vol_mult(hour):
    for name, (s, e, mult) in SESSIONS.items():
        if s <= hour < e:
            return mult
    return 0.08


def pick_regime(rng):
    r = rng.random()
    cum = 0
    for name, cfg in REGIMES.items():
        cum += cfg["weight"]
        if r < cum:
            return name, cfg
    return "range", REGIMES["range"]


def spread(price, rng, session_mult):
    base = 0.25 + rng.random() * 0.15
    return round(base * (1 + (1 - session_mult) * 0.5), 2)


# ── TICK GENERATOR ───────────────────────────────────────────────────────────
def generate_ticks(start_date, num_days, rng):
    """Yield (datetime, bid, ask) tuples."""
    price   = BASE_PRICE
    regime  = "range"
    reg_cfg = REGIMES["range"]
    regime_days_left = rng.randint(3, 12)

    # News events: set of (day_index, hour) pairs
    news_events = set()
    for d in range(num_days):
        if rng.random() < 0.09:   # ~2 per month
            news_events.add((d, rng.randint(13, 18)))

    day_idx = 0
    dt = start_date

    while day_idx < num_days:
        # Skip weekends
        if dt.weekday() >= 5:
            dt += timedelta(days=1)
            continue

        # Weekend gap on Monday open
        if dt.weekday() == 0:
            gap = rng.gauss(0, 1.5)
            price = round(price + gap, 2)

        # Regime switch
        if regime_days_left <= 0:
            regime, reg_cfg = pick_regime(rng)
            regime_days_left = rng.randint(3, 15)
        regime_days_left -= 1

        # Walk through seconds 0:00–23:59
        t = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = t + timedelta(hours=24)
        news_hour = None
        if day_idx in {ev[0] for ev in news_events}:
            news_hour = next(ev[1] for ev in news_events if ev[0] == day_idx)

        news_fired = False

        while t < day_end:
            hour = t.hour
            sv = session_vol_mult(hour)

            # News spike
            if news_hour and hour == news_hour and not news_fired:
                direction = rng.choice([-1, 1])
                spike_pips = rng.randint(*NEWS_SPIKE_PIPS)
                price += direction * spike_pips
                price = round(max(1800, min(2400, price)), 2)
                sprd = spread(price, rng, sv) * 3
                yield (t, round(price, 2), round(price + sprd, 2))
                t += timedelta(seconds=1)
                # Partial reversal over next 30 ticks
                reversal = direction * spike_pips * NEWS_REVERSAL_PCT
                for _ in range(30):
                    price += reversal / 30 + rng.gauss(0, 0.05)
                    price = round(max(1800, min(2400, price)), 2)
                    sprd = spread(price, rng, sv)
                    yield (t, round(price, 2), round(price + sprd, 2))
                    t += timedelta(seconds=rng.randint(1, 4))
                news_fired = True
                continue

            # Normal tick
            if sv < 0.1:
                # Closed session — very few ticks
                interval = rng.randint(30, 120)
            elif sv < 0.5:
                interval = rng.randint(5, 20)
            else:
                interval = rng.randint(1, 6)

            vol     = reg_cfg["vol"] * sv
            drift   = reg_cfg["drift"] * sv
            move    = rng.gauss(drift, vol)

            # Mean reversion pull when far from session open
            if abs(price - BASE_PRICE) > 30:
                move += (BASE_PRICE - price) * 0.002

            price = round(max(1800, min(2400, price + move)), 2)
            sprd  = spread(price, rng, sv)
            yield (t, round(price, 2), round(price + sprd, 2))
            t += timedelta(seconds=interval)

        dt += timedelta(days=1)
        day_idx += 1


# ── BAR AGGREGATOR ───────────────────────────────────────────────────────────
def ticks_to_bars(tick_iter, interval_minutes):
    """Stream ticks → OHLCV bars of given interval."""
    bars = []
    o = h = l = c = None
    vol = 0
    bar_start = None
    delta = timedelta(minutes=interval_minutes)

    for dt, bid, ask in tick_iter:
        mid = round((bid + ask) / 2, 3)
        if bar_start is None:
            bar_start = dt.replace(second=0, microsecond=0)
            bar_start -= timedelta(minutes=bar_start.minute % interval_minutes)
            o = h = l = c = mid

        if dt >= bar_start + delta:
            bars.append({"dt": bar_start, "open": o, "high": h,
                         "low": l, "close": c, "volume": vol})
            bar_start = bar_start + delta
            # fast-forward if gap
            while dt >= bar_start + delta:
                bar_start += delta
            o = h = l = c = mid
            vol = 0
        else:
            h = max(h, mid)
            l = min(l, mid)
            c = mid
            vol += 1

    if o is not None:
        bars.append({"dt": bar_start, "open": o, "high": h,
                     "low": l, "close": c, "volume": vol})
    return bars


# ── WRITERS ──────────────────────────────────────────────────────────────────
def write_ticks(filepath, ticks):
    count = 0
    with open(filepath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "time", "bid", "ask"])
        for dt, bid, ask in ticks:
            w.writerow([dt.strftime("%Y.%m.%d"), dt.strftime("%H:%M:%S"), bid, ask])
            count += 1
    return count


def write_bars(filepath, bars):
    with open(filepath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "time", "open", "high", "low", "close", "volume"])
        w.writeheader()
        for b in bars:
            w.writerow({
                "date":   b["dt"].strftime("%Y.%m.%d"),
                "time":   b["dt"].strftime("%H:%M"),
                "open":   b["open"],
                "high":   b["high"],
                "low":    b["low"],
                "close":  b["close"],
                "volume": b["volume"],
            })


# ── STATS PREVIEW ─────────────────────────────────────────────────────────────
def preview_stats(bars_m5):
    if not bars_m5:
        return
    closes    = [b["close"] for b in bars_m5]
    pip_moves = [abs(b["high"] - b["low"]) for b in bars_m5 if b["high"] - b["low"] > 0]
    daily_ranges = {}
    for b in bars_m5:
        d = b["dt"].date()
        if d not in daily_ranges:
            daily_ranges[d] = {"high": b["high"], "low": b["low"]}
        else:
            daily_ranges[d]["high"] = max(daily_ranges[d]["high"], b["high"])
            daily_ranges[d]["low"]  = min(daily_ranges[d]["low"],  b["low"])

    daily_pips = [v["high"] - v["low"] for v in daily_ranges.values()]
    sep = "─" * 52
    print(f"\n{sep}")
    print(f"  DATA PREVIEW")
    print(sep)
    print(f"  M5 bars generated    : {len(bars_m5):,}")
    print(f"  Trading days         : {len(daily_ranges)}")
    print(f"  Price range          : {min(closes):.2f} – {max(closes):.2f}")
    print(f"  Avg daily range      : {sum(daily_pips)/len(daily_pips):.1f} pips")
    print(f"  Max daily range      : {max(daily_pips):.1f} pips")
    print(f"  Min daily range      : {min(daily_pips):.1f} pips")
    print(f"  Avg M5 candle range  : {sum(pip_moves)/len(pip_moves):.2f} pips")
    print(sep)


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="XAUUSD Realistic Data Generator")
    parser.add_argument("--months", type=int,   default=DEFAULT_MONTHS)
    parser.add_argument("--start",  type=str,   default=DEFAULT_START)
    parser.add_argument("--seed",   type=int,   default=DEFAULT_SEED)
    parser.add_argument("--outdir", type=str,   default=".")
    parser.add_argument("--no-ticks", action="store_true", help="Skip tick CSV (faster)")
    args = parser.parse_args()

    import os
    os.makedirs(args.outdir, exist_ok=True)

    rng        = random.Random(args.seed)
    start_date = datetime.strptime(args.start, "%Y-%m-%d")
    num_days   = args.months * 31   # overestimate, weekends skipped inside

    print(f"  Generating {args.months} months of XAUUSD data from {args.start}...")
    print(f"  Seed: {args.seed}  |  Base price: {BASE_PRICE}")

    # --- Generate ticks (stream, two passes for M1/M5)
    # Pass 1 — M5 bars
    print("  Building M5 bars...")
    m5_path = os.path.join(args.outdir, "XAUUSD_M5.csv")
    bars_m5 = ticks_to_bars(generate_ticks(start_date, num_days, rng), interval_minutes=5)
    write_bars(m5_path, bars_m5)
    print(f"  Wrote {len(bars_m5):,} M5 bars → {m5_path}")

    # Pass 2 — M1 bars (fresh rng same seed)
    print("  Building M1 bars...")
    rng2    = random.Random(args.seed)
    m1_path = os.path.join(args.outdir, "XAUUSD_M1.csv")
    bars_m1 = ticks_to_bars(generate_ticks(start_date, num_days, rng2), interval_minutes=1)
    write_bars(m1_path, bars_m1)
    print(f"  Wrote {len(bars_m1):,} M1 bars → {m1_path}")

    # Pass 3 — Tick CSV (optional, large file)
    if not args.no_ticks:
        print("  Writing tick CSV (large file, use --no-ticks to skip)...")
        rng3      = random.Random(args.seed)
        tick_path = os.path.join(args.outdir, "ticks.csv")
        tick_count = write_ticks(tick_path, generate_ticks(start_date, num_days, rng3))
        print(f"  Wrote {tick_count:,} ticks → {tick_path}")

    preview_stats(bars_m5)
    print(f"\n  Run analysis:")
    print(f"    python analyse_real_data.py --file {m5_path} --type ohlcv --outdir {args.outdir}/analysis")
    print(f"    python analyse_real_data.py --file {tick_path if not args.no_ticks else 'ticks.csv'} --type tick --outdir {args.outdir}/analysis\n")


if __name__ == "__main__":
    main()