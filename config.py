from __future__ import annotations

# =============================================================================
# THRESHOLD STRATEGY — MASTER CONFIG
# Single source of truth. All modules import from here. Nothing hardcoded elsewhere.
#
# STRATEGY DEFINITION (locked):
#   1 threshold = 2000 points = $20.00 price move
#   Entry  = 1.1 × threshold = $22.00 from start  (S ± 22)
#   Exit   = 1.2 × threshold = $24.00 from start  (S ± 24)
#   SL     = 1.0 × threshold = $20.00 from start  (S ± 20) — back at breakout
#   Capture per trade = $2.00 (exit − entry = 24 − 22)
# =============================================================================

from dataclasses import dataclass
from typing import Literal


@dataclass
class StrategyConfig:

    # ── ACCOUNT ───────────────────────────────────────────
    account_size: float        = 50_000.0
    risk_per_trade_pct: float  = 0.01          # 1% risk per trade

    # ── SYMBOL ────────────────────────────────────────────
    symbol: str = "XAUUSD"

    # ── THRESHOLD DEFINITION ──────────────────────────────
    # 1 pip  = $1.00 price move  (e.g. 4000.00 → 4001.00)
    # 1 pt   = $0.01 (MT5 native point)
    # threshold_pips = 20 → breakout zone at S ± $20
    threshold_pips: float     = 20.0           # 1.0× — breakout confirmation level
    entry_multiplier: float   = 1.1            # enter at 1.1× → S ± $22
    exit_multiplier: float    = 1.2            # exit  at 1.2× → S ± $24

    # ── ENTRY OVERSHOOT FILTER ────────────────────────────
    # If price is more than max_entry_overshoot_pips past entry
    # the signal is rejected — price moved too far, fill would be stale
    max_entry_overshoot_pips: float = 3.0      # reject if mid > entry + 3 pips

    # ── DAILY PROFIT STOP ─────────────────────────────────
    # Kill trading once daily realized P&L hits this target
    daily_profit_target_usd: float = 150.0     # stop trading above this

    # ── DAILY LOSS LIMIT ──────────────────────────────────
    max_daily_loss_pct: float  = 0.03          # 3% of account

    # ── TRADE LIMITS ──────────────────────────────────────
    max_trades_per_day: int    = 2
    direction_mode: Literal["first_only", "both"] = "both"

    # ── SESSION FILTER (UTC HH:MM) ────────────────────────
    session_start_hhmm: str    = "08:00"       # London open
    session_end_hhmm: str      = "20:00"       # NY close
    force_close_hhmm: str      = "21:30"       # hard EOD close
    news_blackout_minutes: int = 15

    # ── START PRICE SOURCE ────────────────────────────────
    # Matches resolve_start_root_path(base_dir, symbol) in storage.py:
    # → {base_dir}/start/{symbol}/start.json
    base_dir: str              = "data"

    # ── MT5 ORDER ─────────────────────────────────────────
    magic_number: int          = 20260401
    deviation_points: int      = 10
    order_comment: str         = "threshold_xau"

    # ── POLLING ───────────────────────────────────────────
    poll_seconds: float        = 0.3

    # ── MT5 SERVER TIMEZONE ───────────────────────────────
    # Used for MT5-day alignment (not naive UTC)
    server_utc_offset_hours: int = 2           # EET (MT5 server default)

    # =========================================================
    # DERIVED — computed from config, never hardcoded elsewhere
    # =========================================================

    @property
    def pip_value_per_lot(self) -> float:
        """XAUUSD: 1 lot = 100oz, 1 pip = $1.00 move → $100 per pip per lot"""
        return 100.0

    @property
    def lot_size(self) -> float:
        """Lot size derived from account size + risk % + SL pips."""
        risk_usd = self.account_size * self.risk_per_trade_pct
        return round(risk_usd / (self.sl_pips * self.pip_value_per_lot), 2)

    @property
    def max_daily_loss_usd(self) -> float:
        return round(self.account_size * self.max_daily_loss_pct, 2)

    # ── TRUE THRESHOLD LEVELS ────────────────────────────
    @property
    def breakout_offset(self) -> float:
        """1.0× threshold — breakout confirmation zone (DO NOT enter here)"""
        return self.threshold_pips                                    # $20.00

    @property
    def entry_offset(self) -> float:
        """1.1× threshold — entry trigger"""
        return round(self.threshold_pips * self.entry_multiplier, 2) # $22.00

    @property
    def exit_offset(self) -> float:
        """1.2× threshold — take profit (true exit per strategy definition)"""
        return round(self.threshold_pips * self.exit_multiplier, 2)  # $24.00

    @property
    def sl_pips(self) -> float:
        """SL sits at 1.0× breakout level. SL distance from entry = entry − breakout."""
        return round(self.entry_offset - self.breakout_offset, 2)    # 22 − 20 = $2.00

    @property
    def tp_pips(self) -> float:
        """TP distance from entry = exit − entry."""
        return round(self.exit_offset - self.entry_offset, 2)        # 24 − 22 = $2.00

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
        return round(self.tp_pips / self.sl_pips, 2)                 # 2/2 = 1.0

    @property
    def breakeven_win_rate(self) -> float:
        return self.sl_pips / (self.sl_pips + self.tp_pips)          # 2/4 = 0.50

    def summary(self) -> str:
        return (
            f"\n{'='*60}\n"
            f"  THRESHOLD STRATEGY CONFIG\n"
            f"{'='*60}\n"
            f"  Account            : ${self.account_size:>12,.0f}\n"
            f"  Risk/trade         : {self.risk_per_trade_pct*100:.1f}%"
            f"  (${self.account_size*self.risk_per_trade_pct:,.0f})\n"
            f"  Lot size           : {self.lot_size}\n"
            f"{'─'*60}\n"
            f"  Threshold (1.0×)   : {self.threshold_pips} pips  (${self.threshold_pips:.2f})\n"
            f"  Entry     (1.1×)   : {self.entry_offset} pips from start\n"
            f"  Exit      (1.2×)   : {self.exit_offset} pips from start\n"
            f"  SL                 : {self.sl_pips} pips  "
            f"(-${self.sl_dollar:,.0f} @ {self.lot_size} lot)\n"
            f"  TP                 : {self.tp_pips} pips  "
            f"(+${self.tp_dollar:,.0f} @ {self.lot_size} lot)\n"
            f"  R:R                : 1 : {self.risk_reward:.1f}\n"
            f"  Breakeven WR       : {self.breakeven_win_rate*100:.0f}%\n"
            f"{'─'*60}\n"
            f"  Daily profit stop  : +${self.daily_profit_target_usd:,.0f}\n"
            f"  Daily loss limit   : -${self.max_daily_loss_usd:,.0f}"
            f"  ({self.max_daily_loss_pct*100:.0f}% of account)\n"
            f"  Max trades/day     : {self.max_trades_per_day}\n"
            f"  Direction mode     : {self.direction_mode}\n"
            f"  Overshoot filter   : >{self.max_entry_overshoot_pips} pips = reject\n"
            f"  Session            : {self.session_start_hhmm} – {self.session_end_hhmm} UTC\n"
            f"  Force close        : {self.force_close_hhmm} UTC\n"
            f"{'='*60}\n"
        )


# Default — set account_size before running live
cfg = StrategyConfig(account_size=50_000.0)


if __name__ == "__main__":
    for acc in [10_000, 20_000, 50_000, 100_000]:
        c = StrategyConfig(account_size=acc)
        print(c.summary())
