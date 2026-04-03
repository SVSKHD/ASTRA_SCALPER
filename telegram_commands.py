from __future__ import annotations

# =============================================================================
# TELEGRAM COMMAND LISTENER
#
# Runs as a background daemon thread in runner.py.
# Polls Telegram every 3s for incoming commands from CHAT_ID only.
#
# COMMANDS:
#   /status  → sends current P&L, trade count, open position, levels
#   /restart → gracefully signals the main loop to restart runner.py
#
# SECURITY: only responds to messages from CHAT_ID (ignores all others).
#
# USAGE in runner.py:
#   from telegram_commands import CommandListener
#   cmd = CommandListener(state, cfg)
#   cmd.start()   # starts daemon thread
#   # In main loop:
#   if cmd.restart_requested:
#       break  # watchdog will restart runner.py
# =============================================================================

import json
import threading
import time
import urllib.request
import urllib.parse
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("telegram_cmd")

_IST = timedelta(hours=5, minutes=30)


def _now_ist() -> str:
    utc = datetime.now(timezone.utc)
    ist = utc + timedelta(hours=5, minutes=30)
    return ist.strftime("%d %b %Y  %H:%M IST")


class CommandListener:
    """
    Background thread that polls Telegram getUpdates and handles commands.
    Safe to start before MT5 is initialized — it only uses urllib.
    """

    def __init__(self, state, cfg, get_pnl_fn=None, get_open_fn=None):
        """
        state       : DayState object from runner.py
        cfg         : StrategyConfig
        get_pnl_fn  : callable() → float  (day realized P&L)
        get_open_fn : callable() → float  (open unrealized P&L)
        """
        from telegram_notify import TOKEN, CHAT_ID, send
        self._token       = TOKEN
        self._chat_id     = CHAT_ID
        self._send        = send
        self._state       = state
        self._cfg         = cfg
        self._get_pnl     = get_pnl_fn
        self._get_open    = get_open_fn
        self._offset      = 0
        self._poll_sec    = 3.0
        self._thread: threading.Thread | None = None
        self.restart_requested: bool = False

    def start(self):
        if not self._chat_id:
            log.warning("[TgCmd] CHAT_ID not set — command listener disabled")
            return
        # ── Drain all pending messages on startup ─────────────────────────
        # Without this, a /restart message stays in the queue and triggers
        # another restart on every relaunch → infinite restart loop.
        try:
            updates = self._get_updates()
            if updates:
                self._offset = updates[-1]["update_id"] + 1
                print(f"[TgCmd] Drained {len(updates)} pending message(s) on startup")
        except Exception as e:
            log.debug(f"[TgCmd] Startup drain failed (ok): {e}")
        # ─────────────────────────────────────────────────────────────────
        self._thread = threading.Thread(
            target=self._loop,
            name="tg-cmd-listener",
            daemon=True,
        )
        self._thread.start()
        log.info("[TgCmd] Command listener started (polling every 3s)")
        print(f"[TgCmd] Listening for /status and /restart commands")

    def _loop(self):
        while True:
            try:
                updates = self._get_updates()
                for u in updates:
                    self._offset = u["update_id"] + 1
                    self._handle(u)
            except Exception as e:
                log.debug(f"[TgCmd] Poll error: {e}")
            time.sleep(self._poll_sec)

    def _get_updates(self) -> list:
        url = (
            f"https://api.telegram.org/bot{self._token}/getUpdates"
            f"?offset={self._offset}&timeout=2&limit=10"
        )
        req  = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=8)
        body = json.loads(resp.read())
        if not body.get("ok"):
            return []
        return body.get("result", [])

    def _handle(self, update: dict):
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        # Security: only respond to configured CHAT_ID
        from_id = str(msg.get("chat", {}).get("id", ""))
        if from_id != str(self._chat_id):
            log.warning(f"[TgCmd] Ignored message from unknown chat_id={from_id}")
            return

        text = (msg.get("text") or "").strip().lower()

        if text == "/status":
            self._cmd_status()
        elif text == "/restart":
            self._cmd_restart()
        elif text == "/help":
            self._cmd_help()
        else:
            # Unknown command — send help
            self._cmd_help()

    def _cmd_status(self):
        """Send current bot status, P&L, levels, current price, open trade."""
        state = self._state
        cfg   = self._cfg

        # ── P&L ──────────────────────────────────────────────────────────
        realized = 0.0
        open_pnl = 0.0
        try:
            if self._get_pnl:
                realized = self._get_pnl()
            if self._get_open:
                open_pnl = self._get_open()
        except Exception:
            pass
        total = realized + open_pnl

        # ── Current price ─────────────────────────────────────────────────
        try:
            import MetaTrader5 as mt5
            tick = mt5.symbol_info_tick(cfg.symbol)
            if tick:
                bid = float(tick.bid)
                ask = float(tick.ask)
                mid = (bid + ask) / 2.0
                price_txt = (
                    f"\n<b>Price</b>\n"
                    f"  Bid : {bid:.3f}\n"
                    f"  Ask : {ask:.3f}\n"
                    f"  Mid : {mid:.3f}"
                )
            else:
                price_txt = "\nPrice : unavailable"
        except Exception:
            price_txt = "\nPrice : unavailable"

        # ── Open trade ────────────────────────────────────────────────────
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get(symbol=cfg.symbol)
            if positions:
                p = positions[0]
                direction = "LONG" if p.type == 0 else "SHORT"
                entry     = p.price_open
                current   = p.price_current
                volume    = p.volume
                trade_pnl = p.profit
                pips_moved = (
                    (current - entry) if direction == "LONG"
                    else (entry - current)
                )
                trade_txt = (
                    f"\n<b>Open trade</b>\n"
                    f"  Direction : {direction}\n"
                    f"  Entry     : {entry:.3f}\n"
                    f"  Current   : {current:.3f}\n"
                    f"  Pips      : {pips_moved:+.1f}\n"
                    f"  Volume    : {volume}\n"
                    f"  Float P&L : <b>${trade_pnl:+.2f}</b>"
                )
            else:
                trade_txt = "\nOpen trade : none"
        except Exception:
            trade_txt = "\nOpen trade : unavailable"

        # ── Levels ────────────────────────────────────────────────────────
        lv = state.levels
        if lv:
            levels_txt = (
                f"\n<b>Levels</b>\n"
                f"  Start  : {lv.start:.3f}\n"
                f"  L Entry: {lv.long_entry:.3f} → TP {lv.long_tp:.3f}\n"
                f"  S Entry: {lv.short_entry:.3f} → TP {lv.short_tp:.3f}"
            )
        else:
            levels_txt = "\nLevels : waiting for start price"

        pnl_emoji = "🟢" if total >= 0 else "🔴"

        msg = (
            f"<b>📊 STATUS — {cfg.symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Date      : {state.date or 'not set'}\n"
            f"Trades    : {state.trade_count}/{cfg.max_trades_per_day}\n"
            f"Realized  : ${realized:+.2f}\n"
            f"Float     : ${open_pnl:+.2f}\n"
            f"Total     : {pnl_emoji} <b>${total:+.2f}</b>\n"
            f"Loss limit: -${cfg.max_daily_loss_usd:.0f} | TP: +${cfg.daily_profit_target_usd:.0f}"
            f"{price_txt}"
            f"{trade_txt}"
            f"{levels_txt}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now_ist()}"
        )
        self._send(msg)
        log.info("[TgCmd] /status sent")

    def _cmd_restart(self):
        """Signal main loop to exit — watchdog will restart."""
        self._send(
            f"<b>🔄 RESTART — {self._cfg.symbol}</b>\n"
            f"Restarting bot in ~5 seconds...\n"
            f"Watchdog will relaunch runner.py automatically.\n"
            f"🕐 {_now_ist()}"
        )
        log.info("[TgCmd] /restart received — setting restart_requested=True")
        self.restart_requested = True

    def _cmd_help(self):
        msg = (
            f"<b>🤖 ASTRA XAU Bot Commands</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"/status  — current P&amp;L, trades, levels\n"
            f"/restart — restart the bot (watchdog relaunches)\n"
            f"/help    — show this message"
        )
        self._send(msg)