from __future__ import annotations

# =============================================================================
# TRADE SIGNAL — renamed from signal.py to avoid Python stdlib collision
# Pure logic. No MT5. No I/O.
#
# FIX: signal.py collides with Python's built-in signal module.
# All imports must use: from trade_signal import Signal, evaluate_signal
# =============================================================================

from dataclasses import dataclass
from typing import Literal

from config import cfg, StrategyConfig
from threshold import ThresholdLevels

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class Signal:
    direction:   Direction
    entry_price: float
    tp_price:    float
    sl_price:    float
    start_price: float

    def __str__(self) -> str:
        capture = (
            self.tp_price - self.entry_price if self.direction == "LONG"
            else self.entry_price - self.tp_price
        )
        return (
            f"SIGNAL {self.direction} | "
            f"Entry={self.entry_price:.2f} | "
            f"TP={self.tp_price:.2f} | "
            f"SL={self.sl_price:.2f} | "
            f"Capture=${capture:.2f} | "
            f"Start={self.start_price:.2f}"
        )


def evaluate_signal(
    mid: float,
    levels: ThresholdLevels,
    already_traded: set[Direction],
    strategy_cfg: StrategyConfig = cfg,
) -> Signal | None:
    """
    Evaluate whether current mid price triggers a long or short entry.

    Rules:
      1. direction_mode=first_only → once any trade placed, return None
      2. Never re-enter a direction already traded today
      3. Overshoot filter: reject if mid is more than max_entry_overshoot_pips
         beyond the entry level — price has moved too far, fill would be stale

    Overshoot filter examples (long):
      mid = 4535.0 → entry = 4535.0 → overshoot = 0.0 → ALLOW
      mid = 4537.5 → entry = 4535.0 → overshoot = 2.5 → ALLOW (< 3.0)
      mid = 4539.0 → entry = 4535.0 → overshoot = 4.0 → REJECT (> 3.0)
    """
    mode     = strategy_cfg.direction_mode
    overshoot = strategy_cfg.max_entry_overshoot_pips

    # direction_mode = first_only: one trade per day max
    if mode == "first_only" and already_traded:
        return None

    long_ok  = "LONG"  not in already_traded
    short_ok = "SHORT" not in already_traded

    # ── LONG ──────────────────────────────────────────────────────────────
    if long_ok and mid >= levels.long_entry:
        # overshoot check: reject if price has run too far past entry
        if mid - levels.long_entry > overshoot:
            return None   # stale — do not chase
        return Signal(
            direction   = "LONG",
            entry_price = levels.long_entry,
            tp_price    = levels.long_tp,
            sl_price    = levels.long_sl,
            start_price = levels.start,
        )

    # ── SHORT ─────────────────────────────────────────────────────────────
    if short_ok and mid <= levels.short_entry:
        # overshoot check
        if levels.short_entry - mid > overshoot:
            return None   # stale — do not chase
        return Signal(
            direction   = "SHORT",
            entry_price = levels.short_entry,
            tp_price    = levels.short_tp,
            sl_price    = levels.short_sl,
            start_price = levels.start,
        )

    return None


if __name__ == "__main__":
    from threshold import compute_levels

    levels = compute_levels(4513.0)
    print(levels.display())

    tests = [
        (4535.0, "LONG  exact entry"),
        (4537.0, "LONG  within overshoot"),
        (4539.0, "LONG  overshoot rejected"),
        (4491.0, "SHORT exact entry"),
        (4489.0, "SHORT within overshoot"),
        (4487.0, "SHORT overshoot rejected"),
        (4513.0, "No signal — at start"),
    ]
    for mid, desc in tests:
        sig = evaluate_signal(mid, levels, set())
        status = str(sig) if sig else "NO SIGNAL"
        print(f"  mid={mid:.2f}  [{desc}] → {status}")
