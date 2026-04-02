from __future__ import annotations

# =============================================================================
# THRESHOLD STRATEGY — MASTER CONFIG
#
# CONFIRMED EDGE (backtest March 2026, 23 days, real start prices):
#   --sl-pips 5 → 43.5% win rate → NET +$539  (breakeven 33.3%)
#   --sl-pips 2 → 13.6% win rate → NET -$1,685 (too tight, M5 noise)
#
# ACTIVE SETTINGS (sl-pips=5, R:R=1:3):
#   Threshold (1.0×) : S ± 20 pips  — breakout level / SL anchor
#   Entry     (1.25×): S ± 25 pips  — trigger (5 pips above breakout)
#   Exit      (2.0×) : S ± 40 pips  — take profit (15 pips above entry)
#   SL buffer        : 5 pips entry→SL
#   TP distance      : 15 pips entry→TP
#   R:R              : 1:3  (breakeven = 25.0%)
#   Lot size         : 0.4  (200 / (5×100) = 0.4)
#   SL per trade     : $200  (1 loss day)
#   TP per trade     : $600  (1 win covers 3 losses exactly)
#
# RISK LOGIC:
#   3 consecutive losses = -$600
#   1 win               = +$600
#   Net = $0 after 3 bad days + 1 good day
#   At 43.5% WR: 10 wins × $600 - 13 losses × $200 = +$3,400 gross/month
#
# TIMING:
#   lock_hhmm_mt5 = "00:00" UTC — server UTC+3 is display only
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
    threshold_pips:   float = 20.0   # 1.0× = S ± 20 (SL anchor)
    entry_multiplier: float = 1.25   # 1.25× = S ± 25 (entry, 5 pips above breakout)
    exit_multiplier:  float = 2.0    # 2.0×  = S ± 40 (TP, 15 pips above entry)

    # ── DOLLAR TARGETS ────────────────────────────────────
    sl_dollar_target: float = 200.0  # $200 SL per trade → lot = 0.4
    tp_dollar_target: float = 600.0  # $600 TP per trade → R:R = 1:3

    # ── DAILY LIMITS ──────────────────────────────────────
    daily_profit_target_usd: float = 600.0  # stop after 1 TP hit (+$600)
    max_daily_loss_usd:       float = 200.0  # stop after 1 SL hit (-$200)

    # ── TRADE LIMITS ──────────────────────────────────────
    max_trades_per_day: int = 1
    direction_mode: Literal["first_only", "both"] = "both"

    # ── ENTRY OVERSHOOT FILTER ────────────────────────────
    max_entry_overshoot_pips: float = 3.0

    # ── NEWS BLACKOUT DAYS ────────────────────────────────
    # List of "YYYY-MM-DD" UTC dates to skip trading entirely
    # e.g. ["2026-04-02", "2026-04-03"] for NFP / FOMC days
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
    # DERIVED
    # =========================================================

    @property
    def pip_value_per_lot(self) -> float:
        return 100.0

    @property
    def breakout_offset(self) -> float:
        return self.threshold_pips  # 20.0

    @property
    def entry_offset(self) -> float:
        return round(self.threshold_pips * self.entry_multiplier, 2)  # 25.0

    @property
    def exit_offset(self) -> float:
        return round(self.threshold_pips * self.exit_multiplier, 2)   # 40.0

    @property
    def sl_pips(self) -> float:
        """entry_offset − breakout_offset = 25 − 20 = 5"""
        return round(self.entry_offset - self.breakout_offset, 2)

    @property
    def tp_pips(self) -> float:
        """exit_offset − entry_offset = 40 − 25 = 15"""
        return round(self.exit_offset - self.entry_offset, 2)

    @property
    def lot_size(self) -> float:
        """sl_dollar_target / (sl_pips × pip_value_per_lot) = 200 / (5×100) = 0.4"""
        return round(
            self.sl_dollar_target / (self.sl_pips * self.pip_value_per_lot), 2
        )

    @property
    def sl_dollar(self) -> float:
        return round(self.sl_pips * self.pip_value_per_lot * self.lot_size, 2)

    @property
    def tp_dollar(self) -> float:
        return round(self.tp_pips * self.pip_value_per_lot * self.lot_size, 2)

    @property
    def risk_reward(self) -> float:
        return round(self.tp_pips / self.sl_pips, 2)

    @property
    def breakeven_win_rate(self) -> float:
        return round(self.sl_pips / (self.sl_pips + self.tp_pips), 4)

    def summary(self) -> str:
        return (
            f"\n{'='*64}\n"
            f"  THRESHOLD STRATEGY CONFIG\n"
            f"{'='*64}\n"
            f"  Account              : ${self.account_size:>12,.0f}\n"
            f"  Lot size             : {self.lot_size}  "
            f"(${self.sl_dollar_target:.0f} SL ÷ {self.sl_pips:.0f} pips ÷ $100/pip)\n"
            f"{'─'*64}\n"
            f"  Threshold (1.0×)     : S ± {self.breakout_offset} pips  — breakout / SL anchor\n"
            f"  Entry     ({self.entry_multiplier}×)   : S ± {self.entry_offset} pips  — trigger\n"
            f"  Exit      ({self.exit_multiplier}×)    : S ± {self.exit_offset} pips  — take profit\n"
            f"{'─'*64}\n"
            f"  SL buffer            : {self.sl_pips} pips entry→SL  → ${self.sl_dollar:,.0f}/trade\n"
            f"  TP distance          : {self.tp_pips} pips entry→TP  → ${self.tp_dollar:,.0f}/trade\n"
            f"  R:R                  : 1 : {self.risk_reward:.1f}\n"
            f"  Breakeven win rate   : {self.breakeven_win_rate*100:.1f}%\n"
            f"{'─'*64}\n"
            f"  Daily profit stop    : +${self.daily_profit_target_usd:,.0f}  (1 TP hit → done)\n"
            f"  Daily loss limit     : -${self.max_daily_loss_usd:,.0f}  (1 SL hit → done)\n"
            f"  3 loss days covered by: 1 win day exactly\n"
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
    print(f"  entry  = S ± {c.entry_offset}  (long side: S+25, short side: S-25)")
    print(f"  SL     = S ± {c.breakout_offset}  ({c.sl_pips} pips from entry)")
    print(f"  TP     = S ± {c.exit_offset}  ({c.tp_pips} pips from entry)")
    print(f"  lot    = {c.lot_size}  (${c.sl_dollar} risk → ${c.tp_dollar} reward)")
    print(f"  1 win covers {int(c.tp_dollar / c.sl_dollar)} losses exactly")