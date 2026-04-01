from __future__ import annotations

# =============================================================================
# THRESHOLD STRATEGY — MASTER CONFIG
#
# TIMING FACTS (confirmed from 2026-04-01.json + start_price.py source):
#   - MT5 UI timezone : UTC  (tz.mt5_ui = "UTC")
#   - lock_hhmm_mt5   : "00:00" — first UTC tick at/after midnight
#   - date_mt5        : UTC calendar date  (NOT server timezone date)
#   - Server timezone : UTC+03 — display only, irrelevant for locking
#
# Therefore:
#   - server_utc_offset_hours = 0  (UTC = MT5 date boundary)
#   - session_start_hhmm = "00:00" (UTC midnight = lock trigger)
#   - All day/session comparisons use UTC
# =============================================================================

from dataclasses import dataclass
from typing import Literal


@dataclass
class StrategyConfig:

    # ── ACCOUNT ───────────────────────────────────────────
    account_size:          float = 50_000.0
    risk_per_trade_pct:    float = 0.01         # 1% risk per trade

    # ── SYMBOL ────────────────────────────────────────────
    symbol: str = "XAUUSD"

    # ── THRESHOLD DEFINITION ──────────────────────────────
    # 1 pip = $1.00 price move  (e.g. 4000.00 → 4001.00)
    threshold_pips:   float = 20.0              # 1.0× breakout = $20 from start
    entry_multiplier: float = 1.1               # enter at 1.1× = $22 from start
    exit_multiplier:  float = 1.2               # exit  at 1.2× = $24 from start

    # ── ENTRY OVERSHOOT FILTER ────────────────────────────
    max_entry_overshoot_pips: float = 3.0       # reject if mid > entry + 3 pips (stale fill)

    # ── DAILY PROFIT STOP ─────────────────────────────────
    daily_profit_target_usd: float = 150.0      # stop trading once daily P&L hits this

    # ── DAILY LOSS LIMIT ──────────────────────────────────
    max_daily_loss_pct: float = 0.03            # 3% of account

    # ── TRADE LIMITS ──────────────────────────────────────
    max_trades_per_day: int = 2
    direction_mode: Literal["first_only", "both"] = "both"

    # ── SESSION FILTER ────────────────────────────────────
    # All times in UTC — matching MT5 UI clock (tz.mt5_ui = "UTC")
    # Default 00:00 = UTC midnight = real bot lock_hhmm_mt5 trigger
    session_start_hhmm:    str = "00:00"        # UTC midnight — matches lock_hhmm_mt5
    session_end_hhmm:      str = "23:00"        # UTC
    force_close_hhmm:      str = "23:30"        # UTC
    news_blackout_minutes: int = 15

    # ── START PRICE SOURCE ────────────────────────────────
    # Mirrors storage.resolve_start_root_path(base_dir, symbol)
    # Path: {base_dir}/start/{symbol}/start.json
    base_dir: str = "data"

    # ── MT5 CLOCK ─────────────────────────────────────────
    # MT5 UI displays UTC. date_mt5 is UTC calendar date.
    # server_utc_offset_hours = 0 → day boundaries at UTC midnight.
    # (The server displays UTC+3 for logging/display only — irrelevant here.)
    server_utc_offset_hours: int = 0            # MT5 UI clock = UTC

    # ── MT5 ORDER ─────────────────────────────────────────
    magic_number:     int = 20260401
    deviation_points: int = 10
    order_comment:    str = "threshold_xau"

    # ── POLLING ───────────────────────────────────────────
    poll_seconds: float = 0.3

    # =========================================================
    # DERIVED — never hardcoded elsewhere
    # =========================================================

    @property
    def pip_value_per_lot(self) -> float:
        """XAUUSD: 1 lot=100oz, 1 pip=$1 move → $100 per pip per lot"""
        return 100.0

    @property
    def lot_size(self) -> float:
        """Lot from account + risk % + SL pips."""
        risk_usd = self.account_size * self.risk_per_trade_pct
        return round(risk_usd / (self.sl_pips * self.pip_value_per_lot), 2)

    @property
    def max_daily_loss_usd(self) -> float:
        return round(self.account_size * self.max_daily_loss_pct, 2)

    @property
    def breakout_offset(self) -> float:
        return self.threshold_pips                                    # 20.0

    @property
    def entry_offset(self) -> float:
        return round(self.threshold_pips * self.entry_multiplier, 2) # 22.0

    @property
    def exit_offset(self) -> float:
        return round(self.threshold_pips * self.exit_multiplier, 2)  # 24.0

    @property
    def sl_pips(self) -> float:
        return round(self.entry_offset - self.breakout_offset, 2)    # 2.0

    @property
    def tp_pips(self) -> float:
        return round(self.exit_offset - self.entry_offset, 2)        # 2.0

    @property
    def capture_per_trade_usd(self) -> float:
        return round(self.tp_pips * self.pip_value_per_lot * self.lot_size, 2)

    @property
    def sl_dollar(self) -> float:
        return round(self.sl_pips * self.pip_value_per_lot * self.lot_size, 2)

    @property
    def tp_dollar(self) -> float:
        return self.capture_per_trade_usd

    @property
    def risk_reward(self) -> float:
        return round(self.tp_pips / self.sl_pips, 2)

    @property
    def breakeven_win_rate(self) -> float:
        return self.sl_pips / (self.sl_pips + self.tp_pips)

    def summary(self) -> str:
        return (
            f"\n{'='*62}\n"
            f"  THRESHOLD STRATEGY CONFIG\n"
            f"{'='*62}\n"
            f"  Account            : ${self.account_size:>12,.0f}\n"
            f"  Risk/trade         : {self.risk_per_trade_pct*100:.1f}%  (${self.account_size*self.risk_per_trade_pct:,.0f})\n"
            f"  Lot size           : {self.lot_size}\n"
            f"{'─'*62}\n"
            f"  Threshold (1.0×)   : {self.threshold_pips} pips  (${self.threshold_pips:.2f})\n"
            f"  Entry     (1.1×)   : S ± {self.entry_offset} pips\n"
            f"  Exit      (1.2×)   : S ± {self.exit_offset} pips\n"
            f"  SL                 : {self.sl_pips} pips  (-${self.sl_dollar:,.0f} @ {self.lot_size} lot)\n"
            f"  TP                 : {self.tp_pips} pips  (+${self.tp_dollar:,.0f} @ {self.lot_size} lot)\n"
            f"  R:R                : 1 : {self.risk_reward:.1f}\n"
            f"  Breakeven WR       : {self.breakeven_win_rate*100:.0f}%\n"
            f"{'─'*62}\n"
            f"  Daily profit stop  : +${self.daily_profit_target_usd:,.0f}\n"
            f"  Daily loss limit   : -${self.max_daily_loss_usd:,.0f}  ({self.max_daily_loss_pct*100:.0f}% of account)\n"
            f"  Max trades/day     : {self.max_trades_per_day}\n"
            f"  Direction mode     : {self.direction_mode}\n"
            f"  Overshoot filter   : >{self.max_entry_overshoot_pips} pips = reject\n"
            f"  Session (UTC)      : {self.session_start_hhmm} – {self.session_end_hhmm}\n"
            f"  Force close (UTC)  : {self.force_close_hhmm}\n"
            f"  MT5 clock          : UTC  (server_utc_offset=0)\n"
            f"{'='*62}\n"
        )


cfg = StrategyConfig(account_size=50_000.0)


if __name__ == "__main__":
    for acc in [10_000, 20_000, 50_000, 100_000]:
        print(StrategyConfig(account_size=acc).summary())
