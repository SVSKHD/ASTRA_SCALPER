from __future__ import annotations

# =============================================================================
# STRATEGY RUNNER — MAIN LOOP
# Reads start price from: data/start_price/<symbol>.json
# (matches storage.resolve_start_root_path in the repo)
# =============================================================================

import time
import logging
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

from config import cfg
from start_reader import read_start_price, _utc_date_today
from threshold import compute_levels, ThresholdLevels
from trade_signal import evaluate_signal, Signal, Direction
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
try:
    from telegram_notify import (
        notify_day_start, notify_trade_placed,
        notify_tp, notify_sl, notify_force_close,
        notify_day_end, notify_loss_limit, notify_profit_target,
    )
    TELEGRAM = True
except Exception:
    TELEGRAM = False

log = logging.getLogger("runner")

# IST = UTC + 5:30
_IST = timezone(timedelta(hours=5, minutes=30))

_MAX_CLOSE_RETRIES = 3


def _tg_safe(fn, *args, **kwargs):
    """Call a Telegram notify function; swallow all exceptions."""
    if not TELEGRAM:
        return
    try:
        fn(*args, **kwargs)
    except Exception as e:
        print(f"[Telegram] Send failed: {repr(e)}")


def _lookup_deal_pnl(ticket: int) -> float | None:
    """
    Look up exact closed P&L from MT5 deal history for today.
    Uses history_deals_get(day_start, now) filtered by magic + symbol,
    then matches deal.position_id == ticket.
    Returns the profit from the closing deal, or None if not yet in history.
    """
    try:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(day_start, now)
        if not deals:
            return None
        pos_deals = [
            d for d in deals
            if d.magic == cfg.magic_number
            and d.symbol == cfg.symbol
            and d.position_id == ticket
        ]
        if not pos_deals:
            return None
        return sum(d.profit for d in pos_deals)
    except Exception:
        return None


class DayState:
    def __init__(self):
        self.date:            str                   = ""
        self.start_price:     float | None          = None
        self.levels:          ThresholdLevels | None = None
        self.already_traded:  set[Direction]        = set()
        self.trade_count:     int                   = 0
        # BUG 2 FIX: internal P&L tracking (independent of MT5 deal history)
        self.internal_pnl:    float                 = 0.0
        self._tracked_positions: list[dict]         = []
        # BUG 3 FIX: prevent double-firing within same poll cycle
        self.order_in_flight: bool                  = False
        # Telegram: each notification key fires exactly once per UTC day
        # Keys: "day_start", "loss_limit", "profit_target",
        #       "tp_{ticket}", "sl_{ticket}", "fc_{ticket}"
        self.notified_today:  set[str]              = set()
        # Deal history retry tracking: ticket → retry count
        self._close_retry_count: dict[int, int]     = {}

    def reset(self, date: str):
        self.date           = date
        self.start_price    = None
        self.levels         = None
        self.already_traded = set()
        self.trade_count    = 0
        self.internal_pnl   = 0.0
        self._tracked_positions = []
        self.order_in_flight = False
        self.notified_today = set()
        self._close_retry_count = {}
        print(f"\n{'='*55}\n  DAY RESET → {date}\n{'='*55}\n")

    def track_position(self, ticket: int, fill_price: float,
                       direction: str, volume: float,
                       sl_price: float, tp_price: float):
        """Register a newly filled position for internal P&L tracking."""
        self._tracked_positions.append({
            "ticket": ticket,
            "fill_price": fill_price,
            "direction": direction,
            "volume": volume,
            "sl_price": sl_price,
            "tp_price": tp_price,
        })

    def update_closed_positions(self) -> list[dict]:
        """
        Check tracked positions against MT5 open positions.
        If a tracked position is no longer open, read exact P&L from
        MT5 deal history (filtered by magic + symbol + position_id).
        Falls back to tick estimate after _MAX_CLOSE_RETRIES poll cycles.

        Returns list of dicts for each confirmed closed position:
          {"ticket", "direction", "fill_price", "sl_price", "tp_price", "pnl"}
        """
        if not self._tracked_positions:
            return []

        open_tickets = {p.ticket for p in get_open_positions(cfg)}
        still_open = []
        closed = []

        for pos in self._tracked_positions:
            if pos["ticket"] in open_tickets:
                still_open.append(pos)
                continue

            ticket = pos["ticket"]

            # Try exact P&L from MT5 deal history
            pnl = _lookup_deal_pnl(ticket)

            if pnl is None:
                # Deal not yet in history — retry or fall back to estimate
                retries = self._close_retry_count.get(ticket, 0)
                if retries < _MAX_CLOSE_RETRIES:
                    self._close_retry_count[ticket] = retries + 1
                    log.info(
                        f"[{cfg.symbol}] Position #{ticket} closed but deal not in history "
                        f"(retry {retries + 1}/{_MAX_CLOSE_RETRIES})"
                    )
                    # Keep in tracked list — will retry next poll cycle
                    still_open.append(pos)
                    continue

                # Max retries exhausted — fall back to tick estimate
                log.warning(
                    f"[{cfg.symbol}] Position #{ticket} deal history unavailable after "
                    f"{_MAX_CLOSE_RETRIES} retries — using tick estimate"
                )
                tick = mt5.symbol_info_tick(cfg.symbol)
                if tick is not None:
                    if pos["direction"] == "LONG":
                        close_est = float(tick.bid)
                        pnl = (close_est - pos["fill_price"]) * pos["volume"] * cfg.pip_value_per_lot
                    else:
                        close_est = float(tick.ask)
                        pnl = (pos["fill_price"] - close_est) * pos["volume"] * cfg.pip_value_per_lot
                else:
                    pnl = -cfg.sl_dollar

            # Clean up retry counter
            self._close_retry_count.pop(ticket, None)

            self.internal_pnl += pnl
            log.info(
                f"[{cfg.symbol}] Position #{ticket} closed | "
                f"pnl: {pnl:+.2f} | internal_pnl total: {self.internal_pnl:+.2f}"
            )
            closed.append({
                "ticket": ticket,
                "direction": pos["direction"],
                "fill_price": pos["fill_price"],
                "sl_price": pos["sl_price"],
                "tp_price": pos["tp_price"],
                "pnl": pnl,
            })

        self._tracked_positions = still_open
        return closed


def _today() -> str:
    """UTC calendar date — matches date_mt5 written by start_price.py."""
    return _utc_date_today()


def _mid(symbol: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if not tick or tick.time == 0:
        return None
    bid, ask = float(tick.bid), float(tick.ask)
    return (bid + ask) / 2.0 if bid > 0 and ask > 0 else None


def _live_fill_price(symbol: str, direction: str) -> float | None:
    """Get the current ask (LONG) or bid (SHORT) — the price MT5 would fill at."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return float(tick.ask) if direction == "LONG" else float(tick.bid)


def _snapshot(trade_count: int, internal_pnl: float) -> RiskSnapshot:
    mt5_pnl = calculate_day_pnl(cfg)
    # BUG 2 FIX: use the worse of MT5 history P&L and internal tracking
    effective_pnl = min(mt5_pnl, internal_pnl) if internal_pnl < 0 else mt5_pnl
    if internal_pnl < mt5_pnl:
        log.warning(
            f"[{cfg.symbol}] INTERNAL P&L ({internal_pnl:+.2f}) worse than "
            f"MT5 history ({mt5_pnl:+.2f}) — using internal value"
        )
    return RiskSnapshot(
        realized_pnl        = effective_pnl,
        open_pnl            = calculate_open_pnl(cfg),
        trade_count         = trade_count,
        open_position_count = len(get_open_positions(cfg)),
    )


def _force_close(state: DayState):
    print(f"[{cfg.symbol}] FORCE CLOSE — EOD")
    results = close_all_by_magic(cfg)
    for r in results:
        print(f"  {'OK' if r['success'] else 'FAIL'} ticket={r['ticket']} retcode={r['retcode']}")
    pnl = calculate_day_pnl(cfg)

    # Telegram: notify each force-closed position (with dedup)
    for r in results:
        fc_key = f"fc_{r['ticket']}"
        if TELEGRAM and fc_key not in state.notified_today:
            # Look up fill_price from tracked positions if available
            fill = 0.0
            for pos in state._tracked_positions:
                if pos["ticket"] == r["ticket"]:
                    fill = pos["fill_price"]
                    break
            tick = mt5.symbol_info_tick(cfg.symbol)
            close_price = float(tick.bid) if tick else 0.0
            _tg_safe(notify_force_close, cfg.symbol, fill, close_price, pnl)
            state.notified_today.add(fc_key)

    print(
        f"\n[{cfg.symbol}] ── END OF DAY ─────────────────────\n"
        f"  UTC Date   : {state.date}\n"
        f"  Trades     : {state.trade_count}\n"
        f"  Realized   : ${pnl:+.2f}\n"
        f"───────────────────────────────────────\n"
    )

    # Telegram: end-of-day summary
    _tg_safe(notify_day_end, cfg.symbol, state.date, state.trade_count, pnl)


def _handle_signal(signal: Signal, state: DayState) -> bool:
    # BUG 1 FIX: overshoot filter — check live fill price vs entry level
    fill_price_est = _live_fill_price(cfg.symbol, signal.direction)
    if fill_price_est is not None:
        if signal.direction == "LONG":
            overshoot = fill_price_est - signal.entry_price
        else:
            overshoot = signal.entry_price - fill_price_est
        if overshoot > cfg.max_entry_overshoot_pips:
            log.warning(
                f"[{cfg.symbol}] OVERSHOOT BLOCKED | {signal.direction} | "
                f"fill_est={fill_price_est:.2f} | entry={signal.entry_price:.2f} | "
                f"overshoot={overshoot:.2f} pips > max={cfg.max_entry_overshoot_pips:.1f}"
            )
            print(
                f"[{cfg.symbol}] OVERSHOOT BLOCKED | {signal.direction} | "
                f"overshoot={overshoot:.2f} pips > max {cfg.max_entry_overshoot_pips:.1f} pips"
            )
            return False

    snap    = _snapshot(state.trade_count, state.internal_pnl)
    allowed, reason = can_place_trade(snap, cfg)

    if not allowed:
        print(f"[{cfg.symbol}] BLOCKED | {reason}")
        return False

    print(f"\n[{cfg.symbol}] {signal}")
    print(loss_scenario_summary(snap.realized_pnl, cfg))

    # BUG 3 FIX: set order_in_flight before place_order
    state.order_in_flight = True
    result = place_order(signal, cfg)
    state.order_in_flight = False

    if result.get("success"):
        state.already_traded.add(signal.direction)
        state.trade_count += 1

        # BUG 2 FIX: track position for internal P&L
        ticket = result.get("order", 0)
        actual_fill = result.get("fill_price", signal.entry_price)
        state.track_position(
            ticket=ticket,
            fill_price=actual_fill,
            direction=signal.direction,
            volume=cfg.lot_size,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
        )

        post   = calculate_day_pnl(cfg)
        budget = cfg.max_daily_loss_usd - abs(min(post, 0))
        print(
            f"[{cfg.symbol}] Trade #{state.trade_count} PLACED\n"
            f"  Direction  : {signal.direction}\n"
            f"  Fill       : {actual_fill}\n"
            f"  TP         : {signal.tp_price:.2f}\n"
            f"  SL         : {signal.sl_price:.2f}\n"
            f"  Day PnL    : ${post:+.2f}\n"
            f"  Budget left: ${budget:.0f}\n"
            f"  Profit tgt : +${cfg.daily_profit_target_usd:.0f}"
        )

        # Telegram: trade placed
        _tg_safe(
            notify_trade_placed,
            cfg.symbol, signal.direction, actual_fill,
            signal.sl_price, signal.tp_price,
            cfg.lot_size, cfg.sl_dollar, cfg.tp_dollar,
        )
        return True

    print(f"[{cfg.symbol}] ORDER FAILED | retcode={result.get('retcode')} | {result.get('comment')}")
    return False


def _print_day_levels(levels: ThresholdLevels):
    """Print today's start price levels with IST equivalent time."""
    now_utc = datetime.now(timezone.utc)
    ist_time = now_utc.astimezone(_IST)
    utc_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    ist_midnight = utc_midnight.astimezone(_IST)
    print(
        f"  Start price locked at: UTC 00:00 (IST {ist_midnight.strftime('%H:%M')})\n"
        f"  Current IST time    : {ist_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(levels.display())


def run():
    print(cfg.summary())
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    state        = DayState()
    state.reset(_today())
    force_closed = False
    last_log_ts  = 0.0
    last_warn_ts = 0.0

    print(f"[{cfg.symbol}] Waiting for start price...")
    print(f"[{cfg.symbol}] Reading from: data/start_price/{cfg.symbol}.json")

    while True:
        try:
            today = _today()

            # ── DATE ROLLOVER (UTC midnight) ─────────────────────────────────
            if today != state.date:
                state.reset(today)
                force_closed = False

            # ── START PRICE ──────────────────────────────────────────────────
            if state.start_price is None:
                price = read_start_price(cfg)
                if price is None:
                    time.sleep(2.0)
                    continue
                state.start_price = price
                state.levels      = compute_levels(price, cfg)
                _print_day_levels(state.levels)

                # Telegram: day start notification (once per day)
                if TELEGRAM and "day_start" not in state.notified_today:
                    lv = state.levels
                    _tg_safe(
                        notify_day_start,
                        cfg.symbol, price,
                        lv.long_entry, lv.long_tp, lv.long_sl,
                        lv.short_entry, lv.short_tp, lv.short_sl,
                        cfg.lot_size,
                    )
                    state.notified_today.add("day_start")

            # ── FORCE CLOSE ──────────────────────────────────────────────────
            if is_force_close_time(cfg) and not force_closed:
                _force_close(state)
                force_closed = True
                time.sleep(60.0)
                continue

            if force_closed:
                time.sleep(10.0)
                continue

            # ── SESSION GATE ─────────────────────────────────────────────────
            if not is_session_allowed(cfg):
                time.sleep(5.0)
                continue

            # ── BUG 2 FIX: update internal P&L from closed positions ────────
            closed_positions = state.update_closed_positions()

            # Telegram: notify TP/SL for each closed position (with dedup)
            for cp in closed_positions:
                ticket = cp["ticket"]
                pnl = cp["pnl"]
                if pnl > 0:
                    tp_key = f"tp_{ticket}"
                    if TELEGRAM and tp_key not in state.notified_today:
                        _tg_safe(
                            notify_tp,
                            cfg.symbol, cp["direction"], cp["fill_price"],
                            cp["tp_price"], pnl,
                        )
                        state.notified_today.add(tp_key)
                elif pnl < 0:
                    sl_key = f"sl_{ticket}"
                    if TELEGRAM and sl_key not in state.notified_today:
                        _tg_safe(
                            notify_sl,
                            cfg.symbol, cp["direction"], cp["fill_price"],
                            cp["sl_price"], pnl,
                        )
                        state.notified_today.add(sl_key)
                else:
                    fc_key = f"fc_{ticket}"
                    if TELEGRAM and fc_key not in state.notified_today:
                        _tg_safe(
                            notify_force_close,
                            cfg.symbol, cp["fill_price"], cp["fill_price"], 0.0,
                        )
                        state.notified_today.add(fc_key)

            # Use worse of MT5 history and internal tracking for P&L checks
            mt5_realized = calculate_day_pnl(cfg)
            realized = min(mt5_realized, state.internal_pnl) if state.internal_pnl < 0 else mt5_realized

            # ── DAILY PROFIT STOP ────────────────────────────────────────────
            if is_daily_profit_hit(realized, cfg):
                now = time.time()
                if now - last_warn_ts >= 60.0:
                    print(
                        f"[{cfg.symbol}] PROFIT TARGET HIT | "
                        f"PnL=${realized:+.2f} | target=+${cfg.daily_profit_target_usd:.0f}"
                    )
                    last_warn_ts = now
                # Telegram: one-time profit target notification
                if TELEGRAM and "profit_target" not in state.notified_today:
                    _tg_safe(notify_profit_target, cfg.symbol, realized)
                    state.notified_today.add("profit_target")
                time.sleep(10.0)
                continue

            # ── DAILY LOSS GATE ──────────────────────────────────────────────
            if is_daily_limit_breached(realized, cfg):
                now = time.time()
                if now - last_warn_ts >= 60.0:
                    src = "internal" if state.internal_pnl < mt5_realized else "MT5"
                    print(
                        f"[{cfg.symbol}] LOSS LIMIT BREACHED ({src}) | "
                        f"PnL=${realized:+.2f} | limit=-${cfg.max_daily_loss_usd:.0f}"
                    )
                    if state.internal_pnl < mt5_realized:
                        log.warning(
                            f"[{cfg.symbol}] Internal P&L ({state.internal_pnl:+.2f}) "
                            f"breached limit before MT5 history ({mt5_realized:+.2f}) caught up"
                        )
                    last_warn_ts = now
                # Telegram: one-time loss limit notification
                if TELEGRAM and "loss_limit" not in state.notified_today:
                    _tg_safe(notify_loss_limit, cfg.symbol, realized)
                    state.notified_today.add("loss_limit")
                time.sleep(10.0)
                continue

            # ── MAX TRADES ───────────────────────────────────────────────────
            if state.trade_count >= cfg.max_trades_per_day:
                time.sleep(5.0)
                continue

            # ── BUG 3 FIX: skip if order is in flight ───────────────────────
            if state.order_in_flight:
                time.sleep(cfg.poll_seconds)
                continue

            # ── PRICE ────────────────────────────────────────────────────────
            mid = _mid(cfg.symbol)
            if mid is None:
                time.sleep(cfg.poll_seconds)
                continue

            # ── STATUS LOG (every 30s) ───────────────────────────────────────
            now = time.time()
            if now - last_log_ts >= 30.0:
                budget = cfg.max_daily_loss_usd - abs(min(realized, 0))
                print(
                    f"[{cfg.symbol}] {session_status(cfg)} | "
                    f"Mid={mid:.2f} | "
                    f"LEntry={state.levels.long_entry:.2f} | "
                    f"SEntry={state.levels.short_entry:.2f} | "
                    f"Trades={state.trade_count}/{cfg.max_trades_per_day} | "
                    f"PnL=${realized:+.2f} | Budget=${budget:.0f}"
                )
                last_log_ts = now

            # ── SIGNAL → RISK GATE → ORDER ───────────────────────────────────
            sig = evaluate_signal(mid, state.levels, state.already_traded, cfg)
            if sig:
                _handle_signal(sig, state)

            time.sleep(cfg.poll_seconds)

        except KeyboardInterrupt:
            print(f"\n[{cfg.symbol}] Stopped manually.")
            close_all_by_magic(cfg)
            break
        except Exception as e:
            print(f"[{cfg.symbol}] Error: {repr(e)}")
            state.order_in_flight = False  # Reset on error to avoid permanent lock
            time.sleep(2.0)


if __name__ == "__main__":
    run()
