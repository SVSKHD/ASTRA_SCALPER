from __future__ import annotations

# =============================================================================
# SIGNAL LOGGER — Feature logger for ML training data collection
#
# Every time a signal fires (whether traded or skipped), this module logs
# ALL features to data/signal_log.csv with the outcome written later.
#
# USAGE:
#   # In runner.py after signal fires:
#   from signal_logger import log_signal, update_outcome
#
#   log_id = log_signal(signal, bars_m5, state, cfg)
#   # ... trade executes ...
#   update_outcome(log_id, "TP")  # or "SL" or "FC"
#
# CSV COLUMNS:
#   id, date, time_utc, direction, start_price, entry_price,
#   # Price features
#   breakout_bar_body_ratio,   # body / total range (0.0-1.0). >0.6 = strong
#   breakout_bar_range_pips,   # high-low of breakout bar in pips
#   atr14,                     # ATR(14) at signal time
#   atr14_vs_20d_avg,          # current ATR / 20-day avg ATR (>1.5 = volatile)
#   price_vs_20d_mean,         # (price - 20d mean) / 20d mean × 100
#   # Session features
#   hour_utc,                  # 0-23
#   day_of_week,               # 0=Mon 4=Fri
#   # Context features
#   prev_day_outcome,          # TP / SL / NO_SIGNAL / UNKNOWN
#   consecutive_losses,        # 0,1,2,3... losses in a row
#   h1_trend_align,            # 1=trend aligned, 0=counter-trend
#   spread_pips,               # spread at signal time
#   # Outcome (filled in after trade closes)
#   outcome,                   # TP / SL / FC / SKIPPED / PENDING
#   pnl_net,                   # actual net P&L
#   filter_applied,            # which filter blocked it (empty = traded)
# =============================================================================

import csv
import os
import time
from datetime import datetime, timezone
from typing import Optional
import logging

log = logging.getLogger("signal_logger")

_CSV_PATH = os.path.join("data", "signal_log.csv")
_FIELDNAMES = [
    "id", "date", "time_utc", "direction",
    "start_price", "entry_price", "tp_price", "sl_price",
    # Price structure
    "breakout_bar_body_ratio", "breakout_bar_range_pips",
    "atr14", "atr14_vs_20d_avg",
    "price_vs_20d_mean",
    # Session
    "hour_utc", "day_of_week",
    # Context
    "prev_day_outcome", "consecutive_losses",
    "h1_trend_align", "spread_pips",
    # Outcome
    "outcome", "pnl_net", "filter_applied",
]

_pending: dict[str, dict] = {}   # id → row dict (for outcome update)


def _ensure_csv():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(_CSV_PATH):
        with open(_CSV_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writeheader()
        print(f"[SignalLogger] Created {_CSV_PATH}")


def _make_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:20]


def _compute_features(signal, bars_m5: list, spread_pips: float,
                       prev_day_outcome: str, consecutive_losses: int,
                       h1_trend_align: int) -> dict:
    """
    Compute features from available M5 bars.

    bars_m5: list of dicts with keys: open, high, low, close, volume
             Most recent bar = bars_m5[-1]
             At least 14 bars needed for ATR.
    """
    features = {
        "breakout_bar_body_ratio": 0.0,
        "breakout_bar_range_pips": 0.0,
        "atr14": 0.0,
        "atr14_vs_20d_avg": 1.0,
        "price_vs_20d_mean": 0.0,
        "spread_pips": round(spread_pips, 2),
        "prev_day_outcome": prev_day_outcome,
        "consecutive_losses": consecutive_losses,
        "h1_trend_align": h1_trend_align,
    }

    if not bars_m5 or len(bars_m5) < 2:
        return features

    # ── Breakout bar features (last completed bar) ────────────────────────
    bar = bars_m5[-2]   # -1 is still forming, -2 is the closed breakout bar
    bar_range = bar["high"] - bar["low"]
    bar_body  = abs(bar["close"] - bar["open"])

    if bar_range > 0:
        features["breakout_bar_body_ratio"] = round(bar_body / bar_range, 3)
        features["breakout_bar_range_pips"]  = round(bar_range, 2)

    # ── ATR(14) ───────────────────────────────────────────────────────────
    if len(bars_m5) >= 15:
        true_ranges = []
        for i in range(1, 15):
            b   = bars_m5[-(i+1)]
            b_p = bars_m5[-(i+2)] if len(bars_m5) > i+1 else b
            tr  = max(
                b["high"] - b["low"],
                abs(b["high"] - b_p["close"]),
                abs(b["low"]  - b_p["close"]),
            )
            true_ranges.append(tr)
        atr14 = sum(true_ranges) / len(true_ranges)
        features["atr14"] = round(atr14, 4)

    # ── ATR vs 20-day average (need ~20d × 288 bars = 5760 bars for proper calc)
    # Simplified: compare last 14 bars ATR vs bars 15-28 ATR
    if len(bars_m5) >= 29:
        older_trs = []
        for i in range(15, 29):
            b   = bars_m5[-(i+1)]
            b_p = bars_m5[-(i+2)] if len(bars_m5) > i+1 else b
            tr  = max(
                b["high"] - b["low"],
                abs(b["high"] - b_p["close"]),
                abs(b["low"]  - b_p["close"]),
            )
            older_trs.append(tr)
        older_atr = sum(older_trs) / len(older_trs)
        if older_atr > 0 and features["atr14"] > 0:
            features["atr14_vs_20d_avg"] = round(features["atr14"] / older_atr, 3)

    # ── Price vs 20-period mean ───────────────────────────────────────────
    if len(bars_m5) >= 20:
        closes = [b["close"] for b in bars_m5[-20:]]
        mean_20 = sum(closes) / 20
        if mean_20 > 0:
            features["price_vs_20d_mean"] = round(
                (signal.entry_price - mean_20) / mean_20 * 100, 4
            )

    return features


def log_signal(
    signal,
    bars_m5: list,
    spread_pips: float = 0.0,
    prev_day_outcome: str = "UNKNOWN",
    consecutive_losses: int = 0,
    h1_trend_align: int = 1,
    filter_applied: str = "",
    outcome: str = "PENDING",
) -> str:
    """
    Log a signal with all features. Returns log_id for later outcome update.

    Call this BEFORE deciding to trade or skip.
    Call update_outcome(log_id, "TP"/"SL") after trade closes.
    If filter blocked the trade, pass filter_applied="atr_volatile" etc.
    """
    _ensure_csv()

    now = datetime.now(timezone.utc)
    log_id = _make_id()

    features = _compute_features(
        signal, bars_m5, spread_pips,
        prev_day_outcome, consecutive_losses, h1_trend_align
    )

    row = {
        "id":           log_id,
        "date":         now.strftime("%Y-%m-%d"),
        "time_utc":     now.strftime("%H:%M:%S"),
        "direction":    signal.direction,
        "start_price":  signal.start_price,
        "entry_price":  signal.entry_price,
        "tp_price":     signal.tp_price,
        "sl_price":     signal.sl_price,
        "outcome":      outcome,
        "pnl_net":      "",
        "filter_applied": filter_applied,
        **features,
    }

    with open(_CSV_PATH, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=_FIELDNAMES).writerow(row)

    _pending[log_id] = row
    log.info(f"[SignalLogger] Logged signal {log_id} | {signal.direction} | filter={filter_applied or 'none'}")
    return log_id


def update_outcome(log_id: str, outcome: str, pnl_net: float = 0.0):
    """
    Update the outcome of a previously logged signal.
    Rewrites the CSV row in place.

    outcome: "TP" | "SL" | "FC" | "SKIPPED"
    """
    if log_id not in _pending:
        log.warning(f"[SignalLogger] update_outcome: id {log_id} not in pending")
        return

    _pending[log_id]["outcome"] = outcome
    _pending[log_id]["pnl_net"] = round(pnl_net, 2)

    # Rewrite CSV with updated row
    try:
        rows = []
        with open(_CSV_PATH, "r", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r["id"] == log_id:
                    r["outcome"] = outcome
                    r["pnl_net"] = str(round(pnl_net, 2))
                rows.append(r)

        with open(_CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

        log.info(f"[SignalLogger] Updated {log_id} → outcome={outcome} pnl={pnl_net:+.2f}")
        del _pending[log_id]

    except Exception as e:
        log.error(f"[SignalLogger] Failed to update outcome: {e}")


def get_stats() -> dict:
    """Quick stats from the log file. Useful for morning briefing."""
    if not os.path.exists(_CSV_PATH):
        return {}
    rows = []
    with open(_CSV_PATH, "r", newline="") as f:
        rows = list(csv.DictReader(f))

    total   = len(rows)
    traded  = [r for r in rows if r["outcome"] in ("TP", "SL", "FC")]
    tp      = [r for r in traded if r["outcome"] == "TP"]
    sl      = [r for r in traded if r["outcome"] == "SL"]
    skipped = [r for r in rows if r["filter_applied"]]
    wr      = round(len(tp) / len(traded) * 100, 1) if traded else 0.0

    return {
        "total_signals": total,
        "traded": len(traded),
        "tp": len(tp),
        "sl": len(sl),
        "skipped": len(skipped),
        "win_rate": wr,
    }


if __name__ == "__main__":
    print(f"Signal log: {_CSV_PATH}")
    stats = get_stats()
    if stats:
        print(f"  Total signals : {stats['total_signals']}")
        print(f"  Traded        : {stats['traded']}")
        print(f"  TP            : {stats['tp']}")
        print(f"  SL            : {stats['sl']}")
        print(f"  Skipped       : {stats['skipped']}")
        print(f"  Win rate      : {stats['win_rate']}%")
    else:
        print("  No data yet.")