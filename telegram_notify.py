from __future__ import annotations

# =============================================================================
# TELEGRAM NOTIFICATIONS — threshold strategy trade alerts
#
# SETUP:
#   1. Open Telegram → search @userinfobot → send /start → note your chat_id
#   2. Paste your chat_id in CHAT_ID below
#   3. Run: python telegram_notify.py   ← sends a test message
#
# USAGE in runner.py:
#   from telegram_notify import notify_trade_placed, notify_tp, notify_sl, notify_day_end
# =============================================================================

import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN   = "7341988489:AAG5m0fqUs8mu1ZyMrXWBFVFVmzme09Ns8k"
CHAT_ID = "1353536439"   # ← paste your Telegram chat_id here (get it from @userinfobot)
# ─────────────────────────────────────────────────────────────────────────────


def _now_ist() -> str:
    """Current time as IST string."""
    from datetime import timedelta
    utc = datetime.now(timezone.utc)
    ist = utc + timedelta(hours=5, minutes=30)
    return ist.strftime("%d %b %Y  %H:%M IST")


def send(message: str, chat_id: str = CHAT_ID) -> bool:
    """
    Send a Telegram message. Returns True on success.
    Uses no external libraries — only stdlib urllib.
    """
    if not chat_id:
        print("[Telegram] ⚠️  CHAT_ID not set — message not sent")
        return False

    url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "HTML",
    }).encode()

    try:
        req  = urllib.request.Request(url, data=data, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read())
        if body.get("ok"):
            return True
        print(f"[Telegram] ❌ API error: {body}")
        return False
    except Exception as e:
        print(f"[Telegram] ❌ Send failed: {e}")
        return False


# =============================================================================
# NOTIFICATION TEMPLATES
# =============================================================================

def notify_trade_placed(
    symbol:    str,
    direction: str,
    entry:     float,
    sl:        float,
    tp:        float,
    lot:       float,
    sl_usd:    float,
    tp_usd:    float,
) -> bool:
    arrow = "🟢 LONG  📈" if direction == "LONG" else "🔴 SHORT 📉"
    msg = (
        f"<b>🎯 TRADE PLACED — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{arrow}\n"
        f"📥 Entry  : <b>{entry:.3f}</b>\n"
        f"🛡 SL     : {sl:.3f}  (−${sl_usd:.0f})\n"
        f"🎯 TP     : {tp:.3f}  (+${tp_usd:.0f})\n"
        f"📦 Lot    : {lot}\n"
        f"⚖️ R:R    : 1:3\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


def notify_tp(
    symbol:  str,
    direction: str,
    entry:   float,
    tp:      float,
    profit:  float,
) -> bool:
    msg = (
        f"<b>✅ TP HIT — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Direction : {direction}\n"
        f"Entry     : {entry:.3f}\n"
        f"TP hit    : <b>{tp:.3f}</b>\n"
        f"Profit    : <b>+${profit:.2f}</b> 🎉\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


def notify_sl(
    symbol:    str,
    direction: str,
    entry:     float,
    sl:        float,
    loss:      float,
) -> bool:
    msg = (
        f"<b>❌ SL HIT — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Direction : {direction}\n"
        f"Entry     : {entry:.3f}\n"
        f"SL hit    : {sl:.3f}\n"
        f"Loss      : −${abs(loss):.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


def notify_force_close(
    symbol: str,
    entry:  float,
    close:  float,
    pnl:    float,
) -> bool:
    emoji = "🟡"
    msg = (
        f"<b>{emoji} FORCE CLOSE (EOD) — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry     : {entry:.3f}\n"
        f"Closed at : {close:.3f}\n"
        f"P&L       : ${pnl:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


def notify_day_start(
    symbol:      str,
    start_price: float,
    long_entry:  float,
    long_tp:     float,
    long_sl:     float,
    short_entry: float,
    short_tp:    float,
    short_sl:    float,
    lot:         float,
) -> bool:
    msg = (
        f"<b>🌅 NEW DAY — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Start price : <b>{start_price:.3f}</b>\n"
        f"\n"
        f"🟢 LONG\n"
        f"  Entry : {long_entry:.3f}\n"
        f"  TP    : {long_tp:.3f}  (+${lot*15*100:.0f})\n"
        f"  SL    : {long_sl:.3f}  (−${lot*5*100:.0f})\n"
        f"\n"
        f"🔴 SHORT\n"
        f"  Entry : {short_entry:.3f}\n"
        f"  TP    : {short_tp:.3f}  (+${lot*15*100:.0f})\n"
        f"  SL    : {short_sl:.3f}  (−${lot*5*100:.0f})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


def notify_day_end(
    symbol:     str,
    date:       str,
    trades:     int,
    day_pnl:    float,
) -> bool:
    emoji = "🟢" if day_pnl >= 0 else "🔴"
    msg = (
        f"<b>{emoji} DAY END — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Date      : {date}\n"
        f"Trades    : {trades}\n"
        f"Day P&L   : <b>${day_pnl:+.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


def notify_loss_limit(symbol: str, day_pnl: float) -> bool:
    msg = (
        f"<b>⛔ LOSS LIMIT HIT — {symbol}</b>\n"
        f"Day P&L : ${day_pnl:+.2f}\n"
        f"Trading stopped for today.\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


def notify_profit_target(symbol: str, day_pnl: float) -> bool:
    msg = (
        f"<b>🎯 PROFIT TARGET HIT — {symbol}</b>\n"
        f"Day P&L : <b>${day_pnl:+.2f}</b> 🎉\n"
        f"Trading stopped for today.\n"
        f"🕐 {_now_ist()}"
    )
    return send(msg)


# =============================================================================
# TEST / SETUP
# =============================================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  TELEGRAM NOTIFY — SETUP")
    print("=" * 50)

    if not CHAT_ID:
        print()
        print("  ⚠️  CHAT_ID is not set.")
        print()
        print("  To get your chat_id:")
        print("  1. Open Telegram")
        print("  2. Search for @userinfobot")
        print("  3. Send /start")
        print("  4. Copy the 'Id' number")
        print("  5. Paste it as CHAT_ID in this file")
        print()
    else:
        print(f"\n  Sending test message to chat_id: {CHAT_ID}")
        ok = send(
            f"<b>✅ ASTRA XAU Bot — Connected</b>\n"
            f"Telegram notifications are working.\n"
            f"🕐 {_now_ist()}"
        )
        if ok:
            print("  ✅ Test message sent successfully!")
        else:
            print("  ❌ Failed — check TOKEN and CHAT_ID")

        print()
        print("  Testing all notification types...")
        notify_day_start("XAUUSD", 4682.735, 4707.73, 4722.73, 4702.73,
                         4657.73, 4642.73, 4662.73, 0.4)
        notify_trade_placed("XAUUSD", "LONG", 4707.73, 4702.73, 4722.73, 0.4, 200, 600)
        notify_tp("XAUUSD", "LONG", 4707.73, 4722.73, 586.0)
        notify_sl("XAUUSD", "LONG", 4707.73, 4702.73, -214.0)
        notify_day_end("XAUUSD", "2026-04-02", 1, 586.0)
        print("  ✅ All test notifications sent.")