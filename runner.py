from __future__ import annotations

# =============================================================================
# STRATEGY RUNNER — MAIN LOOP
# Flow: start_reader → threshold → trade_signal → risk_control → executor
#
# Fixes applied:
#   - Import from trade_signal (not signal — stdlib collision)
#   - Daily profit stop added
#   - MT5 server-day used for rollover detection
# =============================================================================

import time
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

from config import cfg
from start_reader import read_start_price, _mt5_server_date
from threshold import compute_levels, ThresholdLevels
from trade_signal import evaluate_signal, Signal, Direction  # FIX: trade_signal not signal
from session_guard import is_session_allowed, is_force_close_time, session_status
from risk_control import (
    RiskSnapshot, can_place_trade,
    is_daily_limit_breached, is_daily_profit_hit,
    loss_scenario_summary,
)
from executor import (
    place_order, close_all_by_magic,
    calculate_day_pnl, calculate_open_pnl, get_open_positions,
)


class DayState:
    def __init__(self):
        self.date:           str                  = ""
        self.start_price:    float | None         = None
        self.levels:         ThresholdLevels | None = None
        self.already_traded: set[Direction]       = set()
        self.trade_count:    int                  = 0

    def reset(self, date: str):
        self.date           = date
        self.start_price    = None
        self.levels         = None
        self.already_traded = set()
        self.trade_count    = 0
        print(f"\n{'='*55}\n  DAY RESET → {date}\n{'='*55}\n")


def _today(strategy_cfg=cfg) -> str:
    """MT5 server date — matches date_mt5 field in start price JSON."""
    return _mt5_server_date(strategy_cfg)


def _mid(symbol: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if not tick or tick.time == 0:
        return None
    bid, ask = float(tick.bid), float(tick.ask)
    return (bid + ask) / 2.0 if bid > 0 and ask > 0 else None


def _snapshot(trade_count: int) -> RiskSnapshot:
    return RiskSnapshot(
        realized_pnl        = calculate_day_pnl(cfg),
        open_pnl            = calculate_open_pnl(cfg),
        trade_count         = trade_count,
        open_position_count = len(get_open_positions(cfg)),
    )


def _force_close(state: DayState):
    print(f"[{cfg.symbol}] 🔴 FORCE CLOSE — EOD")
    for r in close_all_by_magic(cfg):
        print(f"  {'✅' if r['success'] else '❌'} ticket={r['ticket']} retcode={r['retcode']}")
    pnl = calculate_day_pnl(cfg)
    print(
        f"\n[{cfg.symbol}] ── END OF DAY ─────────────────────\n"
        f"  MT5 Date   : {state.date}\n"
        f"  Trades     : {state.trade_count}\n"
        f"  Realized   : ${pnl:+.2f}\n"
        f"───────────────────────────────────────\n"
    )


def _handle_signal(signal: Signal, state: DayState) -> bool:
    snap    = _snapshot(state.trade_count)
    allowed, reason = can_place_trade(snap, cfg)

    if not allowed:
        print(f"[{cfg.symbol}] 🚫 BLOCKED | {reason}")
        return False

    print(f"\n[{cfg.symbol}] 🎯 {signal}")
    print(loss_scenario_summary(snap.realized_pnl, cfg))

    result = place_order(signal, cfg)

    if result.get("success"):
        state.already_traded.add(signal.direction)
        state.trade_count += 1
        post   = calculate_day_pnl(cfg)
        budget = cfg.max_daily_loss_usd - abs(min(post, 0))
        print(
            f"[{cfg.symbol}] ✅ Trade #{state.trade_count} PLACED\n"
            f"  Direction  : {signal.direction}\n"
            f"  Fill       : {result.get('fill_price', 'N/A')}\n"
            f"  TP         : {signal.tp_price:.2f}\n"
            f"  SL         : {signal.sl_price:.2f}\n"
            f"  Day PnL    : ${post:+.2f}\n"
            f"  Budget left: ${budget:.0f}\n"
            f"  Profit tgt : ${cfg.daily_profit_target_usd:.0f}"
        )
        return True

    print(f"[{cfg.symbol}] ❌ ORDER FAILED | retcode={result.get('retcode')} | {result.get('comment')}")
    return False


def run():
    print(cfg.summary())
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    state           = DayState()
    state.reset(_today())
    force_closed    = False
    last_log_ts     = 0.0
    last_warn_ts    = 0.0

    print(f"[{cfg.symbol}] ⏳ Waiting for start price lock...")

    while True:
        try:
            today = _today()

            # ── DATE ROLLOVER (MT5 server-day) ──────────────────────────────
            if today != state.date:
                state.reset(today)
                force_closed = False

            # ── START PRICE ─────────────────────────────────────────────────
            if state.start_price is None:
                price = read_start_price(cfg)
                if price is None:
                    time.sleep(2.0)
                    continue
                state.start_price = price
                state.levels      = compute_levels(price, cfg)
                print(state.levels.display())

            # ── FORCE CLOSE (EOD) ───────────────────────────────────────────
            if is_force_close_time(cfg) and not force_closed:
                _force_close(state)
                force_closed = True
                time.sleep(60.0)
                continue

            if force_closed:
                time.sleep(10.0)
                continue

            # ── SESSION GATE ────────────────────────────────────────────────
            if not is_session_allowed(cfg):
                time.sleep(5.0)
                continue

            # ── LIVE P&L ────────────────────────────────────────────────────
            realized = calculate_day_pnl(cfg)

            # ── DAILY PROFIT STOP ───────────────────────────────────────────
            if is_daily_profit_hit(realized, cfg):
                now = time.time()
                if now - last_warn_ts >= 60.0:
                    print(
                        f"[{cfg.symbol}] 🎯 PROFIT TARGET HIT | "
                        f"PnL=${realized:+.2f} | "
                        f"target=+${cfg.daily_profit_target_usd:.0f} | "
                        f"No more trades today."
                    )
                    last_warn_ts = now
                time.sleep(10.0)
                continue

            # ── DAILY LOSS GATE ─────────────────────────────────────────────
            if is_daily_limit_breached(realized, cfg):
                now = time.time()
                if now - last_warn_ts >= 60.0:
                    print(
                        f"[{cfg.symbol}] ⛔ LOSS LIMIT BREACHED | "
                        f"PnL=${realized:+.2f} | "
                        f"limit=-${cfg.max_daily_loss_usd:.0f}"
                    )
                    last_warn_ts = now
                time.sleep(10.0)
                continue

            # ── MAX TRADES ──────────────────────────────────────────────────
            if state.trade_count >= cfg.max_trades_per_day:
                time.sleep(5.0)
                continue

            # ── PRICE ───────────────────────────────────────────────────────
            mid = _mid(cfg.symbol)
            if mid is None:
                time.sleep(cfg.poll_seconds)
                continue

            # ── STATUS LOG (every 30s) ──────────────────────────────────────
            now = time.time()
            if now - last_log_ts >= 30.0:
                budget = cfg.max_daily_loss_usd - abs(min(realized, 0))
                print(
                    f"[{cfg.symbol}] {session_status(cfg)} | "
                    f"Mid={mid:.2f} | "
                    f"LongEntry={state.levels.long_entry:.2f} | "
                    f"ShortEntry={state.levels.short_entry:.2f} | "
                    f"Trades={state.trade_count}/{cfg.max_trades_per_day} | "
                    f"PnL=${realized:+.2f} | "
                    f"Budget=${budget:.0f} | "
                    f"ProfitTgt=${cfg.daily_profit_target_usd:.0f}"
                )
                last_log_ts = now

            # ── SIGNAL → RISK GATE → ORDER ──────────────────────────────────
            sig = evaluate_signal(mid, state.levels, state.already_traded, cfg)
            if sig:
                _handle_signal(sig, state)

            time.sleep(cfg.poll_seconds)

        except KeyboardInterrupt:
            print(f"\n[{cfg.symbol}] 🛑 Stopped manually.")
            close_all_by_magic(cfg)
            break
        except Exception as e:
            print(f"[{cfg.symbol}] ⚠️ Error: {repr(e)}")
            time.sleep(2.0)


if __name__ == "__main__":
    run()
