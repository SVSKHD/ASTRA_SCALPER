from __future__ import annotations

# =============================================================================
# THRESHOLD STRATEGY — MASTER CONFIG
#
# STRATEGY DEFINITION:
#   Threshold    = 20 pips ($20 from start)
#   Entry   1.1× = S ± 22  (2 pips above breakout)
#   Exit    1.2× = S ± 24  → TP = 2 pips  (SYMMETRIC — baseline)
#   SL      1.0× = S ± 20  → SL = 2 pips  (back at breakout)
#
# TARGET DOLLAR VALUES (drives lot size and exit_multiplier):
#   sl_dollar_target = $100/trade  → lot = 0.5 at 2 pip SL
#   tp_dollar_target = $150/trade  → exit at S ± 25 (3 pips, exit_multiplier=1.25)
#   tp_dollar_target = $200/trade  → exit at S ± 26 (4 pips, exit_multiplier=1.30)
#   R:R = 1.5:1 or 2:1  →  breakeven win rate = 40% or 33%
#
# TIMING (confirmed from 2026-04-01.json + pricing/settings.py):
#   lock_hhmm_mt5 = "00:00" UTC → day boundary = UTC midnight
#   date_mt5 = UTC calendar date — server UTC+3 is display only
# =============================================================================

from dataclasses import dataclass
from typing import Literal


@dataclass
class StrategyConfig:

    # ── ACCOUNT ───────────────────────────────────────────
    account_size:          float = 50_000.0

    # ── SYMBOL ────────────────────────────────────────────
    symbol: str = "XAUUSD"

    # ── THRESHOLD DEFINITION ──────────────────────────────
    # 1 pip = $1.00 price move  (e.g. 4000.00 → 4001.00)
    threshold_pips:   float = 20.0     # 1.0× breakout = S ± $20
    entry_multiplier: float = 1.1      # enter  at 1.1× = S ± $22
    exit_multiplier:  float = 1.25     # exit   at 1.25× = S ± $25 (3 pips TP → $150 at 0.5 lot)
    #                                  # use 1.30 for S±26 (4 pips → $200 at 0.5 lot)

    # ── DOLLAR TARGETS ────────────────────────────────────
    # These are the SOURCE OF TRUTH for risk sizing.
    # lot_size is derived from sl_dollar_target, NOT from account %.
    sl_dollar_target: float = 100.0    # max loss per trade in USD
    tp_dollar_target: float = 150.0    # target profit per trade in USD
    #                                  # (must match exit_multiplier × lot_size × 100)

    # ── DAILY LIMITS ──────────────────────────────────────
    daily_profit_target_usd: float = 150.0   # stop trading once daily PnL hits this
    max_daily_loss_usd:       float = 100.0  # stop trading once daily loss hits this
    #                                        # = 1 SL hit → done for the day

    # ── TRADE LIMITS ──────────────────────────────────────
    max_trades_per_day: int = 2
    direction_mode: Literal["first_only", "both"] = "both"

    # ── ENTRY OVERSHOOT FILTER ────────────────────────────
    max_entry_overshoot_pips: float = 3.0

    # ── SESSION FILTER (UTC) ──────────────────────────────
    session_start_hhmm:    str = "00:00"    # UTC midnight = lock_hhmm_mt5
    session_end_hhmm:      str = "23:00"
    force_close_hhmm:      str = "23:30"
    news_blackout_minutes: int = 15

    # ── START PRICE SOURCE ────────────────────────────────
    # storage.resolve_start_root_path → data/start_price/<symbol>.json
    base_dir: str = "data"

    # ── MT5 CLOCK ─────────────────────────────────────────
    server_utc_offset_hours: int = 0    # MT5 UI = UTC

    # ── MT5 ORDER ─────────────────────────────────────────
    magic_number:     int   = 20260401
    deviation_points: int   = 10
    order_comment:    str   = "threshold_xau"
    poll_seconds:     float = 0.3

    # =========================================================
    # DERIVED — computed from targets, never hardcoded
    # =========================================================

    @property
    def pip_value_per_lot(self) -> float:
        """XAUUSD: 1 lot=100oz, 1 pip=$1 move → $100/pip/lot"""
        return 100.0

    @property
    def breakout_offset(self) -> float:
        """1.0× threshold = $20 from start (SL level)"""
        return self.threshold_pips  # 20.0

    @property
    def entry_offset(self) -> float:
        """1.1× entry = $22 from start"""
        return round(self.threshold_pips * self.entry_multiplier, 2)  # 22.0

    @property
    def exit_offset(self) -> float:
        """exit_multiplier× = TP level from start"""
        return round(self.threshold_pips * self.exit_multiplier, 2)   # 25.0 at 1.25×

    @property
    def sl_pips(self) -> float:
        """SL distance from entry = entry − breakout = 2 pips"""
        return round(self.entry_offset - self.breakout_offset, 2)     # 22 − 20 = 2.0

    @property
    def tp_pips(self) -> float:
        """TP distance from entry = exit − entry"""
        return round(self.exit_offset - self.entry_offset, 2)         # 25 − 22 = 3.0

    @property
    def lot_size(self) -> float:
        """
        Lot size derived from sl_dollar_target and sl_pips.
        sl_dollar_target = sl_pips × lot_size × pip_value_per_lot
        → lot_size = sl_dollar_target / (sl_pips × pip_value_per_lot)
        e.g. $100 / (2 pips × $100/pip/lot) = 0.5 lot
        """
        return round(
            self.sl_dollar_target / (self.sl_pips * self.pip_value_per_lot), 2
        )

    @property
    def sl_dollar(self) -> float:
        """Actual SL in USD (should equal sl_dollar_target)."""
        return round(self.sl_pips * self.pip_value_per_lot * self.lot_size, 2)

    @property
    def tp_dollar(self) -> float:
        """Actual TP in USD."""
        return round(self.tp_pips * self.pip_value_per_lot * self.lot_size, 2)

    @property
    def risk_reward(self) -> float:
        return round(self.tp_pips / self.sl_pips, 2)

    @property
    def breakeven_win_rate(self) -> float:
        return round(self.sl_pips / (self.sl_pips + self.tp_pips), 4)

    def summary(self) -> str:
        return (
            f"\n{'='*62}\n"
            f"  THRESHOLD STRATEGY CONFIG\n"
            f"{'='*62}\n"
            f"  Account              : ${self.account_size:>12,.0f}\n"
            f"  Lot size             : {self.lot_size}  "
            f"(from SL target ${self.sl_dollar_target:.0f})\n"
            f"{'─'*62}\n"
            f"  Threshold (1.0×)     : S ± {self.breakout_offset} pips  — breakout / SL\n"
            f"  Entry     (1.1×)     : S ± {self.entry_offset} pips  — trigger\n"
            f"  Exit      ({self.exit_multiplier}×)    : S ± {self.exit_offset} pips  — take profit\n"
            f"{'─'*62}\n"
            f"  SL per trade         : {self.sl_pips} pips  → ${self.sl_dollar:,.0f}\n"
            f"  TP per trade         : {self.tp_pips} pips  → ${self.tp_dollar:,.0f}\n"
            f"  R:R                  : 1 : {self.risk_reward:.2f}\n"
            f"  Breakeven win rate   : {self.breakeven_win_rate*100:.1f}%\n"
            f"{'─'*62}\n"
            f"  Daily profit stop    : +${self.daily_profit_target_usd:,.0f}\n"
            f"  Daily loss limit     : -${self.max_daily_loss_usd:,.0f}  "
            f"(= {int(self.max_daily_loss_usd / self.sl_dollar):.0f} SL hit(s) then stop)\n"
            f"  Max trades/day       : {self.max_trades_per_day}\n"
            f"  Direction mode       : {self.direction_mode}\n"
            f"  Overshoot filter     : >{self.max_entry_overshoot_pips} pips = reject\n"
            f"  Session (UTC)        : {self.session_start_hhmm} – {self.session_end_hhmm}\n"
            f"  Force close (UTC)    : {self.force_close_hhmm}\n"
            f"  MT5 clock            : UTC (server UTC+3 = display only)\n"
            f"{'='*62}\n"
        )


# Default instance
cfg = StrategyConfig(account_size=50_000.0)


if __name__ == "__main__":
    print("=== SL=$100, TP=$150  (exit_multiplier=1.25, R:R=1.5:1) ===")
    c1 = StrategyConfig(sl_dollar_target=100, tp_dollar_target=150,
                        exit_multiplier=1.25, max_daily_loss_usd=100,
                        daily_profit_target_usd=150)
    print(c1.summary())

    print("=== SL=$100, TP=$200  (exit_multiplier=1.30, R:R=2:1) ===")
    c2 = StrategyConfig(sl_dollar_target=100, tp_dollar_target=200,
                        exit_multiplier=1.30, max_daily_loss_usd=100,
                        daily_profit_target_usd=200)
    print(c2.summary())
