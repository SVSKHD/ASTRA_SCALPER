from __future__ import annotations

# =============================================================================
# STRATEGY RUNNER — MAIN LOOP
# Reads start price from: data/start_price/<symbol>.json
# =============================================================================

import sys
import time
import logging
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta

from config import cfg
from start_reader import read_start_price, read_start_payload, _utc_date_today
from threshold import compute_levels, ThresholdLevels
from trade_signal import evaluate_signal, Signal, Direction
from session_guard import is_session_allowed, is_force_close_time, is_news_blackout_day, session_status
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

# ── ML / LOGGING (graceful fallback if files not yet present) ─────────────
try:
    from signal_logger import log_signal, update_outcome as _log_update_outcome
    SIGNAL_LOGGER = True
except Exception as _e:
    SIGNAL_LOGGER = False
    print(f"[Runner] signal_logger not loaded: {_e}")

try:
    from signal_filter import apply_filters
    SIGNAL_FILTER = True
except Exception as _e:
    SIGNAL_FILTER = False
    print(f"[Runner] signal_filter not loaded: {_e}")

try:
    from telegram_commands import CommandListener
    CMD_LISTENER = True
except Exception as _e:
    CMD_LISTENER = False
    print(f"[Runner] telegram_commands not loaded: {_e}")

log = logging.getLogger("runner")

# IST = UTC + 5:30
_IST = timezone(timedelta(hours=5, minutes=30))

_MAX_CLOSE_RETRIES = 3

# Overshoot tolerance at close-confirm execution time.
# 3 pips (config default) was killing every fast move — close-confirm delays
# entry by one full M5 bar, so next-bar open is routinely 5-10 pips past
# the entry level on any clean directional day. 8 pips gives enough room
# while still rejecting genuine gap-chases.
_MAX_OVERSHOOT_PIPS: float = 8.0


def _tg_safe(fn, *args, **kwargs):
    if not TELEGRAM:
        return
    try:
        fn(*args, **kwargs)
    except Exception as e:
        print(f"[Telegram] Send failed: {repr(e)}")


def _lookup_deal_pnl(ticket: int) -> float | None:
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
        self.date:            str                    = ""
        self.start_price:     float | None           = None
        self.levels:          ThresholdLevels | None = None
        self.already_traded:  set[Direction]         = set()
        self.trade_count:     int                    = 0
        # BUG 2 FIX: internal P&L tracking
        self.internal_pnl:    float                  = 0.0
        self._tracked_positions: list[dict]          = []
        # BUG 3 FIX: prevent double-firing
        self.order_in_flight: bool                   = False
        # Telegram dedup
        self.notified_today:  set[str]               = set()
        self._close_retry_count: dict[int, int]      = {}
        self.blackout_notified: bool                 = False
        # ── ML TRAINING CONTEXT ───────────────────────────────────────────
        self.prev_day_outcome:    str   = "UNKNOWN"  # TP / SL / NO_SIGNAL
        self.consecutive_losses:  int   = 0
        self._current_log_id:     str   = ""         # updated after close
        self._recent_bars_m5:     list  = []         # last 40 M5 bars
        # ── CLOSE-CONFIRM STATE ───────────────────────────────────────────
        # Signal detected on bar N — execute at open of bar N+1.
        # pending_signal      : the signal waiting for next bar
        # pending_bar_slot    : 5-min slot string when signal fired ("HH:MM")
        # pending_log_id      : signal_logger id for the pending signal
        self.pending_signal:   Signal | None = None
        self.pending_bar_slot: str           = ""
        self.pending_log_id:   str           = ""
        # ── DAY RANGE TELEMETRY ───────────────────────────────────────────
        # Track how far price has moved from start in each direction today.
        # Updated on every mid price tick in the main loop.
        self.day_high:      float = 0.0   # highest mid seen today
        self.day_low:       float = 0.0   # lowest mid seen today
        self.day_high_time: str   = ""    # IST time string of day high
        self.day_low_time:  str   = ""    # IST time string of day low

    def reset(self, date: str,
              prev_outcome: str = "UNKNOWN",
              consec_losses: int = 0):
        self.date                = date
        self.start_price         = None
        self.levels              = None
        self.already_traded      = set()
        self.trade_count         = 0
        self.internal_pnl        = 0.0
        self._tracked_positions  = []
        self.order_in_flight     = False
        self.notified_today      = set()
        self._close_retry_count  = {}
        self.blackout_notified   = False
        # ML context carries forward across day boundary
        self.prev_day_outcome    = prev_outcome
        self.consecutive_losses  = consec_losses
        self._current_log_id     = ""
        self._recent_bars_m5     = []
        # Close-confirm state resets each day
        self.pending_signal      = None
        self.pending_bar_slot    = ""
        self.pending_log_id      = ""
        # Day range resets each day
        self.day_high      = 0.0
        self.day_low       = 0.0
        self.day_high_time = ""
        self.day_low_time  = ""
        print(f"\n{'='*55}\n  DAY RESET → {date}\n{'='*55}\n")

    def track_position(self, ticket: int, fill_price: float,
                       direction: str, volume: float,
                       sl_price: float, tp_price: float):
        self._tracked_positions.append({
            "ticket":     ticket,
            "fill_price": fill_price,
            "direction":  direction,
            "volume":     volume,
            "sl_price":   sl_price,
            "tp_price":   tp_price,
        })

    def update_closed_positions(self) -> list[dict]:
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
            pnl = _lookup_deal_pnl(ticket)

            if pnl is None:
                retries = self._close_retry_count.get(ticket, 0)
                if retries < _MAX_CLOSE_RETRIES:
                    self._close_retry_count[ticket] = retries + 1
                    log.info(f"[{cfg.symbol}] #{ticket} not in history yet (retry {retries+1}/{_MAX_CLOSE_RETRIES})")
                    still_open.append(pos)
                    continue
                log.warning(f"[{cfg.symbol}] #{ticket} deal history unavailable — using tick estimate")
                tick = mt5.symbol_info_tick(cfg.symbol)
                if tick is not None:
                    if pos["direction"] == "LONG":
                        pnl = (float(tick.bid) - pos["fill_price"]) * pos["volume"] * cfg.pip_value_per_lot
                    else:
                        pnl = (pos["fill_price"] - float(tick.ask)) * pos["volume"] * cfg.pip_value_per_lot
                else:
                    pnl = -cfg.sl_dollar

            self._close_retry_count.pop(ticket, None)
            self.internal_pnl += pnl
            log.info(f"[{cfg.symbol}] #{ticket} closed | pnl={pnl:+.2f} | total={self.internal_pnl:+.2f}")
            closed.append({
                "ticket":     ticket,
                "direction":  pos["direction"],
                "fill_price": pos["fill_price"],
                "sl_price":   pos["sl_price"],
                "tp_price":   pos["tp_price"],
                "pnl":        pnl,
            })

        self._tracked_positions = still_open
        return closed


def _today() -> str:
    return _utc_date_today()


def _mid(symbol: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if not tick or tick.time == 0:
        return None
    bid, ask = float(tick.bid), float(tick.ask)
    return (bid + ask) / 2.0 if bid > 0 and ask > 0 else None


def _live_fill_price(symbol: str, direction: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return float(tick.ask) if direction == "LONG" else float(tick.bid)


def _snapshot(trade_count: int, internal_pnl: float) -> RiskSnapshot:
    mt5_pnl = calculate_day_pnl(cfg)
    effective_pnl = min(mt5_pnl, internal_pnl) if internal_pnl < 0 else mt5_pnl
    if internal_pnl < mt5_pnl:
        log.warning(f"[{cfg.symbol}] INTERNAL P&L ({internal_pnl:+.2f}) worse than MT5 ({mt5_pnl:+.2f})")
    return RiskSnapshot(
        realized_pnl        = effective_pnl,
        open_pnl            = calculate_open_pnl(cfg),
        trade_count         = trade_count,
        open_position_count = len(get_open_positions(cfg)),
    )


def _fetch_m5_bars(symbol: str, count: int = 40) -> list:
    """Fetch last N closed M5 bars as list of dicts. Returns [] on failure."""
    try:
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, count)
        if bars is None or len(bars) == 0:
            return []
        return [
            {"open": float(b[1]), "high": float(b[2]),
             "low":  float(b[3]), "close": float(b[4])}
            for b in bars
        ]
    except Exception:
        return []


def _current_bar_slot(now_utc: datetime | None = None) -> str:
    """
    Returns the current M5 bar slot as 'HH:MM' (floored to 5-min boundary).
    e.g. any time between 02:10:00 and 02:14:59 → '02:10'
    Used by close-confirm to detect when the next bar has opened.
    """
    t = now_utc or datetime.now(timezone.utc)
    slot_min = (t.minute // 5) * 5
    return f"{t.hour:02d}:{slot_min:02d}"


def _trend_filter_ok(direction: str, mid: float, start_price: float) -> bool:
    """
    Mirrors backtest --trend-filter exactly.
    LONG  only allowed when mid >= start_price (price above day anchor).
    SHORT only allowed when mid <= start_price (price below day anchor).
    """
    if direction == "LONG":
        return mid >= start_price
    else:
        return mid <= start_price


def _force_close(state: DayState):
    # Cancel any pending close-confirm signal — don't execute after EOD
    if state.pending_signal is not None:
        print(f"[{cfg.symbol}] FORCE CLOSE — cancelling pending {state.pending_signal.direction} signal")
        if SIGNAL_LOGGER and state.pending_log_id:
            _log_update_outcome(state.pending_log_id, "SKIPPED", 0.0)
        state.pending_signal   = None
        state.pending_bar_slot = ""
        state.pending_log_id   = ""

    print(f"[{cfg.symbol}] FORCE CLOSE — EOD")
    results = close_all_by_magic(cfg)
    for r in results:
        print(f"  {'OK' if r['success'] else 'FAIL'} ticket={r['ticket']} retcode={r['retcode']}")
    pnl = calculate_day_pnl(cfg)

    for r in results:
        fc_key = f"fc_{r['ticket']}"
        if TELEGRAM and fc_key not in state.notified_today:
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
    _tg_safe(notify_day_end, cfg.symbol, state.date, state.trade_count, pnl)


def _handle_signal_execute(signal: Signal, state: DayState, pre_log_id: str = "") -> bool:
    """
    Execute a confirmed signal (close-confirm already passed).
    Called on the NEW bar after signal was detected on previous bar.
    Risk check was done pre-confirmation — we do a final check here
    in case P&L changed while waiting for bar close.
    """
    # ── Fetch current spread ──────────────────────────────────────────────
    spread_pips = 0.0
    tick = mt5.symbol_info_tick(cfg.symbol)
    if tick:
        spread_pips = round(float(tick.ask) - float(tick.bid), 2)

    # ── Session context for filters ───────────────────────────────────────
    now_utc     = datetime.now(timezone.utc)
    hour_utc    = now_utc.hour
    day_of_week = now_utc.weekday()   # 0=Mon, 4=Fri

    # ── RULE-BASED FILTERS — ALL DISABLED ────────────────────────────────
    # ATR filter: blocks any elevated-ATR day — which is every clean
    #   directional move. A 35-pip smooth run IS elevated ATR by definition.
    # Candle filter: body < 45% blocks valid breakouts with wicks. Not
    #   relevant for a one-trade-per-day threshold strategy.
    # Session filter: was blocking Mon 00-06 UTC (05:30-11:30 IST) — the
    #   cleanest directional gold window before London noise arrives.
    # Remaining guards: trend filter + close-confirm + overshoot + daily loss limit.
    filter_reason = ""
    if SIGNAL_FILTER:
        result = apply_filters(
            bars_m5        = state._recent_bars_m5,
            hour_utc       = hour_utc,
            day_of_week    = day_of_week,
            enable_atr     = False,
            enable_candle  = False,
            enable_session = False,
        )
        if not result.passed:
            filter_reason = result.reason
            print(f"[{cfg.symbol}] SIGNAL FILTERED | {signal.direction} | {filter_reason}")
            if SIGNAL_LOGGER:
                log_signal(
                    signal             = signal,
                    bars_m5            = state._recent_bars_m5,
                    spread_pips        = spread_pips,
                    prev_day_outcome   = state.prev_day_outcome,
                    consecutive_losses = state.consecutive_losses,
                    filter_applied     = filter_reason,
                    outcome            = "SKIPPED",
                )
            return False

    # ── LOG SIGNAL (will be traded — outcome=PENDING) ─────────────────────
    log_id = pre_log_id
    if SIGNAL_LOGGER and not log_id:
        log_id = log_signal(
            signal             = signal,
            bars_m5            = state._recent_bars_m5,
            spread_pips        = spread_pips,
            prev_day_outcome   = state.prev_day_outcome,
            consecutive_losses = state.consecutive_losses,
            filter_applied     = "",
            outcome            = "PENDING",
        )
        state._current_log_id = log_id

    # ── FINAL RISK CHECK (re-check in case P&L changed during bar wait) ──
    snap = _snapshot(state.trade_count, state.internal_pnl)
    allowed, reason = can_place_trade(snap, cfg)
    if not allowed:
        print(f"[{cfg.symbol}] BLOCKED (post-confirm) | {reason}")
        if SIGNAL_LOGGER and log_id:
            _log_update_outcome(log_id, "SKIPPED", 0.0)
        return False

    # ── OVERSHOOT CHECK at execution time ────────────────────────────────
    # On close-confirm the entry is at next bar open — check we haven't
    # gapped past the entry level by more than the allowed overshoot.
    fill_price_est = _live_fill_price(cfg.symbol, signal.direction)
    if fill_price_est is not None:
        if signal.direction == "LONG":
            overshoot = fill_price_est - signal.entry_price
        else:
            overshoot = signal.entry_price - fill_price_est
        if overshoot > _MAX_OVERSHOOT_PIPS:
            log.warning(
                f"[{cfg.symbol}] OVERSHOOT BLOCKED | {signal.direction} | "
                f"overshoot={overshoot:.2f} > max={_MAX_OVERSHOOT_PIPS}"
            )
            print(f"[{cfg.symbol}] OVERSHOOT BLOCKED | overshoot={overshoot:.2f} pips (limit={_MAX_OVERSHOOT_PIPS})")
            if SIGNAL_LOGGER and log_id:
                _log_update_outcome(log_id, "SKIPPED", 0.0)
            return False

    print(f"\n[{cfg.symbol}] {signal}")
    print(loss_scenario_summary(snap.realized_pnl, cfg))

    # BUG 3 FIX: order in flight guard
    state.order_in_flight = True
    result = place_order(signal, cfg)
    state.order_in_flight = False

    if result.get("success"):
        state.already_traded.add(signal.direction)
        state.trade_count += 1

        ticket      = result.get("order", 0)
        actual_fill = result.get("fill_price", signal.entry_price)
        state.track_position(
            ticket     = ticket,
            fill_price = actual_fill,
            direction  = signal.direction,
            volume     = cfg.lot_size,
            sl_price   = signal.sl_price,
            tp_price   = signal.tp_price,
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
        _tg_safe(
            notify_trade_placed,
            cfg.symbol, signal.direction, actual_fill,
            signal.sl_price, signal.tp_price,
            cfg.lot_size, cfg.sl_dollar, cfg.tp_dollar,
        )
        return True

    print(f"[{cfg.symbol}] ORDER FAILED | retcode={result.get('retcode')} | {result.get('comment')}")
    if SIGNAL_LOGGER and log_id:
        _log_update_outcome(log_id, "SKIPPED", 0.0)
    return False


def _print_day_levels(levels: ThresholdLevels):
    now_utc = datetime.now(timezone.utc)
    ist_time = now_utc.astimezone(_IST)
    utc_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    ist_midnight = utc_midnight.astimezone(_IST)
    print(
        f"  Start price locked at: UTC 00:00 (IST {ist_midnight.strftime('%H:%M')})\n"
        f"  Current IST time    : {ist_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(levels.display())


def _catchup_analysis(state: DayState) -> None:
    """
    Called once after start price is locked on bot startup.
    Fetches all M5 bars from UTC midnight to now and reports:
      - Day high / low from start price
      - Whether any entry level was crossed while the bot was offline
      - Estimated P&L of the missed trade (if any)
    Sends a Telegram alert if a missed trade is detected.
    """
    if state.start_price is None or state.levels is None:
        return

    now_utc     = datetime.now(timezone.utc)
    midnight    = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    lv          = state.levels
    start       = state.start_price

    try:
        bars = mt5.copy_rates_range(cfg.symbol, mt5.TIMEFRAME_M5, midnight, now_utc)
    except Exception as e:
        print(f"[{cfg.symbol}] CATCHUP: failed to fetch bars — {e}")
        return

    if bars is None or len(bars) == 0:
        print(f"[{cfg.symbol}] CATCHUP: no M5 bars available since midnight UTC")
        return

    # ── scan bars for high/low and entry crossings ────────────────────────
    day_high = day_low = float(bars[0][1])   # open of first bar
    day_high_bar = day_low_bar = None

    long_entry_crossed  = False
    short_entry_crossed = False
    long_tp_crossed     = False
    short_tp_crossed    = False
    long_cross_time     = ""
    short_cross_time    = ""

    for b in bars:
        bar_time = datetime.fromtimestamp(int(b[0]), tz=timezone.utc).astimezone(_IST)
        bar_time_str = bar_time.strftime("%H:%M IST")
        hi = float(b[2])
        lo = float(b[3])

        if hi > day_high:
            day_high = hi
            day_high_bar = bar_time_str
        if lo < day_low:
            day_low = lo
            day_low_bar = bar_time_str

        if not long_entry_crossed and hi >= lv.long_entry:
            long_entry_crossed = True
            long_cross_time    = bar_time_str
        if long_entry_crossed and not long_tp_crossed and hi >= lv.long_tp:
            long_tp_crossed = True

        if not short_entry_crossed and lo <= lv.short_entry:
            short_entry_crossed = True
            short_cross_time    = bar_time_str
        if short_entry_crossed and not short_tp_crossed and lo <= lv.short_tp:
            short_tp_crossed = True

    # seed DayState high/low so status log is accurate immediately
    state.day_high      = day_high
    state.day_low       = day_low
    state.day_high_time = day_high_bar or ""
    state.day_low_time  = day_low_bar  or ""

    up_pips   = round(day_high - start, 1)
    down_pips = round(start - day_low,  1)

    sep = "─" * 62
    print(f"\n[{cfg.symbol}] CATCHUP ANALYSIS ── since midnight UTC")
    print(sep)
    print(f"  Start price : {start:.2f}")
    print(f"  Day high    : {day_high:.2f}  ({up_pips:+.1f} pips)  @ {day_high_bar}")
    print(f"  Day low     : {day_low:.2f}  (-{down_pips:.1f} pips)  @ {day_low_bar}")
    print(f"  Bars scanned: {len(bars)}")
    print(sep)

    missed_direction = ""
    missed_pnl       = 0.0

    if long_entry_crossed:
        tp_tag = f"TP HIT ({lv.long_tp:.2f}) → +${cfg.tp_dollar:.0f}" if long_tp_crossed else f"TP NOT hit ({lv.long_tp:.2f})"
        print(f"  ⚠️  MISSED LONG — entry {lv.long_entry:.2f} crossed @ {long_cross_time}  |  {tp_tag}")
        # Mark LONG consumed — blocks re-entry if price drops back through long entry.
        # A pullback re-entry is not a fresh breakout; it's a reversal trap.
        state.already_traded.add("LONG")
        print(f"  → LONG marked consumed — re-entry on pullback blocked for today")
        if long_tp_crossed:
            missed_direction = "LONG"
            missed_pnl       = cfg.tp_dollar

    if short_entry_crossed:
        tp_tag = f"TP HIT ({lv.short_tp:.2f}) → +${cfg.tp_dollar:.0f}" if short_tp_crossed else f"TP NOT hit ({lv.short_tp:.2f})"
        print(f"  ⚠️  MISSED SHORT — entry {lv.short_entry:.2f} crossed @ {short_cross_time}  |  {tp_tag}")
        # Mark SHORT consumed — blocks re-entry as price recovers back through short entry.
        # Today's case: crashed to 4604, recovering toward 4648. Do NOT short the recovery.
        state.already_traded.add("SHORT")
        print(f"  → SHORT marked consumed — re-entry on recovery blocked for today")
        if short_tp_crossed:
            missed_direction = "SHORT"
            missed_pnl       = cfg.tp_dollar

    if not long_entry_crossed and not short_entry_crossed:
        print(f"  ✅ No entry level crossed while offline — nothing missed")

    print(sep + "\n")

    # ── Telegram alert for missed full trade ──────────────────────────────
    if missed_direction and TELEGRAM:
        msg = (
            f"<b>⚠️ MISSED TRADE — {cfg.symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Bot was offline during the move.\n"
            f"Direction : {missed_direction}\n"
            f"Entry crossed : "
            f"{lv.long_entry:.2f} @ {long_cross_time}" if missed_direction == "LONG"
            else f"{lv.short_entry:.2f} @ {short_cross_time}\n"
            f"TP hit    : YES → +${missed_pnl:.0f} left on the table\n"
            f"Day low   : {day_low:.2f}  (-{down_pips:.1f} pips from start)\n"
            f"Day high  : {day_high:.2f}  (+{up_pips:.1f} pips from start)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Bot is now live. Next signal will fire."
        )
        _tg_safe(lambda m: __import__('telegram_notify', fromlist=['send']).send(m), msg)


def run():
    print(cfg.summary())
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    state        = DayState()
    state.reset(_today())
    force_closed = False
    last_log_ts  = 0.0
    last_warn_ts = 0.0

    # ── TELEGRAM COMMAND LISTENER ───────────────────────────────────────────
    cmd_listener = None
    if CMD_LISTENER:
        cmd_listener = CommandListener(
            state       = state,
            cfg         = cfg,
            get_pnl_fn  = lambda: calculate_day_pnl(cfg),
            get_open_fn = lambda: calculate_open_pnl(cfg),
        )
        cmd_listener.start()

    print(f"[{cfg.symbol}] Waiting for start price...")
    print(f"[{cfg.symbol}] Reading from: data/start_price/{cfg.symbol}.json")
    print(f"[{cfg.symbol}] Entry mode     : CLOSE-CONFIRM (waits for M5 bar close)")
    print(f"[{cfg.symbol}] Overshoot limit: {_MAX_OVERSHOOT_PIPS} pips (close-confirm execution tolerance)")
    print(f"[{cfg.symbol}] Trend filter   : ACTIVE (LONG only above start, SHORT only below)")
    print(f"[{cfg.symbol}] Rule filters   : DISABLED (ATR=off, candle=off, session=off)")
    print(f"[{cfg.symbol}] Protection     : trend-filter + close-confirm + overshoot + daily-loss-limit")
    if SIGNAL_LOGGER:
        print(f"[{cfg.symbol}] Signal logger  : ACTIVE  → data/signal_log.csv")

    while True:
        try:
            # ── DATE ROLLOVER — driven by start_price file, NOT UTC clock ────
            # Only reset when run_start_price.py has LOCKED a new day's price.
            # This prevents the "limbo" state where state.date=Apr3 but
            # start_price file still shows Apr2 (not yet locked).
            today_utc = _today()   # UTC wall clock date

            # ── DATE ROLLOVER — file-driven, forward-only ────────────────────
            # Reset ONLY when start_price file shows a new date that is:
            #   1. LOCKED
            #   2. Different from current state.date
            #   3. Equal to today's UTC date (never reset backward to a past date)
            #
            # This prevents the "limbo" bug where:
            #   - Bot starts on Apr 3 UTC
            #   - File still has Apr 2 (not yet locked for Apr 3)
            #   - Old logic reset backward to Apr 2 → picked up Apr 2's -$526 P&L
            payload = read_start_payload(cfg)
            if payload:
                file_date   = payload.get("date_mt5", "")
                file_status = (payload.get("start") or {}).get("status", "")
                if (file_date
                        and file_status == "LOCKED"
                        and file_date != state.date
                        and file_date >= today_utc):   # ← forward-only guard
                    _prev   = state.prev_day_outcome
                    _consec = state.consecutive_losses
                    state.reset(file_date, _prev, _consec)
                    force_closed = False
                    print(f"[{cfg.symbol}] DAY ROLLOVER → {file_date}")
                    if cmd_listener:
                        cmd_listener._state = state

            today = today_utc

            # ── START PRICE ──────────────────────────────────────────────────
            if state.start_price is None:
                price = read_start_price(cfg)
                if price is None:
                    # File not locked for today yet — wait quietly
                    time.sleep(2.0)
                    continue
                state.start_price = price
                state.levels      = compute_levels(price, cfg)
                _print_day_levels(state.levels)
                _catchup_analysis(state)   # scan bars since midnight, report missed moves

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

            # ── NEWS BLACKOUT DAY ────────────────────────────────────────────
            if is_news_blackout_day(cfg):
                now = time.time()
                if now - last_warn_ts >= 3600.0:
                    print(f"[{cfg.symbol}] NEWS BLACKOUT DAY — trading suspended")
                    last_warn_ts = now
                if TELEGRAM and not state.blackout_notified:
                    try:
                        from telegram_notify import _now_ist, send as _tg_send_fn
                        _tg_send_fn(
                            f"<b>NEWS BLACKOUT — {cfg.symbol}</b>\n"
                            f"Date: {today}\nTrading suspended all day.\n"
                        )
                    except Exception as e:
                        print(f"[Telegram] Send failed: {repr(e)}")
                    state.blackout_notified = True
                time.sleep(60.0)
                continue

            # ── FETCH M5 BARS — only needed for signal_logger ML training data ──
            if SIGNAL_LOGGER:
                bars = _fetch_m5_bars(cfg.symbol, 40)
                if bars:
                    state._recent_bars_m5 = bars

            # ── BUG 2 FIX: update closed positions ───────────────────────────
            closed_positions = state.update_closed_positions()

            # Telegram + ML outcome update for each closed position
            for cp in closed_positions:
                ticket = cp["ticket"]
                pnl    = cp["pnl"]

                if pnl > 0:
                    tp_key = f"tp_{ticket}"
                    if TELEGRAM and tp_key not in state.notified_today:
                        _tg_safe(notify_tp, cfg.symbol, cp["direction"],
                                 cp["fill_price"], cp["tp_price"], pnl)
                        state.notified_today.add(tp_key)
                elif pnl < 0:
                    sl_key = f"sl_{ticket}"
                    if TELEGRAM and sl_key not in state.notified_today:
                        _tg_safe(notify_sl, cfg.symbol, cp["direction"],
                                 cp["fill_price"], cp["sl_price"], pnl)
                        state.notified_today.add(sl_key)
                else:
                    fc_key = f"fc_{ticket}"
                    if TELEGRAM and fc_key not in state.notified_today:
                        _tg_safe(notify_force_close, cfg.symbol,
                                 cp["fill_price"], cp["fill_price"], 0.0)
                        state.notified_today.add(fc_key)

                # ── ML: update outcome + streak tracking ─────────────────────
                if SIGNAL_LOGGER and state._current_log_id:
                    outcome_str = "TP" if pnl > 0 else ("SL" if pnl < 0 else "FC")
                    _log_update_outcome(state._current_log_id, outcome_str, pnl)
                    state._current_log_id = ""
                    if outcome_str == "SL":
                        state.consecutive_losses += 1
                        state.prev_day_outcome    = "SL"
                    elif outcome_str == "TP":
                        state.consecutive_losses  = 0
                        state.prev_day_outcome    = "TP"

            # ── P&L checks ───────────────────────────────────────────────────
            mt5_realized = calculate_day_pnl(cfg)
            realized = min(mt5_realized, state.internal_pnl) if state.internal_pnl < 0 else mt5_realized

            # ── DAILY PROFIT STOP ────────────────────────────────────────────
            if is_daily_profit_hit(realized, cfg):
                now = time.time()
                if now - last_warn_ts >= 60.0:
                    print(f"[{cfg.symbol}] PROFIT TARGET HIT | PnL=${realized:+.2f}")
                    last_warn_ts = now
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
                    print(f"[{cfg.symbol}] LOSS LIMIT BREACHED ({src}) | PnL=${realized:+.2f}")
                    last_warn_ts = now
                if TELEGRAM and "loss_limit" not in state.notified_today:
                    _tg_safe(notify_loss_limit, cfg.symbol, realized)
                    state.notified_today.add("loss_limit")
                time.sleep(10.0)
                continue

            # ── MAX TRADES ───────────────────────────────────────────────────
            if state.trade_count >= cfg.max_trades_per_day:
                time.sleep(5.0)
                continue

            # ── BUG 3 FIX: order in flight ───────────────────────────────────
            if state.order_in_flight:
                time.sleep(cfg.poll_seconds)
                continue

            # ── PRICE ────────────────────────────────────────────────────────
            mid = _mid(cfg.symbol)
            if mid is None:
                time.sleep(cfg.poll_seconds)
                continue

            # ── TRACK DAY HIGH / LOW ──────────────────────────────────────────
            if state.start_price is not None:
                now_ist_str = datetime.now(timezone.utc).astimezone(_IST).strftime("%H:%M IST")
                if state.day_high == 0.0 or mid > state.day_high:
                    state.day_high      = mid
                    state.day_high_time = now_ist_str
                if state.day_low == 0.0 or mid < state.day_low:
                    state.day_low      = mid
                    state.day_low_time = now_ist_str

            # ── STATUS LOG (every 30s) ───────────────────────────────────────
            now = time.time()
            if now - last_log_ts >= 30.0:
                budget      = cfg.max_daily_loss_usd - abs(min(realized, 0))
                pips_from_s = round(mid - state.start_price, 2)
                direction   = f"▲ UP   +{pips_from_s:.1f} pips" if pips_from_s >= 0 else f"▼ DOWN  {pips_from_s:.1f} pips"
                lv          = state.levels

                # trade status line
                if state.pending_signal is not None:
                    trade_status = (
                        f"⏳ PENDING {state.pending_signal.direction} "
                        f"@ bar {state.pending_bar_slot} — waiting for bar close"
                    )
                elif state.trade_count > 0 and state._tracked_positions:
                    pos   = state._tracked_positions[0]
                    pips_live = (
                        round(mid - pos["fill_price"], 2) if pos["direction"] == "LONG"
                        else round(pos["fill_price"] - mid, 2)
                    )
                    trade_status = (
                        f"🟢 IN TRADE {pos['direction']} | fill={pos['fill_price']:.2f} | "
                        f"live={pips_live:+.1f} pips | TP={pos['tp_price']:.2f} SL={pos['sl_price']:.2f}"
                    )
                elif state.trade_count > 0:
                    trade_status = f"✅ DONE — {state.trade_count} trade(s) closed today | PnL=${realized:+.2f}"
                else:
                    # show distance to nearest entry
                    dist_long  = round(lv.long_entry  - mid, 1)
                    dist_short = round(mid - lv.short_entry, 1)
                    if dist_long <= dist_short:
                        trade_status = f"👀 NO TRADE — {dist_long:.1f} pips to LONG entry ({lv.long_entry:.2f})"
                    else:
                        trade_status = f"👀 NO TRADE — {dist_short:.1f} pips to SHORT entry ({lv.short_entry:.2f})"

                now_ist = datetime.now(timezone.utc).astimezone(_IST)
                up_pips   = round(state.day_high - state.start_price, 1) if state.day_high else 0.0
                down_pips = round(state.start_price - state.day_low,  1) if state.day_low  else 0.0
                print(
                    f"\n[{cfg.symbol}] ── {now_ist.strftime('%H:%M:%S IST')} ──────────────────────────────\n"
                    f"  Start     : {state.start_price:.2f}  →  Mid: {mid:.2f}  |  {direction}\n"
                    f"  Day range : ▲ high {state.day_high:.2f} (+{up_pips:.1f} pips) @ {state.day_high_time}"
                    f"  |  ▼ low {state.day_low:.2f} (-{down_pips:.1f} pips) @ {state.day_low_time}\n"
                    f"  Levels    : L-entry={lv.long_entry:.2f}  L-TP={lv.long_tp:.2f}  |  "
                    f"S-entry={lv.short_entry:.2f}  S-TP={lv.short_tp:.2f}\n"
                    f"  Trade     : {trade_status}\n"
                    f"  Risk      : PnL=${realized:+.2f}  Budget=${budget:.0f}  "
                    f"Trades={state.trade_count}/{cfg.max_trades_per_day}  {session_status(cfg)}\n"
                )
                last_log_ts = now

            # ── TELEGRAM COMMAND: /restart ───────────────────────────────────────
            if cmd_listener and cmd_listener.restart_requested:
                print(f"[{cfg.symbol}] /restart received — exiting with code 42 for watchdog relaunch")
                close_all_by_magic(cfg)
                sys.exit(42)   # watchdog treats 42 as "restart" not crash

            # ── CLOSE-CONFIRM: execute pending signal on next bar open ────────
            # If a signal was detected on a previous M5 bar and we're now on
            # a new bar, execute it immediately at the current market price.
            if state.pending_signal is not None:
                current_slot = _current_bar_slot()
                if current_slot != state.pending_bar_slot:
                    # New bar has opened — execute now
                    sig_to_exec = state.pending_signal
                    log_id_exec = state.pending_log_id
                    state.pending_signal   = None
                    state.pending_bar_slot = ""
                    state.pending_log_id   = ""
                    print(
                        f"[{cfg.symbol}] CLOSE-CONFIRM EXECUTE | "
                        f"{sig_to_exec.direction} | bar={current_slot} | "
                        f"entry≈{sig_to_exec.entry_price:.3f}"
                    )
                    _handle_signal_execute(sig_to_exec, state, log_id_exec)
                # Whether we executed or are still waiting, skip new signal detection
                time.sleep(cfg.poll_seconds)
                continue

            # ── SIGNAL DETECTION + TREND FILTER ──────────────────────────────
            # Step 1: Check if price has reached an entry level
            sig = evaluate_signal(mid, state.levels, state.already_traded, cfg)
            if sig:
                # Step 2: Apply trend filter — only trade in direction of day bias
                if not _trend_filter_ok(sig.direction, mid, state.start_price):
                    log.debug(
                        f"[{cfg.symbol}] TREND FILTER BLOCKED | {sig.direction} | "
                        f"mid={mid:.3f} start={state.start_price:.3f}"
                    )
                    time.sleep(cfg.poll_seconds)
                    continue

                # Step 2b: RE-ENTRY GUARD ──────────────────────────────────────
                # Block signals where price has already made a full excursion
                # through the entry level today and is now recovering back through it.
                # Example: price crashed through short entry (4648) to 4604, now
                # bouncing UP back through 4648. That is a recovery, not a fresh
                # short breakout. Shorting a recovery is a reversal trap.
                #
                # Rule: SHORT blocked if day_low is already below short_entry
                #        LONG  blocked if day_high is already above long_entry
                # (day_high/day_low are seeded by catchup and updated live every tick)
                if sig.direction == "SHORT" and state.day_low > 0 and state.day_low < state.levels.short_entry:
                    print(
                        f"[{cfg.symbol}] RE-ENTRY BLOCKED | SHORT | "
                        f"day_low={state.day_low:.2f} already below entry={state.levels.short_entry:.2f} — "
                        f"recovery re-entry, not a fresh breakout"
                    )
                    state.already_traded.add("SHORT")   # consume so this check is instant next poll
                    time.sleep(cfg.poll_seconds)
                    continue

                if sig.direction == "LONG" and state.day_high > 0 and state.day_high > state.levels.long_entry:
                    print(
                        f"[{cfg.symbol}] RE-ENTRY BLOCKED | LONG | "
                        f"day_high={state.day_high:.2f} already above entry={state.levels.long_entry:.2f} — "
                        f"pullback re-entry, not a fresh breakout"
                    )
                    state.already_traded.add("LONG")    # consume so this check is instant next poll
                    time.sleep(cfg.poll_seconds)
                    continue

                # Step 3: Pre-flight risk check before parking the pending signal
                snap = _snapshot(state.trade_count, state.internal_pnl)
                allowed, reason = can_place_trade(snap, cfg)
                if not allowed:
                    print(f"[{cfg.symbol}] BLOCKED (pre-confirm) | {reason}")
                    time.sleep(cfg.poll_seconds)
                    continue

                # Step 4: Park signal — wait for bar close (close-confirm)
                current_slot = _current_bar_slot()
                state.pending_signal   = sig
                state.pending_bar_slot = current_slot
                state.pending_log_id   = ""   # log_id assigned at execution time
                print(
                    f"[{cfg.symbol}] SIGNAL DETECTED | {sig.direction} | "
                    f"bar={current_slot} | entry={sig.entry_price:.3f} | "
                    f"waiting for bar close (close-confirm)..."
                )

            time.sleep(cfg.poll_seconds)

        except KeyboardInterrupt:
            print(f"\n[{cfg.symbol}] Stopped manually.")
            close_all_by_magic(cfg)
            break
        except Exception as e:
            print(f"[{cfg.symbol}] Error: {repr(e)}")
            state.order_in_flight = False
            time.sleep(2.0)


if __name__ == "__main__":
    run()