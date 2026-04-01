from __future__ import annotations

import time
import threading
from datetime import datetime, timezone

import MetaTrader5 as mt5

from settings import PriceSettings
from clock import tick_time_to_clock, to_server_time, to_local_time, iso_z
from storage import (
    resolve_day_path,
    resolve_start_root_path,
    read_json,
    default_payload,
    build_start_root_payload,
    atomic_write_json,
    resolve_start_emergency_path,
    append_line,
)
from .config import list_enabled_symbols
# from Revamp.notify import (
#     send_runner_card,
#     embed_field,
# )

MIDNIGHT_GRACE_MINUTES = 10
STALE_AFTER_SECONDS = 20  # tune: 10–60


def ensure_mt5(max_retries: int = 10, sleep_s: float = 1.0):
    for _ in range(max_retries):
        if mt5.initialize():
            return
        time.sleep(sleep_s)
    raise RuntimeError(f"MT5 initialize failed after retries: {mt5.last_error()}")


def ensure_symbol_ready(symbol: str) -> bool:
    sinfo = mt5.symbol_info(symbol)
    if sinfo is None:
        return False
    if not getattr(sinfo, "visible", True):
        mt5.symbol_select(symbol, True)
        sinfo = mt5.symbol_info(symbol)
    return sinfo is not None and getattr(sinfo, "visible", True)


def get_tick(symbol: str):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or tick.time == 0:
        return None
    bid = float(tick.bid)
    ask = float(tick.ask)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(tick.last) if tick.last else 0.0
    if mid <= 0:
        return None
    return tick, bid, ask, mid


def lock_window_ok(cfg: PriceSettings, tick_hhmm: str) -> bool:
    return tick_hhmm >= cfg.lock_hhmm_mt5


def _within_midnight_grace(clk) -> bool:
    if clk.tick_time_utc.hour != 0:
        return False
    return clk.tick_time_utc.minute < MIDNIGHT_GRACE_MINUTES


def _print_no_tick_diagnostics(symbol: str):
    term = mt5.terminal_info()
    acc = mt5.account_info()
    sinfo = mt5.symbol_info(symbol)
    last_err = mt5.last_error()

    print(f"[{symbol}] ⏳ no tick / no quotes yet... last_error={last_err}")

    if term is not None:
        connected = getattr(term, "connected", None)
        trade_allowed = getattr(term, "trade_allowed", None)
        print(f"[{symbol}] terminal: connected={connected} trade_allowed={trade_allowed}")

    if acc is not None:
        print(f"[{symbol}] account: login=****** server={getattr(acc, 'server', None)}")

    if sinfo is None:
        print(f"[{symbol}] symbol_info: None (symbol name may be wrong / broker suffix?)")
    else:
        visible = getattr(sinfo, "visible", None)
        trade_mode = getattr(sinfo, "trade_mode", None)
        print(f"[{symbol}] symbol_info: visible={visible} trade_mode={trade_mode}")
        if visible is False:
            ok = mt5.symbol_select(symbol, True)
            print(f"[{symbol}] symbol_select({symbol}, True) => {ok}")


def _safe_write_json(path: str, payload: dict, pretty: bool, em_path: str, em_line: str, warn_tag: str) -> bool:
    try:
        ok = atomic_write_json(path, payload, pretty=pretty)
        if ok is None:
            return True
        return bool(ok)
    except Exception as e:
        append_line(em_path, f"{em_line} | WRITE_FAIL {warn_tag} | err={repr(e)} | path={path}")
        return False


def _reset_start_block() -> dict:
    return {
        "status": "PENDING",
        "price": None,
        "source": None,
        "locked_tick_time_utc": None,
        "locked_server_time": None,
        "locked_local_time": None,
    }


def _reset_extremes_block() -> dict:
    return {
        "high": None,
        "high_tick_time_utc": None,
        "high_server_time": None,
        "high_local_time": None,
        "low": None,
        "low_tick_time_utc": None,
        "low_server_time": None,
        "low_local_time": None,
        "backfilled": False,
    }


def _reset_extreme_events_block() -> list[dict]:
    return []


def _event_exists(events: list[dict], kind: str, price: float, tick_time_utc: str) -> bool:
    for ev in events:
        if (
            ev.get("kind") == kind
            and float(ev.get("price", 0.0)) == float(price)
            and ev.get("tick_time_utc") == tick_time_utc
        ):
            return True
    return False


def _append_extreme_event(payload: dict, kind: str, price: float, clk, server_dt, local_dt):
    payload.setdefault("extreme_events", _reset_extreme_events_block())
    tick_iso = iso_z(clk.tick_time_utc)

    if _event_exists(payload["extreme_events"], kind, price, tick_iso):
        return

    payload["extreme_events"].append({
        "kind": kind,
        "price": float(price),
        "tick_time_utc": tick_iso,
        "server_time": server_dt.isoformat(),
        "local_time": local_dt.isoformat(),
    })


def _backfill_extremes_from_lock(symbol: str, payload: dict, cfg: PriceSettings, em_path: str, date_mt5: str) -> dict:
    """
    Rebuild true high/low since the locked start time using M1 bars.
    This allows earlier extremes (before runner restart / feature deployment)
    to be recovered and stored.
    """
    start = payload.get("start") or {}
    extremes = payload.get("extremes") or _reset_extremes_block()

    if start.get("status") != "LOCKED":
        return extremes

    locked_tick_utc = start.get("locked_tick_time_utc")
    if not locked_tick_utc:
        return extremes

    if extremes.get("backfilled") is True:
        return extremes

    try:
        from_dt = datetime.fromisoformat(str(locked_tick_utc).replace("Z", "+00:00"))
        to_dt = datetime.now(timezone.utc)

        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, from_dt, to_dt)
        if rates is None or len(rates) == 0:
            append_line(em_path, f"{date_mt5} | EXTREME_BACKFILL_EMPTY | from={locked_tick_utc}")
            return extremes

        high_bar = max(rates, key=lambda r: float(r["high"]))
        low_bar = min(rates, key=lambda r: float(r["low"]))

        high_price = float(high_bar["high"])
        low_price = float(low_bar["low"])

        high_dt_utc = datetime.fromtimestamp(int(high_bar["time"]), tz=timezone.utc)
        low_dt_utc = datetime.fromtimestamp(int(low_bar["time"]), tz=timezone.utc)

        high_server_dt = to_server_time(high_dt_utc, cfg.server_tz)
        high_local_dt = to_local_time(high_dt_utc, cfg.local_tz)

        low_server_dt = to_server_time(low_dt_utc, cfg.server_tz)
        low_local_dt = to_local_time(low_dt_utc, cfg.local_tz)

        extremes["high"] = high_price
        extremes["high_tick_time_utc"] = iso_z(high_dt_utc)
        extremes["high_server_time"] = high_server_dt.isoformat()
        extremes["high_local_time"] = low_local_dt.isoformat() if False else high_local_dt.isoformat()

        extremes["low"] = low_price
        extremes["low_tick_time_utc"] = iso_z(low_dt_utc)
        extremes["low_server_time"] = low_server_dt.isoformat()
        extremes["low_local_time"] = low_local_dt.isoformat()

        extremes["backfilled"] = True

        payload["extremes"] = extremes
        payload.setdefault("extreme_events", _reset_extreme_events_block())

        if not _event_exists(payload["extreme_events"], "HIGH", high_price, extremes["high_tick_time_utc"]):
            payload["extreme_events"].append({
                "kind": "HIGH",
                "price": high_price,
                "tick_time_utc": extremes["high_tick_time_utc"],
                "server_time": extremes["high_server_time"],
                "local_time": extremes["high_local_time"],
            })

        if not _event_exists(payload["extreme_events"], "LOW", low_price, extremes["low_tick_time_utc"]):
            payload["extreme_events"].append({
                "kind": "LOW",
                "price": low_price,
                "tick_time_utc": extremes["low_tick_time_utc"],
                "server_time": extremes["low_server_time"],
                "local_time": extremes["low_local_time"],
            })

        append_line(
            em_path,
            f"{date_mt5} | EXTREME_BACKFILL_OK | "
            f"high={high_price}@{extremes['high_tick_time_utc']} | "
            f"low={low_price}@{extremes['low_tick_time_utc']}"
        )

        return extremes

    except Exception as e:
        append_line(em_path, f"{date_mt5} | EXTREME_BACKFILL_FAIL | err={repr(e)}")
        return extremes


def send_start_runner_boot(symbols: list[str]) -> None:
    try:
        # send_runner_card(
        #     "update",
        #     title="🚀 Start Price Runner Initialised",
        #     symbol="MULTI",
        #     status="BOOT",
        #     fields=[
        #         embed_field("Symbols", ", ".join(symbols), False),
        #         embed_field(
        #             "UTC",
        #             datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        #             False,
        #         ),
        #     ],
        #     footer="pricing.start_price",
        # )
        print("✅ Startup notify sent")
    except Exception as e:
        print(f"⚠️ Startup notify failed: {e}")


def run_start_price_loop(symbol: str, cfg: PriceSettings):
    ensure_mt5()

    if not ensure_symbol_ready(symbol):
        print(f"[{symbol}] ❌ symbol not ready (wrong name or not available). Will keep retrying...")

    last_date_mt5: str | None = None
    last_status_print = 0.0

    last_tick_epoch: int | None = None
    last_tick_change_monotonic = time.time()

    last_locked_date: str | None = None
    last_start_error_notify_ts = 0.0
    start_error_notify_interval = 300.0  # 5 min

    em_path = resolve_start_emergency_path(cfg.base_dir, symbol)

    while True:
        ensure_symbol_ready(symbol)

        got = get_tick(symbol)
        if got is None:
            now = time.time()
            if now - last_status_print >= cfg.status_print_seconds:
                _print_no_tick_diagnostics(symbol)
                last_status_print = now
            time.sleep(cfg.poll_seconds)
            continue

        tick, bid, ask, mid = got
        clk = tick_time_to_clock(tick.time)

        server_dt = to_server_time(clk.tick_time_utc, cfg.server_tz)
        local_dt = to_local_time(clk.tick_time_utc, cfg.local_tz)

        date_mt5 = clk.date_mt5
        day_path = resolve_day_path(cfg.base_dir, symbol, date_mt5)

        payload = read_json(day_path)
        day_file_exists = payload is not None
        payload = payload or default_payload(symbol, date_mt5)

        payload.setdefault("start", _reset_start_block())
        payload.setdefault("extremes", _reset_extremes_block())
        payload.setdefault("extreme_events", _reset_extreme_events_block())

        payload["tz"]["server"] = str(cfg.server_tz)
        payload["tz"]["local"] = getattr(cfg.local_tz, "key", None) or str(cfg.local_tz)

        if last_date_mt5 is None:
            last_date_mt5 = date_mt5
        elif date_mt5 != last_date_mt5:
            old = last_date_mt5
            new = date_mt5

            print("\n" + "=" * 70)
            print(f"[{symbol}] 🔁 ROLLOVER DETECTED")
            print(f"[{symbol}] OLD MT5 DATE : {old}")
            print(f"[{symbol}] NEW MT5 DATE : {new}")
            print(f"[{symbol}] TICK TIME UTC: {iso_z(clk.tick_time_utc)}")
            print(f"[{symbol}] SERVER TIME  : {server_dt.isoformat()}")
            print(f"[{symbol}] LOCAL TIME   : {local_dt.isoformat()}")
            print("=" * 70 + "\n")

            payload["meta"]["rollover_detected"] = True
            payload["meta"]["last_rollover_from"] = old
            payload["start"] = _reset_start_block()
            payload["extremes"] = _reset_extremes_block()
            payload["extreme_events"] = _reset_extreme_events_block()

            last_tick_epoch = None
            last_tick_change_monotonic = time.time()

            append_line(
                em_path,
                f"{new} | ROLLOVER | from={old} -> to={new} | tick={iso_z(clk.tick_time_utc)}"
            )

            last_date_mt5 = new
            last_locked_date = None

        now_mono = time.time()
        if last_tick_epoch is None:
            last_tick_epoch = tick.time
            last_tick_change_monotonic = now_mono
        else:
            if tick.time != last_tick_epoch:
                last_tick_epoch = tick.time
                last_tick_change_monotonic = now_mono

        stale_for = now_mono - last_tick_change_monotonic
        is_stale = stale_for >= STALE_AFTER_SECONDS
        payload["meta"]["market_open"] = not is_stale

        nowu = datetime.now(timezone.utc)
        payload["timestamps"] = {
            "updated_utc": nowu.isoformat().replace("+00:00", "Z"),
            "tick_time_utc": iso_z(clk.tick_time_utc),
            "server_time": server_dt.isoformat(),
            "local_time": local_dt.isoformat(),
        }

        start = payload.get("start") or _reset_start_block()

        allow_lock_now = day_file_exists or _within_midnight_grace(clk) or cfg.allow_bootstrap_lock

        if start.get("status") != "LOCKED" and allow_lock_now and (not is_stale):
            if lock_window_ok(cfg, clk.time_mt5_hhmm):
                start["status"] = "LOCKED"
                start["price"] = mid

                src_prefix = "tick_lock_midnight_window" if _within_midnight_grace(clk) else "tick_lock_existing_dayfile"
                start["source"] = f"{src_prefix}_at_or_after_{cfg.lock_hhmm_mt5}"

                start["locked_tick_time_utc"] = iso_z(clk.tick_time_utc)
                start["locked_server_time"] = server_dt.isoformat()
                start["locked_local_time"] = local_dt.isoformat()

                append_line(
                    em_path,
                    f"{date_mt5} | START_LOCKED | price={mid} | mt5={start['locked_tick_time_utc']} | "
                    f"server={start['locked_server_time']} | local={start['locked_local_time']} | source={start['source']}"
                )

        payload["start"] = start

        # ------------------------------------------------------------
        # NEW: Backfill true extremes from locked start time
        # ------------------------------------------------------------
        extremes = payload.get("extremes") or _reset_extremes_block()
        if payload["start"]["status"] == "LOCKED":
            extremes = _backfill_extremes_from_lock(symbol, payload, cfg, em_path, date_mt5)

        # ------------------------------------------------------------
        # Existing live extreme tracking (kept intact)
        # ------------------------------------------------------------
        extremes = payload.get("extremes") or _reset_extremes_block()

        if payload["start"]["status"] == "LOCKED":
            if extremes.get("high") is None:
                extremes["high"] = mid
                extremes["high_tick_time_utc"] = iso_z(clk.tick_time_utc)
                extremes["high_server_time"] = server_dt.isoformat()
                extremes["high_local_time"] = local_dt.isoformat()
                _append_extreme_event(payload, "HIGH", mid, clk, server_dt, local_dt)

            if extremes.get("low") is None:
                extremes["low"] = mid
                extremes["low_tick_time_utc"] = iso_z(clk.tick_time_utc)
                extremes["low_server_time"] = server_dt.isoformat()
                extremes["low_local_time"] = local_dt.isoformat()
                _append_extreme_event(payload, "LOW", mid, clk, server_dt, local_dt)

            if extremes.get("high") is None or mid > float(extremes["high"]):
                extremes["high"] = mid
                extremes["high_tick_time_utc"] = iso_z(clk.tick_time_utc)
                extremes["high_server_time"] = server_dt.isoformat()
                extremes["high_local_time"] = local_dt.isoformat()
                _append_extreme_event(payload, "HIGH", mid, clk, server_dt, local_dt)

                append_line(
                    em_path,
                    f"{date_mt5} | NEW_HIGH | price={mid} | "
                    f"mt5={extremes['high_tick_time_utc']} | "
                    f"server={extremes['high_server_time']} | "
                    f"local={extremes['high_local_time']}"
                )

            if extremes.get("low") is None or mid < float(extremes["low"]):
                extremes["low"] = mid
                extremes["low_tick_time_utc"] = iso_z(clk.tick_time_utc)
                extremes["low_server_time"] = server_dt.isoformat()
                extremes["low_local_time"] = local_dt.isoformat()
                _append_extreme_event(payload, "LOW", mid, clk, server_dt, local_dt)

                append_line(
                    em_path,
                    f"{date_mt5} | NEW_LOW | price={mid} | "
                    f"mt5={extremes['low_tick_time_utc']} | "
                    f"server={extremes['low_server_time']} | "
                    f"local={extremes['low_local_time']}"
                )

        payload["extremes"] = extremes

        em_line = f"{date_mt5} | tick_mt5={iso_z(clk.tick_time_utc)} | mid={mid} | stale={int(stale_for)}s"

        do_write_day = (not is_stale) or (time.time() - last_status_print >= cfg.status_print_seconds)
        if do_write_day:
            _safe_write_json(day_path, payload, cfg.pretty_json, em_path, em_line, "DAY_FILE")

        root_path = None
        if payload["start"]["status"] == "LOCKED":
            root_path = resolve_start_root_path(cfg.base_dir, symbol)
            root_payload = build_start_root_payload(payload)
            _safe_write_json(root_path, root_payload, cfg.pretty_json, em_path, em_line, "ROOT_START")

            if last_locked_date != date_mt5:
                print(
                    f"[{symbol}] ✅ START LOCKED for {date_mt5} | price={payload['start']['price']} | "
                    f"MT5={payload['start']['locked_tick_time_utc']} | "
                    f"SERVER={payload['start']['locked_server_time']} | "
                    f"LOCAL={payload['start']['locked_local_time']} | "
                    f"SOURCE={payload['start']['source']}"
                )

                try:
                    # send_runner_card(
                    #     "update",
                    #     title="🟢 Start Price Locked",
                    #     symbol=symbol,
                    #     status="LOCKED",
                    #     fields=[
                    #         embed_field("MT5 Date", date_mt5),
                    #         embed_field("Start Price", f"{float(payload['start']['price']):.5f}"),
                    #         embed_field("Tick UTC", payload["start"]["locked_tick_time_utc"], False),
                    #         embed_field("Server Time", payload["start"]["locked_server_time"], False),
                    #         embed_field("Local Time", payload["start"]["locked_local_time"], False),
                    #         embed_field("Source", payload["start"]["source"], False),
                    #     ],
                    #     footer="pricing.start_price",
                    # )
                    print("hello")
                except Exception:
                    pass

                last_locked_date = date_mt5

        now = time.time()
        if now - last_status_print >= cfg.status_print_seconds:
            sp = payload["start"]["price"]
            st = payload["start"]["status"]
            tick_iso = payload["timestamps"]["tick_time_utc"]
            state = "LIVE" if not is_stale else f"STALE({int(stale_for)}s)"

            eh = payload.get("extremes", {}).get("high")
            el = payload.get("extremes", {}).get("low")
            ev_count = len(payload.get("extreme_events", []))

            print(
                f"[{symbol}] {state} | MT5={tick_iso} | SERVER={server_dt.isoformat()} | LOCAL={local_dt.isoformat()} | "
                f"START={st}({sp}) | HIGH={eh} | LOW={el} | EXTREME_EVENTS={ev_count} | "
                f"MID={mid:.5f} bid={bid:.5f} ask={ask:.5f} | DAY_FILE={day_path}"
                + (f" | ROOT_START={root_path}" if root_path else "")
            )
            last_status_print = now

        if payload["start"]["status"] != "LOCKED":
            now_err = time.time()
            if now_err - last_start_error_notify_ts >= start_error_notify_interval:
                reason = []
                if is_stale:
                    reason.append(f"stale_tick={int(stale_for)}s")
                if not allow_lock_now:
                    reason.append("lock_window_not_allowed")
                if not lock_window_ok(cfg, clk.time_mt5_hhmm):
                    reason.append(f"before_lock_hhmm={cfg.lock_hhmm_mt5}")
                if payload["start"]["price"] is None:
                    reason.append("start_price_none")

                reason_text = ", ".join(reason) if reason else "pending_lock"

                try:
                    # send_runner_card(
                    #     "critical",
                    #     title="⚠️ Start Not Locked",
                    #     symbol=symbol,
                    #     status=str(payload["start"]["status"]),
                    #     fields=[
                    #         embed_field("MT5 Date", date_mt5),
                    #         embed_field("Tick UTC", iso_z(clk.tick_time_utc), False),
                    #         embed_field("Server Time", server_dt.isoformat(), False),
                    #         embed_field("Local Time", local_dt.isoformat(), False),
                    #         embed_field("Mid", f"{mid:.5f}"),
                    #         embed_field("Reason", reason_text, False),
                    #     ],
                    #     footer="pricing.start_price",
                    # )
                    print("hello")
                except Exception:
                    pass

                last_start_error_notify_ts = now_err

        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    cfg = PriceSettings()
    symbols = list_enabled_symbols()

    send_start_runner_boot(symbols)

    print("=== START PRICE RUNNER STARTING ===", symbols)
    for s in symbols:
        t = threading.Thread(target=run_start_price_loop, args=(s, cfg), daemon=True)
        t.start()

    while True:
        time.sleep(2)