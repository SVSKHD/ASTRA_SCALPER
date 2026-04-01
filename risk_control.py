from __future__ import annotations

# =============================================================================
# RISK CONTROL — all loss AND profit gating in one place
#
# Guards checked before every trade (in order):
#   1. Daily profit target hit  → BLOCK (we made enough, stop)
#   2. Daily loss limit hit     → BLOCK
#   3. Next SL would breach limit → BLOCK
#   4. Max trades/day reached   → BLOCK
#   5. Position already open    → BLOCK (no stacking)
#   All pass                    → ALLOW
#
# Worst-case day ($50k, 2.5 lot, SL=$200):
#   Trade 1 loses: realized=-200, budget=1300, next SL=200 ≤ 1300 → allowed
#   Trade 2 loses: realized=-400, budget=1100, next SL=200 ≤ 1100 → allowed
#   ...capped at max_trades_per_day=2 → worst day = -$400 + spread
# =============================================================================

from dataclasses import dataclass
from config import cfg, StrategyConfig


@dataclass
class RiskSnapshot:
    realized_pnl:        float   # closed P&L today (negative = loss)
    open_pnl:            float   # floating P&L on open positions
    trade_count:         int     # trades placed today
    open_position_count: int     # currently open positions


def remaining_loss_budget(
    realized_pnl: float,
    strategy_cfg: StrategyConfig = cfg,
) -> float:
    """How much more loss can be absorbed before hitting the daily limit."""
    current_loss = abs(min(realized_pnl, 0.0))
    return strategy_cfg.max_daily_loss_usd - current_loss


def is_daily_profit_hit(
    realized_pnl: float,
    strategy_cfg: StrategyConfig = cfg,
) -> bool:
    """Returns True if daily profit target has been reached — stop trading."""
    return realized_pnl >= strategy_cfg.daily_profit_target_usd


def is_daily_limit_breached(
    realized_pnl: float,
    strategy_cfg: StrategyConfig = cfg,
) -> bool:
    """Returns True if daily loss limit has been hit."""
    return realized_pnl <= -strategy_cfg.max_daily_loss_usd


def can_place_trade(
    snapshot: RiskSnapshot,
    strategy_cfg: StrategyConfig = cfg,
) -> tuple[bool, str]:
    """
    Master gate. Returns (allowed, reason).
    Call before every order_send().
    """

    # Guard 1 — daily profit target hit
    if snapshot.realized_pnl >= strategy_cfg.daily_profit_target_usd:
        return False, (
            f"DAILY_PROFIT_HIT | "
            f"realized=${snapshot.realized_pnl:+.2f} | "
            f"target=+${strategy_cfg.daily_profit_target_usd:.0f}"
        )

    # Guard 2 — daily loss limit hit
    if snapshot.realized_pnl <= -strategy_cfg.max_daily_loss_usd:
        return False, (
            f"DAILY_LIMIT_HIT | "
            f"realized=${snapshot.realized_pnl:+.2f} | "
            f"limit=-${strategy_cfg.max_daily_loss_usd:.0f}"
        )

    # Guard 3 — next SL would breach daily limit
    budget  = remaining_loss_budget(snapshot.realized_pnl, strategy_cfg)
    sl_cost = strategy_cfg.sl_dollar
    if sl_cost > budget:
        return False, (
            f"INSUFFICIENT_BUDGET | "
            f"sl_cost=${sl_cost:.0f} > budget=${budget:.0f} | "
            f"realized=${snapshot.realized_pnl:+.2f}"
        )

    # Guard 4 — max trades per day
    if snapshot.trade_count >= strategy_cfg.max_trades_per_day:
        return False, (
            f"MAX_TRADES_REACHED | "
            f"{snapshot.trade_count}/{strategy_cfg.max_trades_per_day}"
        )

    # Guard 5 — position already open (no stacking)
    if snapshot.open_position_count > 0:
        return False, f"POSITION_OPEN | open={snapshot.open_position_count}"

    return True, "OK"


def loss_scenario_summary(
    realized_pnl: float,
    strategy_cfg: StrategyConfig = cfg,
) -> str:
    budget   = remaining_loss_budget(realized_pnl, strategy_cfg)
    sl_cost  = strategy_cfg.sl_dollar
    safe     = sl_cost <= budget
    profit_ok = realized_pnl < strategy_cfg.daily_profit_target_usd
    return (
        f"  Realized PnL       : ${realized_pnl:+.2f}\n"
        f"  Daily profit target: +${strategy_cfg.daily_profit_target_usd:.0f}  "
        f"{'✅ not yet' if profit_ok else '🛑 HIT — no more trades'}\n"
        f"  Daily loss limit   : -${strategy_cfg.max_daily_loss_usd:.0f}\n"
        f"  Remaining budget   : ${budget:.0f}\n"
        f"  SL cost/trade      : ${sl_cost:.0f}\n"
        f"  Next trade safe    : {'✅ YES' if safe and profit_ok else '❌ BLOCKED'}"
    )


@dataclass
class DayResult:
    wins:         int
    losses:       int
    gross_pnl:    float
    net_pnl:      float
    spread_cost:  float
    hit_loss_limit:   bool
    hit_profit_target: bool

    def __str__(self) -> str:
        stop_reason = (
            "PROFIT_TARGET" if self.hit_profit_target
            else "LOSS_LIMIT"  if self.hit_loss_limit
            else "MAX_TRADES"
        )
        return (
            f"W={self.wins} L={self.losses} | "
            f"Gross=${self.gross_pnl:+,.2f} | "
            f"Spread=-${self.spread_cost:.2f} | "
            f"Net=${self.net_pnl:+,.2f} | "
            f"Stop={stop_reason}"
        )


def simulate_day(
    wins: int,
    losses: int,
    strategy_cfg: StrategyConfig = cfg,
    spread_per_trade: float = 35.0,
) -> DayResult:
    """
    Simulate one trading day with given win/loss count.
    Applies all guards: profit target, loss limit, budget, max trades.
    Order: wins first then losses (conservative estimate).
    """
    realized       = 0.0
    actual_wins    = 0
    actual_losses  = 0
    hit_loss       = False
    hit_profit     = False

    for outcome in (["WIN"] * wins + ["LOSS"] * losses):
        # profit target gate
        if realized >= strategy_cfg.daily_profit_target_usd:
            hit_profit = True
            break
        # trade count gate
        if actual_wins + actual_losses >= strategy_cfg.max_trades_per_day:
            break
        # budget gate for losses
        if outcome == "LOSS":
            budget = remaining_loss_budget(realized, strategy_cfg)
            if strategy_cfg.sl_dollar > budget:
                hit_loss = True
                break
        # execute
        if outcome == "WIN":
            realized    += strategy_cfg.tp_dollar
            actual_wins += 1
        else:
            realized    -= strategy_cfg.sl_dollar
            actual_losses += 1

    total_trades = actual_wins + actual_losses
    spread_cost  = total_trades * spread_per_trade

    return DayResult(
        wins=actual_wins,
        losses=actual_losses,
        gross_pnl=realized,
        net_pnl=realized - spread_cost,
        spread_cost=spread_cost,
        hit_loss_limit=hit_loss,
        hit_profit_target=hit_profit,
    )


if __name__ == "__main__":
    from config import StrategyConfig
    c = StrategyConfig(account_size=50_000)
    print(c.summary())
    print("SCENARIO TESTS")
    for label, w, l in [
        ("Both win",      2, 0),
        ("Win + loss",    1, 1),
        ("Both lose",     0, 2),
        ("Profit target", 3, 0),
    ]:
        r = simulate_day(w, l, c, spread_per_trade=0)
        print(f"  {label:15s} → {r}")