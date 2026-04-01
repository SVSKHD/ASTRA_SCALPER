# storage.py
from __future__ import annotations

import os
import json
import time
import traceback
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# --------------------------
# Paths
# --------------------------

def resolve_day_path(base_dir: str, symbol: str, date_mt5: str) -> str:
    """
    Per-day audit file:
      <base_dir>/<symbol>/<YYYY-MM-DD>.json
    """
    return os.path.join(base_dir, symbol, f"{date_mt5}.json")

def resolve_start_root_path(base_dir: str, symbol: str) -> str:
    """
    ROOT start-price file (shared across runners):
      <base_dir>/start_price/<symbol>.json
    """
    return os.path.join(base_dir, "start_price", f"{symbol}.json")

def resolve_price_assembly_root_path(base_dir: str, symbol: str) -> str:
    """
    ROOT assembled price packet for strategy to read:
      <base_dir>/price_assembly/<symbol>.json
    """
    return os.path.join(base_dir, "price_assembly", f"{symbol}.json")


# --------------------------
# IO
# --------------------------

def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None

def _safe_makedirs_for_file(path: str) -> None:
    dirn = os.path.dirname(path)
    if dirn:
        os.makedirs(dirn, exist_ok=True)

def _write_text_fallback(path: str, payload: Dict[str, Any], err: Exception) -> None:
    """
    Final fallback: write a .txt snapshot so you can recover manually.
    """
    try:
        _safe_makedirs_for_file(path)
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        safe_ts = ts.replace(":", "-")
        txt_path = path + f".FAILED_WRITE.{safe_ts}.txt"

        lines = []
        lines.append("=== WRITE FAILURE FALLBACK ===")
        lines.append(f"UTC_TIME: {ts}")
        lines.append(f"TARGET_JSON: {path}")
        lines.append(f"ERROR: {repr(err)}")
        lines.append("")
        lines.append("TRACEBACK:")
        lines.append(traceback.format_exc())
        lines.append("")
        lines.append("PAYLOAD:")
        lines.append(json.dumps(payload, indent=2, ensure_ascii=False))

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        # Never crash from fallback
        pass

def append_jsonl(path: str, obj: Dict[str, Any]) -> bool:
    """
    Append one JSON object per line (history mode).
    """
    try:
        _safe_makedirs_for_file(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False

def atomic_write_json(path: str, payload: Dict[str, Any], pretty: bool = True) -> bool:
    """
    Never-raise writer:
    - tmp write
    - os.replace retry
    - direct write fallback
    - txt snapshot fallback
    Returns True if JSON persisted, False otherwise.
    """
    _safe_makedirs_for_file(path)
    tmp = path + ".tmp"

    try:
        # Write temp
        with open(tmp, "w", encoding="utf-8") as f:
            if pretty:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            else:
                json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

        last_err: Exception | None = None

        # Try atomic replace with retries
        for attempt in range(15):
            try:
                os.replace(tmp, path)
                return True
            except (PermissionError, OSError) as e:
                last_err = e
                time.sleep(0.05 * (attempt + 1))

        # Fallback: direct overwrite
        try:
            with open(path, "w", encoding="utf-8") as f:
                if pretty:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                else:
                    json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
            # Cleanup tmp
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return True
        except Exception as e:
            last_err = e

        # Final fallback: txt snapshot
        if last_err is not None:
            _write_text_fallback(path, payload, last_err)
        return False

    except Exception as e:
        # Cleanup tmp
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

        _write_text_fallback(path, payload, e)
        return False

def resolve_start_emergency_path(base_dir: str, symbol: str) -> str:
    """
    Emergency append-only log for locked start prices:
      <base_dir>/start_price/_emergency_<symbol>.log
    """
    return os.path.join(base_dir, "start_price", f"_emergency_{symbol}.log")

def append_line(path: str, line: str) -> None:
    try:
        _safe_makedirs_for_file(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


# --------------------------
# Schema / Defaults
# --------------------------

def default_payload(symbol: str, date_mt5: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "symbol": symbol,
        "date_mt5": date_mt5,
        "tz": {"mt5_ui": "UTC", "server": None, "local": None},
        "timestamps": {"updated_utc": None, "tick_time_utc": None, "server_time": None, "local_time": None},
        "start": {
            "status": "PENDING",
            "price": None,
            "source": None,
            "locked_tick_time_utc": None,
            "locked_server_time": None,
            "locked_local_time": None,
        },
        "current": {"mid": None, "bid": None, "ask": None},
        "meta": {"market_open": True, "rollover_detected": False, "last_rollover_from": None},
        "extremes": {
            "high": None,
            "high_tick_time_utc": None,
            "high_server_time": None,
            "high_local_time": None,
            "low": None,
            "low_tick_time_utc": None,
            "low_server_time": None,
            "low_local_time": None,
            "backfilled": False,
        },
        "extreme_events": [],
    }

def build_start_root_payload(payload: dict) -> dict:
    return {
        "schema_version": payload.get("schema_version", 1),
        "symbol": payload.get("symbol"),
        "date_mt5": payload.get("date_mt5"),
        "tz": payload.get("tz", {}),
        "timestamps": payload.get("timestamps", {}),
        "start": payload.get("start", {}),
        "meta": payload.get("meta", {}),
        "extremes": payload.get("extremes", {}),
        "extreme_events": payload.get("extreme_events", []),
    }