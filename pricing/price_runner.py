# price_runner.py
from __future__ import annotations

import os
import time
import threading
from datetime import datetime, timezone

import MetaTrader5 as mt5

from settings import PriceSettings
from price_assembly import build_price_packet
from storage import resolve_price_assembly_root_path, atomic_write_json, append_jsonl
from Revamp.config import get_tradeable_symbols

def ensure_mt5():
    if mt5.initialize():
        return
    raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")


def _symbol_thread(symbol: str, cfg: PriceSettings):
    # Init MT5 (retry)
    try:
        ensure_mt5()
    except Exception:
        print(f"[{symbol}] ❌ MT5 init failed: {mt5.last_error()} | retrying...")
        while True:
            time.sleep(2)
            try:
                ensure_mt5()
                break
            except Exception:
                continue

    try:
        mt5.symbol_select(symbol, True)
    except Exception:
        pass

    last_print = 0.0
    last_write_ok = True

    # ---- per-day high/low (MT5 date) ----
    active_day = None
    hi = hi_mt5 = hi_srv = None
    lo = lo_mt5 = lo_srv = None

    # ---- stale state ----
    last_tick_epoch = None
    last_tick_change = None  # ✅ important (don’t pre-seed with time.time())
    stale_after = getattr(cfg, "stale_after_seconds", 20)

    while True:
        try:
            pkt = build_price_packet(symbol, cfg)

            # -----------------------
            # NO TICK => heartbeat
            # -----------------------
            if pkt is None:
                now = time.time()
                if now - last_print >= cfg.status_print_seconds:
                    _no_tick_diagnostics(symbol)
                    last_print = now

                live_out = resolve_price_assembly_root_path(cfg.base_dir, symbol)
                hb = {
                    "symbol": symbol,
                    "start": None,
                    "current": None,
                    "high": None,
                    "low": None,
                    "meta": {
                        "note": "NO_TICK",
                        "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "is_stale": True,
                        "stale_seconds": 999999,
                    },
                }

                try:
                    atomic_write_json(live_out, hb, pretty=cfg.pretty_json)
                    last_write_ok = True
                except Exception:
                    last_write_ok = False

                time.sleep(cfg.poll_seconds)
                continue

            meta = pkt.get("meta") or {}
            date_mt5 = meta.get("date_mt5") or "UNKNOWN_DATE"
            hhmm_mt5 = meta.get("hhmm_mt5") or "??:??"
            mt5_ui_human = meta.get("mt5_ui_human") or f"{date_mt5} {hhmm_mt5}"

            live_out = resolve_price_assembly_root_path(cfg.base_dir, symbol)
            daily_out = os.path.join(cfg.base_dir, "price_assembly", f"{symbol}_{date_mt5}.json")
            daily_jsonl = os.path.join(cfg.base_dir, "price_assembly", f"{symbol}_{date_mt5}.jsonl")

            # -----------------------
            # STALE DETECTION
            # -----------------------
            tick_epoch = (pkt.get("current") or {}).get("tick_time_epoch")
            now_mono = time.time()

            if tick_epoch is None:
                # no tick time inside packet -> use last_tick_change if set
                stale_seconds = int(now_mono - last_tick_change) if last_tick_change else 999999
            else:
                if last_tick_epoch is None:
                    last_tick_epoch = tick_epoch
                    last_tick_change = now_mono
                    stale_seconds = 0
                elif tick_epoch != last_tick_epoch:
                    last_tick_epoch = tick_epoch
                    last_tick_change = now_mono
                    stale_seconds = 0
                else:
                    stale_seconds = int(now_mono - (last_tick_change or now_mono))

            is_stale = stale_seconds >= stale_after

            pkt.setdefault("meta", {})
            pkt["meta"]["stale_seconds"] = stale_seconds
            pkt["meta"]["is_stale"] = is_stale
            # ✅ do NOT overwrite pkt["meta"]["updated_utc"] (assembly owns that)

            # -----------------------
            # HIGH / LOW PER MT5 DAY
            # -----------------------
            cur = pkt.get("current") or {}
            cur_mid = cur.get("mid")
            cur_mt5 = cur.get("mt5_ui_utc")
            cur_srv = cur.get("server_time")
            cur_date = (pkt.get("meta") or {}).get("date_mt5")

            if cur_date and cur_date != active_day:
                active_day = cur_date
                hi = hi_mt5 = hi_srv = None
                lo = lo_mt5 = lo_srv = None

            if active_day and cur_mid is not None:
                if hi is None or cur_mid > hi:
                    hi, hi_mt5, hi_srv = cur_mid, cur_mt5, cur_srv
                if lo is None or cur_mid < lo:
                    lo, lo_mt5, lo_srv = cur_mid, cur_mt5, cur_srv

            pkt["high"] = None if hi is None else {
                "since_day_start": hi,
                "mt5_ui_utc": hi_mt5,
                "server_time": hi_srv,
                "date_mt5": active_day,
            }
            pkt["low"] = None if lo is None else {
                "since_day_start": lo,
                "mt5_ui_utc": lo_mt5,
                "server_time": lo_srv,
                "date_mt5": active_day,
            }

            # -----------------------
            # WRITE
            # -----------------------
            try:
                atomic_write_json(live_out, pkt, pretty=cfg.pretty_json)
                atomic_write_json(daily_out, pkt, pretty=cfg.pretty_json)
                append_jsonl(daily_jsonl, pkt)
                last_write_ok = True
            except Exception:
                last_write_ok = False

            # -----------------------
            # STATUS PRINT (robust START validity)
            # -----------------------
            now = time.time()
            if now - last_print >= cfg.status_print_seconds:
                start = pkt.get("start") or {}
                start_date = start.get("start_date_mt5") or start.get("date_mt5")
                start_ok = (
                    start.get("status") == "LOCKED"
                    and start.get("price") is not None
                    and start_date == date_mt5
                )

                st = start.get("status") if start_ok else "NONE"
                sp = start.get("price") if start_ok else None

                print(
                    f"[{symbol}] ✅ packet | MT5_UI={mt5_ui_human} | "
                    f"START={st}({sp}) | MID={cur_mid} | "
                    f"H={hi} L={lo} | stale={stale_seconds}s | wrote={last_write_ok} | "
                    f"LIVE={live_out} | DAILY={daily_out} | JSONL={daily_jsonl}"
                )
                last_print = now

        except Exception as e:
            print(f"[{symbol}] ⚠️ thread exception: {e!r}")
            time.sleep(1)

        time.sleep(cfg.poll_seconds)


def _no_tick_diagnostics(symbol: str):
    try:
        term = mt5.terminal_info()
        acc = mt5.account_info()
        sinfo = mt5.symbol_info(symbol)
        last_err = mt5.last_error()

        sel_ok = None
        try:
            sel_ok = mt5.symbol_select(symbol, True)
        except Exception:
            sel_ok = None

        msg = f"[{symbol}] ⏳ no tick | last_error={last_err} | symbol_select={sel_ok}"
        if term is not None:
            msg += f" | connected={getattr(term,'connected',None)} trade_allowed={getattr(term,'trade_allowed',None)}"
        if acc is not None:
            msg += f" | login={getattr(acc,'login',None)} server={getattr(acc,'server',None)}"
        if sinfo is None:
            msg += " | symbol_info=None"
        else:
            msg += f" | visible={getattr(sinfo,'visible',None)} trade_mode={getattr(sinfo,'trade_mode',None)}"

        print(msg)
    except Exception:
        print(f"[{symbol}] ⏳ no tick (diagnostics failed)")


def run_price_runner(cfg: PriceSettings, enabled_symbols: list[str]):
    ensure_mt5()
    threads = []
    for sym in enabled_symbols:
        t = threading.Thread(target=_symbol_thread, args=(sym, cfg), daemon=True)
        t.start()
        threads.append(t)

    print(f"=== PRICE RUNNER STARTED === symbols={enabled_symbols}")
    while True:
        time.sleep(5)


if __name__ == "__main__":
    cfg = PriceSettings()
    enabled_symbols = get_tradeable_symbols()

    print("=== PRICE RUNNER STARTING ===")
    print(f"enabled_symbols={enabled_symbols}")
    run_price_runner(cfg, enabled_symbols)