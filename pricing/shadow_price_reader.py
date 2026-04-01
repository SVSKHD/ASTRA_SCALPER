"""
Revamp / astra-hawk-2026 — Shadow price reader.

Standalone tick + bar writer for shadow symbols.
Completely separate from pricing/run_start_price.py.
Writes to: pricing/data/shadow_price/SYMBOL.json

Zero interaction with pricing/run_start_price.py or its data files.

Usage:
    manager = ShadowPriceManager()        # reads list_shadow_symbols()
    manager.start_all()                    # starts daemon threads
    data = manager.read("EURUSD")          # reads latest JSON
    manager.stop_all()

Shadow JSON format:
{
  "symbol": "EURUSD",
  "timestamp_utc": "2026-03-29T14:32:00Z",
  "bid": 1.08210, "ask": 1.08215, "mid": 1.08212,
  "bar": {
    "bar_time": "2026-03-29T14:30:00",
    "bar_open": 1.08200, "bar_high": 1.08250,
    "bar_low": 1.08190, "bar_close": 1.08212,
    "bar_closed": true
  },
  "session_open": 1.08150,
  "updated_at": "2026-03-29T14:32:01Z"
}
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _safe_print(msg: str) -> None:
    try:
        safe = msg.encode("ascii", errors="replace").decode("ascii")
        print(safe, flush=True)
    except Exception:
        pass


# ── BAR ACCUMULATOR (self-contained copy from apex_reader pattern) ───────────

@dataclass
class _BarAccumulator:
    """
    Converts a tick stream into discrete OHLC bars.
    tick() returns True when a bar just closed.
    """
    bar_minutes: int

    _cur_time:  Optional[str]   = field(default=None, repr=False)
    _cur_open:  Optional[float] = field(default=None, repr=False)
    _cur_high:  Optional[float] = field(default=None, repr=False)
    _cur_low:   Optional[float] = field(default=None, repr=False)
    _cur_close: Optional[float] = field(default=None, repr=False)

    _done_time:  Optional[str]   = field(default=None, repr=False)
    _done_open:  Optional[float] = field(default=None, repr=False)
    _done_high:  Optional[float] = field(default=None, repr=False)
    _done_low:   Optional[float] = field(default=None, repr=False)
    _done_close: Optional[float] = field(default=None, repr=False)

    def _snap(self, ts: datetime) -> str:
        epoch    = int(ts.replace(tzinfo=timezone.utc).timestamp())
        bar_secs = self.bar_minutes * 60
        return datetime.fromtimestamp(
            (epoch // bar_secs) * bar_secs, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")

    def tick(self, price: float) -> bool:
        now = datetime.now(tz=timezone.utc)
        bt  = self._snap(now)

        if self._cur_time is None:
            self._cur_time  = bt
            self._cur_open  = self._cur_high = self._cur_low = self._cur_close = price
            return False

        if bt == self._cur_time:
            self._cur_high  = max(self._cur_high,  price)
            self._cur_low   = min(self._cur_low,   price)
            self._cur_close = price
            return False

        # Bar just closed
        self._done_time  = self._cur_time
        self._done_open  = self._cur_open
        self._done_high  = self._cur_high
        self._done_low   = self._cur_low
        self._done_close = self._cur_close

        self._cur_time  = bt
        self._cur_open  = self._cur_high = self._cur_low = self._cur_close = price
        return True

    @property
    def closed(self) -> dict[str, Any]:
        return {
            "bar_time":  self._done_time,
            "bar_open":  self._done_open,
            "bar_high":  self._done_high,
            "bar_low":   self._done_low,
            "bar_close": self._done_close,
        }


# ── ATOMIC WRITE ─────────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via tmp file. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                time.sleep(0.2)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _read_json(path: Path) -> Optional[dict]:
    """Read JSON file. Never raises."""
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


# ── SHADOW PRICE READER (per-symbol) ─────────────────────────────────────────

class ShadowPriceReader:
    """
    Polls MT5 ticks for one shadow symbol, accumulates bars,
    writes shadow JSON to output_dir/SYMBOL.json.
    """

    def __init__(
        self,
        symbol: str,
        bar_minutes: int = 15,
        output_dir: str = "Revamp/pricing/data/shadow_price",
        poll_seconds: float = 0.5,
    ):
        self._symbol = symbol
        self._bar_minutes = bar_minutes
        self._output_dir = Path(output_dir)
        self._poll_seconds = poll_seconds
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._bar = _BarAccumulator(bar_minutes=bar_minutes)
        self._session_open: Optional[float] = None
        self._last_day: Optional[str] = None

    def start(self) -> None:
        """Start daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name=f"shadow-price-{self._symbol}",
            daemon=True,
        )
        self._thread.start()
        _safe_print(f"[ShadowPrice] started {self._symbol}")

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def read(self) -> Optional[dict]:
        """Read own JSON file."""
        return _read_json(self._output_dir / f"{self._symbol}.json")

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick_once()
            except Exception:
                pass
            time.sleep(self._poll_seconds)

    def _tick_once(self) -> None:
        """Read MT5 tick, accumulate bar, write JSON."""
        try:
            from Revamp.utils import mt5
            from Revamp.trade.mt5_core import ensure_mt5, select_symbol
        except Exception:
            return

        try:
            st = ensure_mt5("shadow_price")
            if not st.get("ok"):
                return
            select_symbol(self._symbol)
            tick = mt5.symbol_info_tick(self._symbol)
            if tick is None:
                return
        except Exception:
            return

        bid = float(getattr(tick, "bid", 0.0))
        ask = float(getattr(tick, "ask", 0.0))
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2.0

        now_utc = datetime.now(timezone.utc)
        today = now_utc.strftime("%Y-%m-%d")

        # Reset session_open at midnight
        if self._last_day != today:
            self._session_open = mid
            self._last_day = today
        if self._session_open is None:
            self._session_open = mid

        # Accumulate bar
        bar_closed = self._bar.tick(mid)

        bar_data = self._bar.closed if bar_closed else {
            "bar_time":  self._bar._cur_time,
            "bar_open":  self._bar._cur_open,
            "bar_high":  self._bar._cur_high,
            "bar_low":   self._bar._cur_low,
            "bar_close": self._bar._cur_close,
        }
        bar_data["bar_closed"] = bar_closed

        payload = {
            "symbol": self._symbol,
            "timestamp_utc": now_utc.isoformat().replace("+00:00", "Z"),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "bar": bar_data,
            "session_open": self._session_open,
            "updated_at": now_utc.isoformat().replace("+00:00", "Z"),
        }

        _atomic_write_json(self._output_dir / f"{self._symbol}.json", payload)


# ── SHADOW PRICE MANAGER ────────────────────────────────────────────────────

class ShadowPriceManager:
    """
    Manages ShadowPriceReader instances for all shadow symbols.
    """

    def __init__(self, symbols: Optional[List[str]] = None):
        if symbols is None:
            try:
                from Revamp.config import list_shadow_symbols
                symbols = list_shadow_symbols()
            except Exception:
                symbols = []
        self._symbols = symbols
        self._readers: Dict[str, ShadowPriceReader] = {}

    def start_all(self) -> None:
        for sym in self._symbols:
            if sym not in self._readers:
                self._readers[sym] = ShadowPriceReader(sym)
            self._readers[sym].start()
        _safe_print(
            f"[ShadowPriceManager] started {len(self._readers)} readers")

    def stop_all(self) -> None:
        for reader in self._readers.values():
            try:
                reader.stop()
            except Exception:
                pass
        _safe_print("[ShadowPriceManager] stopped")

    def read(self, symbol: str) -> Optional[dict]:
        reader = self._readers.get(symbol)
        if reader is None:
            return None
        return reader.read()
