from __future__ import annotations

# =============================================================================
# THRESHOLD LEVELS — pure math, no MT5, no I/O
# All offsets derived from config. Never hardcoded.
#
# For start price S (active config: entry_mult=1.25, exit_mult=2.0):
#   long_breakout = S + 20   (1.0× — SL anchor)
#   long_entry    = S + 25   (1.25× — trigger, 5 pips above breakout)
#   long_tp       = S + 40   (2.0×  — take profit, 15 pips above entry)
#   long_sl       = S + 20   (back at breakout, 5 pips below entry)
# =============================================================================

from dataclasses import dataclass
from config import cfg, StrategyConfig


@dataclass(frozen=True)
class ThresholdLevels:
    start: float

    # Long
    long_breakout: float     # 1.0× — confirmation, not entry
    long_entry:    float     # entry_multiplier× — entry trigger
    long_tp:       float     # exit_multiplier×  — take profit
    long_sl:       float     # 1.0× — stop loss (same as breakout)

    # Short
    short_breakout: float
    short_entry:    float
    short_tp:       float
    short_sl:       float

    def display(self) -> str:
        from config import cfg as _cfg
        c = _cfg
        return (
            f"\n{'─'*50}\n"
            f"  START PRICE      : {self.start:.2f}\n"
            f"{'─'*50}\n"
            f"  LONG\n"
            f"    Breakout (1.0×)  : {self.long_breakout:.2f}   ← SL anchor\n"
            f"    Entry  ({c.entry_multiplier}×) : {self.long_entry:.2f}   ← trigger (+{c.entry_offset:.0f} pips)\n"
            f"    TP     ({c.exit_multiplier}×)  : {self.long_tp:.2f}   ← take profit (+{c.exit_offset:.0f} pips)\n"
            f"    SL              : {self.long_sl:.2f}   ← {c.sl_pips:.0f} pips below entry\n"
            f"    Risk / Reward   : ${c.sl_dollar:.0f} SL  /  ${c.tp_dollar:.0f} TP  | lot={c.lot_size}\n"
            f"{'─'*50}\n"
            f"  SHORT\n"
            f"    Breakout (1.0×)  : {self.short_breakout:.2f}   ← SL anchor\n"
            f"    Entry  ({c.entry_multiplier}×) : {self.short_entry:.2f}   ← trigger (-{c.entry_offset:.0f} pips)\n"
            f"    TP     ({c.exit_multiplier}×)  : {self.short_tp:.2f}   ← take profit (-{c.exit_offset:.0f} pips)\n"
            f"    SL              : {self.short_sl:.2f}   ← {c.sl_pips:.0f} pips above entry\n"
            f"    Risk / Reward   : ${c.sl_dollar:.0f} SL  /  ${c.tp_dollar:.0f} TP  | lot={c.lot_size}\n"
            f"{'─'*50}\n"
        )


def compute_levels(
    start_price: float,
    strategy_cfg: StrategyConfig = cfg,
) -> ThresholdLevels:
    """
    Compute all threshold price levels from the locked start price.
    Uses entry_offset (1.25× = S±25) and exit_offset (2.0× = S±40) from config.
    """
    s  = start_price
    b  = strategy_cfg.breakout_offset   # 20.0 (1.0×)
    e  = strategy_cfg.entry_offset      # 25.0 (1.25×)
    x  = strategy_cfg.exit_offset       # 40.0 (2.0×)

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