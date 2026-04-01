from __future__ import annotations

# =============================================================================
# START PRICE READER
# Reads the locked start price from the JSON written by start_price.py.
#
# PATH CONTRACT (must match storage.resolve_start_root_path):
#   {base_dir}/start/{symbol}/start.json
#
# SCHEMA (written by start_price.py):
#   {
#     "status": "LOCKED" | "PENDING",
#     "price": float | null,
#     "date_mt5": "YYYY-MM-DD",          ← MT5/server-day, NOT UTC calendar day
#     "locked_tick_time_utc": "...",
#     "locked_server_time": "...",
#     "locked_local_time": "..."
#   }
#
# FIX: Day validation now uses MT5 server date (date_mt5 field), not naive UTC.
# This prevents stale-lock rejection around rollover / timezone edges.
# =============================================================================

import json
import os
import time
from datetime import datetime, timezone, timedelta

from config import cfg, StrategyConfig


def _root_path(strategy_cfg: StrategyConfig) -> str:
    """
    Mirrors storage.resolve_start_root_path(base_dir, symbol).
    Path: {base_dir}/start/{symbol}/start.json
    """
    return os.path.join(
        strategy_cfg.base_dir, "start", strategy_cfg.symbol, "start.json"
    )


def _mt5_server_date(strategy_cfg: StrategyConfig) -> str:
    """
    Returns today's date in MT5/server timezone as 'YYYY-MM-DD'.
    Uses server_utc_offset_hours from config (default EET = UTC+2).
    This matches the date_mt5 field written by start_price.py.
    """
    offset = timedelta(hours=strategy_cfg.server_utc_offset_hours)
    server_now = datetime.now(timezone.utc) + offset
    return server_now.strftime("%Y-%m-%d")


def read_start_price(strategy_cfg: StrategyConfig = cfg) -> float | None:
    """
    Read today's locked start price.

    Returns:
        float  — locked price if status=LOCKED and date matches MT5 server date
        None   — if missing, PENDING, stale date, or corrupt JSON
    """
    path = _root_path(strategy_cfg)

    if not os.path.exists(path):
        return None

    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if data.get("status") != "LOCKED":
        return None

    price = data.get("price")
    if price is None:
        return None

    # FIX: compare against MT5 server date, not naive UTC calendar date
    locked_date = data.get("date_mt5")
    if locked_date:
        today_mt5 = _mt5_server_date(strategy_cfg)
        if locked_date != today_mt5:
            return None   # stale lock — different server-day

    return float(price)


def read_start_payload(strategy_cfg: StrategyConfig = cfg) -> dict | None:
    """Return full JSON payload for diagnostics/logging."""
    path = _root_path(strategy_cfg)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def wait_for_start_price(
    strategy_cfg: StrategyConfig = cfg,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 3600.0,
    log: bool = True,
) -> float:
    """
    Blocking wait until start price is LOCKED for today's MT5 server date.
    Raises TimeoutError if not resolved within timeout_seconds.
    """
    elapsed = 0.0
    while elapsed < timeout_seconds:
        price = read_start_price(strategy_cfg)
        if price is not None:
            if log:
                print(f"[{strategy_cfg.symbol}] ✅ Start price locked: {price:.2f}")
            return price
        if log and int(elapsed) % 30 == 0:
            print(
                f"[{strategy_cfg.symbol}] ⏳ Waiting for start price... "
                f"({int(elapsed)}s) path={_root_path(strategy_cfg)}"
            )
        time.sleep(poll_seconds)
        elapsed += poll_seconds
    raise TimeoutError(
        f"[{strategy_cfg.symbol}] Start price not locked after {timeout_seconds}s. "
        f"Check path: {_root_path(strategy_cfg)}"
    )


if __name__ == "__main__":
    price = read_start_price()
    if price:
        print(f"Start price: {price:.2f}")
    else:
        print(f"Not locked yet. Path: {_root_path(cfg)}")
        print(f"MT5 server date expected: {_mt5_server_date(cfg)}")
