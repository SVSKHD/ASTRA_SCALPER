from __future__ import annotations

# =============================================================================
# START PRICE READER
# Reads locked start price from JSON written by start_price.py.
#
# SCHEMA (confirmed from 2026-04-01.json):
# {
#   "schema_version": 1,
#   "symbol": "XAUUSD",
#   "date_mt5": "2026-04-01",           ← UTC calendar date
#   "tz": {
#     "mt5_ui": "UTC",                  ← MT5 clock IS UTC
#     "server": "UTC+03:00",            ← display only, not used for locking
#     "local": "UTC+03:30"
#   },
#   "start": {
#     "status": "LOCKED",
#     "price": 4682.735,
#     "source": "tick_lock_..._at_or_after_00:00",
#     "locked_tick_time_utc": "2026-04-01T00:21:09Z",
#     ...
#   },
#   ...
# }
#
# PATH CONTRACT (matches storage.resolve_start_root_path):
#   {base_dir}/start/{symbol}/start.json
#
# DAY VALIDATION: compare date_mt5 against current UTC calendar date.
# Server timezone (UTC+3) is IGNORED — date_mt5 is always a UTC date.
# =============================================================================

import json
import os
import time
from datetime import datetime, timezone

from config import cfg, StrategyConfig


def _root_path(strategy_cfg: StrategyConfig) -> str:
    """
    Path: {base_dir}/start/{symbol}/start.json
    Mirrors storage.resolve_start_root_path(base_dir, symbol).
    """
    return os.path.join(
        strategy_cfg.base_dir, "start", strategy_cfg.symbol, "start.json"
    )


def _utc_date_today() -> str:
    """Current UTC calendar date as 'YYYY-MM-DD'. Matches date_mt5 in JSON."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def read_start_price(strategy_cfg: StrategyConfig = cfg) -> float | None:
    """
    Read today's locked start price.

    Validation:
    1. File must exist at correct path
    2. status must be "LOCKED"
    3. price must be non-null
    4. date_mt5 must match today's UTC date (prevents using yesterday's lock)

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

    # Must be in LOCKED state
    if data.get("status") != "LOCKED":
        # Also handle nested start block (build_start_root_payload may flatten)
        start = data.get("start", {})
        if start.get("status") != "LOCKED":
            return None
        price     = start.get("price")
        date_mt5  = data.get("date_mt5")
    else:
        price    = data.get("price")
        date_mt5 = data.get("date_mt5")

    if price is None:
        return None

    # Validate date_mt5 against UTC today
    if date_mt5:
        if date_mt5 != _utc_date_today():
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
    Read start price directly from a day JSON file (e.g. 2026-04-01.json).
    Used by backtest to load historical start prices from day files.
    Does NOT validate date — caller controls which file to load.
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
    """
    Parse locked_tick_time_utc from a day JSON file.
    Returns ISO string or None.
    Used by backtest to know exactly when start was locked on a given day.
    """
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
    """
    Blocking wait until today's start price is LOCKED.
    Polls the JSON file every poll_seconds.
    """
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
        print(f"Start price: {price:.3f}")
    else:
        print(f"Not locked yet. Path: {_root_path(cfg)}")
        print(f"UTC date expected: {_utc_date_today()}")
