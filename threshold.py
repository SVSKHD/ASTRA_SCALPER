from __future__ import annotations

# =============================================================================
# THRESHOLD LEVELS — pure math, no MT5, no I/O
# All offsets derived from config. Never hardcoded.
#
# For start price S:
#   long_breakout = S + 20   (1.0× — do not enter)
#   long_entry    = S + 22   (1.1× — trigger)
#   long_tp       = S + 24   (1.2× — true threshold exit)
#   long_sl       = S + 20   (back at 1.0× breakout)
# =============================================================================

from dataclasses import dataclass
from config import cfg, StrategyConfig


@dataclass(frozen=True)
class ThresholdLevels:
    start: float

    # Long
    long_breakout: float     # 1.0× — confirmation, not entry
    long_entry:    float     # 1.1× — entry trigger
    long_tp:       float     # 1.2× — take profit
    long_sl:       float     # 1.0× — stop loss (same as breakout)

    # Short
    short_breakout: float
    short_entry:    float
    short_tp:       float
    short_sl:       float

    def display(self) -> str:
        return (
            f"\n{'─'*50}\n"
            f"  START PRICE      : {self.start:.2f}\n"
            f"{'─'*50}\n"
            f"  LONG\n"
            f"    Breakout (1.0×): {self.long_breakout:.2f}   ← do not enter\n"
            f"    Entry    (1.1×): {self.long_entry:.2f}   ← trigger\n"
            f"    Exit     (1.2×): {self.long_tp:.2f}   ← take profit\n"
            f"    Stop Loss      : {self.long_sl:.2f}   ← back at breakout\n"
            f"    Capture        : ${self.long_tp - self.long_entry:.2f} per pip per lot\n"
            f"{'─'*50}\n"
            f"  SHORT\n"
            f"    Breakout (1.0×): {self.short_breakout:.2f}   ← do not enter\n"
            f"    Entry    (1.1×): {self.short_entry:.2f}   ← trigger\n"
            f"    Exit     (1.2×): {self.short_tp:.2f}   ← take profit\n"
            f"    Stop Loss      : {self.short_sl:.2f}   ← back at breakout\n"
            f"    Capture        : ${self.short_entry - self.short_tp:.2f} per pip per lot\n"
            f"{'─'*50}\n"
        )


def compute_levels(
    start_price: float,
    strategy_cfg: StrategyConfig = cfg,
) -> ThresholdLevels:
    """
    Compute all threshold price levels from the locked start price.
    Uses entry_offset (1.1×) and exit_offset (1.2×) from config.
    """
    s  = start_price
    b  = strategy_cfg.breakout_offset   # 20.0
    e  = strategy_cfg.entry_offset      # 22.0
    x  = strategy_cfg.exit_offset       # 24.0

    return ThresholdLevels(
        start=s,

        long_breakout = round(s + b, 2),
        long_entry    = round(s + e, 2),
        long_tp       = round(s + x, 2),
        long_sl       = round(s + b, 2),   # SL = breakout level

        short_breakout = round(s - b, 2),
        short_entry    = round(s - e, 2),
        short_tp       = round(s - x, 2),
        short_sl       = round(s - b, 2),  # SL = breakout level
    )


if __name__ == "__main__":
    for start in [4513.0, 4384.0, 4517.0]:
        print(compute_levels(start).display())
