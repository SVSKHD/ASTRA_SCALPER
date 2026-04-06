from __future__ import annotations

# =============================================================================
# THRESHOLD STRATEGY — MASTER CONFIG
#
# ── CHANGE LOG ────────────────────────────────────────────────────────────────
# v1.5 (Apr 2026):
#   threshold_pips   : 19  → 20    (round observe level)
#   entry_multiplier : 1.263158 → 1.1   (22 pips — enter earlier, less overshoot)
#   exit_multiplier  : 2.052632 → 1.8   (36 pips — inside avg daily range)
#   lot_size auto    : 0.4 → 1.0   (follows from 2-pip SL buffer × $100/pip)
#   TP per win       : $600 → $1,400
#   Breakeven WR     : 25% → 12.5% (need 1 win in 8)
#
# WHY 1.1× ENTRY:
#   Close-confirm delays execution by 1 M5 bar. At 24-pip entry the bar closes
#   28-30 pips in, next bar opens 30-32 pips in → overshoot = 6-8 pips → BLOCKED.
#   At 22-pip entry the bar closes 24-25 pips in → overshoot = 2-3 pips → EXECUTES.
#   2 pips earlier = dramatically more trades actually firing.
#
# WHY 1.8× TP (36 pips):
#   Previous 39-pip TP was above average daily range. Most days: enter at 24,
#   price runs to 32, reverses → SL. New 36-pip TP is within median range.
#   Gold median dominant move from start: 28–35 pips. TP at 36 catches
#   above-average days and forces no TP on below-average days (SL instead).
#
# EFFECTIVE SL BUFFER:
#   Nominal: 2 pips (entry_offset 22 − threshold 20)
#   Actual at close-confirm fill: 4–6 pips (fill is always 2-4 pips past entry)
#   Dollar SL at fill: ~$400–600 (1.0 lot × 4-6 pips × $100)
#   Daily loss limit $200 still prevents new trades if first trade loses big.
#
# R:R = 1:7  |  Breakeven WR = 12.5%  |  Need 1 win in 8 to be profitable.
# =============================================================================

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class StrategyConfig:

    # ── ACCOUNT ───────────────────────────────────────────
    account_size: float = 50_000.0

    # ── SYMBOL ────────────────────────────────────────────
    symbol: str = "XAUUSD"

    # ── THRESHOLD DEFINITION ──────────────────────────────
    # "20 observe, 1.1 entry, 1.8 close"
    threshold_pips:    float = 20.0      # watch level (SL anchor)
    entry_multiplier:  float = 1.1       # enter at 22 pips (was 1.263158 → 24 pips)
    exit_multiplier:   float = 1.8       # TP at 36 pips (was 2.052632 → 39 pips)
    #
    # For 2.0× TP ($1,800 per win): change exit_multiplier to 2.0
    # and tp_dollar_target / daily_profit_target_usd to 1800.0

    # ── DOLLAR TARGETS ────────────────────────────────────
    sl_dollar_target: float = 200.0    # $200 SL → lot = 200/(2×100) = 1.0
    tp_dollar_target: float = 1400.0   # $1,400 TP (14 pips × $100 × 1.0 lot)

    # ── DAILY LIMITS ──────────────────────────────────────
    daily_profit_target_usd: float = 1400.0  # stop after 1 TP hit (+$1,400)
    max_daily_loss_usd:       float = 200.0   # stop after 1 nominal SL (−$200)

    # ── TRADE LIMITS ──────────────────────────────────────
    max_trades_per_day: int = 1
    direction_mode: Literal["first_only", "both"] = "both"

    # ── ENTRY OVERSHOOT FILTER ────────────────────────────
    # Runner overrides this with _MAX_OVERSHOOT_PIPS = 8.0 at execution time.
    # This value is used only by the backtest. Keep at 8.0 for consistency.
    max_entry_overshoot_pips: float = 8.0

    # ── NEWS BLACKOUT DAYS ────────────────────────────────
    news_blackout_dates: list = field(default_factory=list)

    # ── SESSION FILTER (all UTC) ──────────────────────────
    session_start_hhmm:    str = "00:00"
    session_end_hhmm:      str = "23:00"
    force_close_hhmm:      str = "23:30"
    news_blackout_minutes: int = 15

    # ── PATHS ─────────────────────────────────────────────
    base_dir: str = "data"

    # ── MT5 CLOCK ─────────────────────────────────────────
    server_utc_offset_hours: int = 0

    # ── MT5 ORDER ─────────────────────────────────────────
    magic_number:     int   = 20260401
    deviation_points: int   = 10
    order_comment:    str   = "threshold_xau"
    poll_seconds:     float = 0.3

    # =========================================================
    # DERIVED  (auto-calculated — do not set manually)
    # =========================================================

    @property
    def pip_value_per_lot(self) -> float:
        return 100.0

    @property
    def breakout_offset(self) -> float:
        return self.threshold_pips                                    # 20.0

    @property
    def entry_offset(self) -> float:
        return round(self.threshold_pips * self.entry_multiplier, 2)  # 22.0

    @property
    def exit_offset(self) -> float:
        return round(self.threshold_pips * self.exit_multiplier, 2)   # 36.0

    @property
    def sl_pips(self) -> float:
        return round(self.entry_offset - self.breakout_offset, 2)     # 2.0

    @property
    def tp_pips(self) -> float:
        return round(self.exit_offset - self.entry_offset, 2)         # 14.0

    @property
    def lot_size(self) -> float:
        return round(
            self.sl_dollar_target / (self.sl_pips * self.pip_value_per_lot), 2
        )  # 200 / (2 × 100) = 1.0

    @property
    def sl_dollar(self) -> float:
        return round(self.sl_pips * self.pip_value_per_lot * self.lot_size, 2)  # $200

    @property
    def tp_dollar(self) -> float:
        return round(self.tp_pips * self.pip_value_per_lot * self.lot_size, 2)  # $1,400

    @property
    def risk_reward(self) -> float:
        return round(self.tp_pips / self.sl_pips, 2)  # 7.0

    @property
    def breakeven_win_rate(self) -> float:
        return round(self.sl_pips / (self.sl_pips + self.tp_pips), 4)  # 0.1250 = 12.5%

    def summary(self) -> str:
        return (
            f"\n{'='*64}\n"
            f"  THRESHOLD STRATEGY CONFIG  v1.5\n"
            f"{'='*64}\n"
            f"  Account              : ${self.account_size:>12,.0f}\n"
            f"  Lot size             : {self.lot_size}  "
            f"(${self.sl_dollar_target:.0f} SL ÷ {self.sl_pips:.0f} pips ÷ $100/pip)\n"
            f"{'─'*64}\n"
            f"  Threshold (1.0×)     : S ± {self.breakout_offset} pips  — observe level / SL anchor\n"
            f"  Entry     ({self.entry_multiplier}×)      : S ± {self.entry_offset} pips  — trigger (enters earlier)\n"
            f"  Exit      ({self.exit_multiplier}×)       : S ± {self.exit_offset} pips  — take profit\n"
            f"{'─'*64}\n"
            f"  SL buffer            : {self.sl_pips} pips entry→SL  → ${self.sl_dollar:,.0f}/trade nominal\n"
            f"  SL buffer at fill    : ~4–6 pips  (close-confirm adds 2–4 pips)\n"
            f"  TP distance          : {self.tp_pips} pips entry→TP  → ${self.tp_dollar:,.0f}/trade\n"
            f"  R:R                  : 1 : {self.risk_reward:.1f}\n"
            f"  Breakeven win rate   : {self.breakeven_win_rate*100:.1f}%  (1 win in 8 = profitable)\n"
            f"{'─'*64}\n"
            f"  Daily profit stop    : +${self.daily_profit_target_usd:,.0f}  (1 TP hit → done)\n"
            f"  Daily loss limit     : -${self.max_daily_loss_usd:,.0f}  (1 SL hit → done)\n"
            f"  7 loss days covered by: 1 win day  (at full nominal SL)\n"
            f"  Max trades/day       : {self.max_trades_per_day}\n"
            f"  Direction mode       : {self.direction_mode}\n"
            f"  Overshoot filter     : >{self.max_entry_overshoot_pips} pips gap → cancel\n"
            f"  Session (UTC)        : {self.session_start_hhmm} – {self.session_end_hhmm}\n"
            f"  Force close (UTC)    : {self.force_close_hhmm}\n"
            f"  MT5 clock            : UTC  (server UTC+3 = display only)\n"
            f"{'='*64}\n"
        )


cfg = StrategyConfig(account_size=50_000.0)


if __name__ == "__main__":
    c = StrategyConfig()
    print(c.summary())
    print(f"Verify:")
    print(f"  observe = S ± {c.breakout_offset}  (watch / SL anchor)")
    print(f"  entry   = S ± {c.entry_offset}  (LONG: S+22, SHORT: S-22)")
    print(f"  SL      = S ± {c.breakout_offset}  ({c.sl_pips} pips from entry)")
    print(f"  TP      = S ± {c.exit_offset}  ({c.tp_pips} pips from entry)")
    print(f"  lot     = {c.lot_size}  (${c.sl_dollar} risk → ${c.tp_dollar} reward)")
    print(f"  need {int(round(1/c.breakeven_win_rate))} consecutive losses to wipe 1 win")