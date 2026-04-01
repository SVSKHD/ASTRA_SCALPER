# price_assembly.py
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import MetaTrader5 as mt5

from settings import PriceSettings
from clock import tick_time_to_clock, to_server_time, to_local_time, iso_z
from storage import resolve_start_root_path, read_json


def _ensure_symbol_selected(symbol: str) -> bool:
    """Ensure the symbol is subscribed/selected in MarketWatch for tick streaming."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return False
    try:
        mt5.symbol_select(symbol, True)
    except Exception:
        pass
    return True


def ensure_mt5():
    if mt5.initialize():
        return
    raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")


def _get_current_from_tick(symbol: str) -> Optional[Dict[str, float]]:
    try:
        if not _ensure_symbol_selected(symbol):
            return None

        tick = mt5.symbol_info_tick(symbol)
        if tick is None or getattr(tick, "time", 0) == 0:
            return None

        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        last = float(getattr(tick, "last", 0.0) or 0.0)

        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        elif last > 0:
            mid = last
        elif bid > 0:
            mid = bid
        elif ask > 0:
            mid = ask
        else:
            return None

        return {"bid": bid, "ask": ask, "mid": mid, "tick_time_epoch": int(tick.time)}
    except Exception:
        return None


def build_price_packet(symbol: str, cfg: PriceSettings) -> Optional[Dict[str, Any]]:
    try:
        cur = _get_current_from_tick(symbol)
        if cur is None:
            return None

        clk = tick_time_to_clock(cur["tick_time_epoch"])
        server_dt = to_server_time(clk.tick_time_utc, cfg.server_tz)
        local_dt = to_local_time(clk.tick_time_utc, cfg.local_tz)

        start_path = resolve_start_root_path(cfg.base_dir, symbol)
        start_root = read_json(start_path)

        start_is_for_today = False  # ✅ default
        start_block = None  # ✅ default

        if isinstance(start_root, dict):
            s = (start_root.get("start") or {})
            start_is_for_today = (
                    start_root.get("date_mt5") == clk.date_mt5
                    and s.get("status") == "LOCKED"
                    and s.get("price") is not None
            )
            if start_is_for_today:
                start_block = {
                    "status": s.get("status"),
                    "price": s.get("price"),
                    "source": s.get("source"),
                    "date_mt5": start_root.get("date_mt5"),  # also fix naming, see below
                    "locked_tick_time_utc": s.get("locked_tick_time_utc"),
                    "locked_server_time": s.get("locked_server_time"),
                    "locked_local_time": s.get("locked_local_time"),
                }

        return {
            "symbol": symbol,
            "start": start_block,
            "current": {
                "mid": cur["mid"],
                "bid": cur["bid"],
                "ask": cur["ask"],
                "tick_time_epoch": cur["tick_time_epoch"],
                "mt5_ui_utc": iso_z(clk.tick_time_utc),
                "server_time": server_dt.isoformat(),
                "local_time": local_dt.isoformat(),
            },
            "meta": {
                "date_mt5": clk.date_mt5,
                "hhmm_mt5": clk.time_mt5_hhmm,
                "mt5_ui_human": clk.tick_time_utc.strftime("%Y-%m-%d %H:%M"),
                "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "updated_from_tick_utc": iso_z(clk.tick_time_utc),
                "start_is_for_today": start_is_for_today,
                "start_root_path": start_path,
            },
        }
    except Exception as e:
        try:
            print(f"[{symbol}] ❌ build_price_packet error: {e!r} | mt5_last_error={mt5.last_error()}")
        except Exception:
            pass
        return None


def run_price_assembly_loop(symbol: str, cfg: PriceSettings):
    ensure_mt5()
    while True:
        pkt = build_price_packet(symbol, cfg)
        if pkt is None:
            print(f"[{symbol}] no tick")
        else:
            print(pkt)
        time.sleep(cfg.poll_seconds)