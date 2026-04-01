from __future__ import annotations

# =============================================================================
# THRESHOLD STRATEGY — MASTER CONFIG
#
# CONFIRMED EDGE (backtest March 2026, 23 days, real start prices):
#   --sl-pips 5 → 43.5% win rate → NET +$539  (breakeven 33.3%)
#   --sl-pips 2 → 13.6% win rate → NET -$1,685 (too tight, M5 noise)
#
# ACTIVE SETTINGS (sl-pips = 5):
#   Threshold (1.0×) : S ± 20 pips  — breakout level / SL anchor
#   Entry     (1.25×): S ± 25 pips  — trigger (5 pips above breakout)
#   Exit      (1.75×): S ± 35 pips  — take profit (10 pips above entry)
#   SL buffer        : 5 pips entry→SL  (survives M5 bar noise)
#   TP distance      : 10 pips entry→TP
#   R:R              : 1:2  (breakeven = 33.3%)
#   Lot size         : 0.2  (sl_dollar / (sl_pips × $100) = 100 / (5×100) = 0.2)
#   SL per trade     : $100
#   TP per trade     : $200
#
# TIMING (confirmed from day JSON files + pricing/settings.py):
#   lock_hhmm_mt5 = "00:00" UTC → day boundary = UTC midnight
#   date_mt5 = UTC calendar date — server UTC+3 is display only
#
# TO CHANGE SL WIDTH: use --sl-pips in backtest.py, then update multipliers here.
# =============================================================================

from dataclasses import dataclass
from typing import Literal


@dataclass
class StrategyConfig:

    # ── ACCOUNT ───────────────────────────────────────────
    account_size: float = 50_000.0

    # ── SYMBOL ────────────────────────────────────────────
    symbol: str = "XAUUSD"

    # ── THRESHOLD DEFINITION ──────────────────────────────
    # 1 pip = $1.00 price move on XAUUSD
    threshold_pips:   float = 20.0   # 1.0× = S ± 20 (breakout level = SL anchor)
    entry_multiplier: float = 1.25   # 1.25× = S ± 25 (entry, 5 pips above breakout)
    exit_multiplier:  float = 1.75   # 1.75× = S ± 35 (TP,   10 pips above entry)

    # ── DOLLAR TARGETS ────────────────────────────────────
    sl_dollar_target: float = 100.0   # $100 SL per trade
    tp_dollar_target: float = 200.0   # $200 TP per trade  (R:R = 2:1)

    # ── DAILY LIMITS ──────────────────────────────────────
    daily_profit_target_usd: float = 200.0   # stop after 1 TP hit ($200)
    max_daily_loss_usd:       float = 100.0  # stop after 1 SL hit ($100)

    # ── TRADE LIMITS ──────────────────────────────────────
    max_trades_per_day: int = 2
    direction_mode: Literal["first_only", "both"] = "both"

    # ── ENTRY OVERSHOOT FILTER ────────────────────────────
    max_entry_overshoot_pips: float = 3.0   # cancel if next-bar gaps > 3 pips past entry

    # ── SESSION FILTER (all UTC) ──────────────────────────
    session_start_hhmm:    str = "00:00"
    session_end_hhmm:      str = "23:00"
    force_close_hhmm:      str = "23:30"
    news_blackout_minutes: int = 15

    # ── PATHS ─────────────────────────────────────────────
    # storage.resolve_start_root_path → data/start_price/<symbol>.json
    base_dir: str = "data"

    # ── MT5 CLOCK ─────────────────────────────────────────
    server_utc_offset_hours: int = 0   # MT5 UI = UTC (server UTC+3 = display only)

    # ── MT5 ORDER ─────────────────────────────────────────
    magic_number:     int   = 20260401
    deviation_points: int   = 10
    order_comment:    str   = "threshold_xau"
    poll_seconds:     float = 0.3

    # =========================================================
    # DERIVED — always computed, never hardcoded elsewhere
    # =========================================================

    @property
    def pip_value_per_lot(self) -> float:
        """XAUUSD: 1 lot = 100oz, $1 price move = $100 P&L per lot"""
        return 100.0

    @property
    def breakout_offset(self) -> float:
        """S ± 20 pips — threshold breakout level, also the SL anchor"""
        return self.threshold_pips  # 20.0

    @property
    def entry_offset(self) -> float:
        """S ± 25 pips — entry trigger"""
        return round(self.threshold_pips * self.entry_multiplier, 2)  # 25.0

    @property
    def exit_offset(self) -> float:
        """S ± 35 pips — take profit level"""
        return round(self.threshold_pips * self.exit_multiplier, 2)   # 35.0

    @property
    def sl_pips(self) -> float:
        """Pips from entry to SL = entry_offset − breakout_offset = 5"""
        return round(self.entry_offset - self.breakout_offset, 2)     # 25 − 20 = 5.0

    @property
    def tp_pips(self) -> float:
        """Pips from entry to TP = exit_offset − entry_offset = 10"""
        return round(self.exit_offset - self.entry_offset, 2)         # 35 − 25 = 10.0

    @property
    def lot_size(self) -> float:
        """
        lot = sl_dollar_target / (sl_pips × pip_value_per_lot)
        = 100 / (5 × 100) = 0.2
        Dollar risk is fixed regardless of account size.
        """
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
            f"  Exit      ({self.exit_multiplier}×)   : S ± {self.exit_offset} pips  — take profit\n"
            f"{'─'*64}\n"
            f"  SL buffer            : {self.sl_pips} pips entry→SL  → ${self.sl_dollar:,.0f}/trade\n"
            f"  TP distance          : {self.tp_pips} pips entry→TP  → ${self.tp_dollar:,.0f}/trade\n"
            f"  R:R                  : 1 : {self.risk_reward:.1f}\n"
            f"  Breakeven win rate   : {self.breakeven_win_rate*100:.1f}%\n"
            f"{'─'*64}\n"
            f"  Daily profit stop    : +${self.daily_profit_target_usd:,.0f}\n"
            f"  Daily loss limit     : -${self.max_daily_loss_usd:,.0f}  "
            f"(= {int(self.max_daily_loss_usd / max(self.sl_dollar, 1)):.0f} SL → stop)\n"
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
    from config import StrategyConfig
    c = StrategyConfig()
    print(c.summary())
    print(f"Verify:")
    print(f"  entry = S + {c.entry_offset}  (long side)")
    print(f"  SL    = S + {c.breakout_offset}  ({c.sl_pips} pips below entry)")
    print(f"  TP    = S + {c.exit_offset}  ({c.tp_pips} pips above entry)")
    print(f"  lot   = {c.lot_size}  (${c.sl_dollar} risk, ${c.tp_dollar} reward)")