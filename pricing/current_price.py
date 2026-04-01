# current_price.py
from __future__ import annotations

import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

from .settings import PriceSettings
from .clock import tick_time_to_clock, to_server_time, to_local_time, iso_z
from .storage import resolve_day_path, read_json, atomic_write_json, default_payload

def ensure_mt5():
    if mt5.initialize():
        return
    raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

def get_tick(symbol: str):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or tick.time == 0:
        return None
    bid = float(tick.bid)
    ask = float(tick.ask)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(tick.last) if tick.last else 0.0
    return tick, bid, ask, mid

def run_current_price_loop(cfg: PriceSettings):
    ensure_mt5()

    while True:
        got = get_tick(cfg.symbol)
        if got is None:
            time.sleep(cfg.poll_seconds)
            continue

        tick, bid, ask, mid = got
        clk = tick_time_to_clock(tick.time)

        server_dt = to_server_time(clk.tick_time_utc, cfg.server_tz)
        local_dt  = to_local_time(clk.tick_time_utc, cfg.local_tz)

        path = resolve_day_path(cfg.base_dir, cfg.symbol, clk.date_mt5)
        payload = read_json(path) or default_payload(cfg.symbol, clk.date_mt5)

        payload["tz"]["server"] = str(cfg.server_tz)
        payload["tz"]["local"] = getattr(cfg.local_tz, "key", str(cfg.local_tz))

        payload["current"] = {"mid": mid, "bid": bid, "ask": ask}

        nowu = datetime.now(timezone.utc)
        payload["timestamps"] = {
            "updated_utc": nowu.isoformat().replace("+00:00", "Z"),
            "tick_time_utc": iso_z(clk.tick_time_utc),
            "server_time": server_dt.isoformat(),
            "local_time": local_dt.isoformat(),
        }

        atomic_write_json(path, payload, pretty=cfg.pretty_json)
        time.sleep(cfg.poll_seconds)