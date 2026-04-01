from __future__ import annotations

# =============================================================================
# START PRICE READER
# Reads locked start price from JSON written by start_price.py.
#
# PATH CONTRACT — must match storage.resolve_start_root_path:
#   storage.py:  os.path.join(base_dir, "start_price", f"{symbol}.json")
#   → data/start_price/XAUUSD.json
#
# SCHEMA (from build_start_root_payload in storage.py):
# {
#   "schema_version": 1,
#   "symbol": "XAUUSD",
#   "date_mt5": "2026-04-01",           ← UTC calendar date
#   "tz":  { "mt5_ui": "UTC", "server": "UTC+03:00", ... },
#   "start": {
#     "status": "LOCKED",
#     "price": 4682.735,
#     "locked_tick_time_utc": "2026-04-01T00:21:09Z",
#     ...
#   },
#   "meta": { ... },
#   "extremes": { ... },
#   "extreme_events": [...]
# }
#
# TIMING (confirmed from 2026-04-01.json + pricing/settings.py):
#   - lock_hhmm_mt5 = "00:00" UTC
#   - date_mt5      = UTC calendar date
#   - Server UTC+3  = display only, not used for locking
# =============================================================================

import json
import os
import time
from datetime import datetime, timezone

from config import cfg, StrategyConfig


def _root_path(strategy_cfg: StrategyConfig) -> str:
    """
    Mirrors storage.resolve_start_root_path(base_dir, symbol):
        os.path.join(base_dir, "start_price", f"{symbol}.json")
    → data/start_price/XAUUSD.json
    """
    return os.path.join(
        strategy_cfg.base_dir, "start_price", f"{strategy_cfg.symbol}.json"
    )


def _utc_date_today() -> str:
    """UTC calendar date as 'YYYY-MM-DD'. Matches date_mt5 written by start_price.py."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Kept for backward compatibility with runner.py import
def _mt5_server_date(strategy_cfg: StrategyConfig = cfg) -> str:
    """UTC date — MT5 UI clock is UTC, so server date = UTC date."""
    return _utc_date_today()


def read_start_price(strategy_cfg: StrategyConfig = cfg) -> float | None:
    """
    Read today's locked start price.

    Checks in order:
    1. File at data/start_price/<symbol>.json must exist
    2. start.status must be "LOCKED"
    3. start.price must be non-null
    4. date_mt5 must match today's UTC date

    Returns float or None.
    """
    path = _root_path(strategy_cfg)
    if not os.path.exists(path):
        return None

    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    date_mt5 = data.get("date_mt5")

    # Nested schema (build_start_root_payload format):
    # { "date_mt5": "...", "start": { "status": "LOCKED", "price": ... } }
    start = data.get("start", {})
    if start.get("status") == "LOCKED":
        price = start.get("price")
    # Flat schema fallback (older format):
    # { "status": "LOCKED", "price": ..., "date_mt5": "..." }
    elif data.get("status") == "LOCKED":
        price = data.get("price")
    else:
        return None

    if price is None:
        return None

    # Validate UTC date
    if date_mt5 and date_mt5 != _utc_date_today():
        return None

    return float(price)


def read_start_payload(strategy_cfg: StrategyConfig = cfg) -> dict | None:
    """Full JSON payload for diagnostics."""
    path = _root_path(strategy_cfg)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def read_start_price_from_file(file_path: str) -> float | None:
    """
    Read start price from a specific day JSON file.
    Used by backtest. Does NOT validate date.
    """
    try:
        with open(file_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    start = data.get("start", {})
    if start.get("status") != "LOCKED":
        return None
    price = start.get("price")
    return float(price) if price is not None else None


def parse_lock_utc(file_path: str) -> str | None:
    """Parse locked_tick_time_utc from a day JSON file."""
    try:
        with open(file_path) as f:
            data = json.load(f)
        return data.get("start", {}).get("locked_tick_time_utc")
    except Exception:
        return None


def wait_for_start_price(
    strategy_cfg: StrategyConfig = cfg,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 3600.0,
    log: bool = True,
) -> float:
    """Blocking wait until today's start price is LOCKED."""
    elapsed = 0.0
    while elapsed < timeout_seconds:
        price = read_start_price(strategy_cfg)
        if price is not None:
            if log:
                print(f"[{strategy_cfg.symbol}] ✅ Start price locked: {price:.3f}")
            return price
        if log and int(elapsed) % 30 == 0:
            print(
                f"[{strategy_cfg.symbol}] ⏳ Waiting for start price... "
                f"({int(elapsed)}s)  path={_root_path(strategy_cfg)}"
            )
        time.sleep(poll_seconds)
        elapsed += poll_seconds
    raise TimeoutError(
        f"[{strategy_cfg.symbol}] Start price not locked after {timeout_seconds}s. "
        f"Path: {_root_path(strategy_cfg)}"
    )


if __name__ == "__main__":
    price = read_start_price()
    if price:
        print(f"Start price : {price:.3f}")
        print(f"Path        : {_root_path(cfg)}")
    else:
        print(f"Not locked yet.")
        print(f"Expects     : {_root_path(cfg)}")
        print(f"UTC date    : {_utc_date_today()}")
