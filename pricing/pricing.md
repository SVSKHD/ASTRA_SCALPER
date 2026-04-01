# pricing/ — Module Documentation

## Overview

This folder handles all real-time price data acquisition, assembly, and persistence for astra-hawk-2026.
It connects to MetaTrader5, locks a daily start price per symbol, assembles enriched price packets,
and writes live + historical outputs that strategy chips consume.

---

## Folder Structure

```
pricing/
├── clock.py            # Tick epoch → Clock dataclass (UTC / server / local)
├── current_price.py    # Standalone single-symbol price writer (legacy/debug use)
├── price_assembly.py   # Builds enriched price packet per tick
├── price_runner.py     # Multi-symbol threaded runner (main entry point)
├── settings.py         # Frozen config dataclass (PriceSettings)
├── start_price.py      # Locks daily start price per symbol
├── storage.py          # Atomic file IO, path resolution, schema defaults
├── README.md           # This file
│
notify/
├── discord.py          # Discord webhook notifications
└── telegram.py         # Telegram bot notifications

data/                   # Runtime output (gitignore this)
├── start_price/
│   ├── XAUUSD.json                  # ROOT start price (read by price_assembly)
│   ├── EURUSD.json
│   └── _emergency_XAUUSD.log        # Append-only audit log
├── price_assembly/
│   ├── XAUUSD.json                  # LIVE packet (strategy reads this)
│   ├── XAUUSD_2026-03-05.json       # Daily snapshot
│   └── XAUUSD_2026-03-05.jsonl     # Full tick history (one JSON per line)
└── XAUUSD/
    └── 2026-03-05.json              # Per-day audit file (start_price writes)
```

---

## Data Flow

```
MT5 Terminal
     │
     ├──────────────────────────────────────────┐
     │                                          │
     ▼                                          ▼
start_price.py                         price_assembly.py
(per symbol thread)                    (called by price_runner)
     │                                          │
     │  Locks start price at 00:00 MT5          │  Reads ROOT start file
     │  Writes day audit file                   │  Gets live tick from MT5
     │                                          │  Builds enriched packet
     ▼                                          │
data/start_price/<SYMBOL>.json  ◄───────────────┘
(ROOT — shared across runners)
     │
     ▼
price_runner.py
(one thread per symbol, daemon)
     │
     │  Calls build_price_packet()
     │  Adds intraday H/L (recovered from disk on restart)
     │  Adds stale detection
     │
     ├──► data/price_assembly/<SYMBOL>.json          (LIVE — strategy reads)
     ├──► data/price_assembly/<SYMBOL>_<DATE>.json   (daily snapshot)
     └──► data/price_assembly/<SYMBOL>_<DATE>.jsonl  (full tick history)
```

---

## File Responsibilities

### `clock.py`
- Converts MT5 tick epoch (`int`) → `Clock` dataclass
- Provides `date_mt5` (YYYY-MM-DD) and `time_mt5_hhmm` (HH:MM) strings
- Utility functions: `to_server_time`, `to_local_time`, `to_ist_time`, `iso_z`
- Pure, no side effects, no MT5 dependency

### `settings.py`
- Single `PriceSettings` frozen dataclass
- All tunable parameters in one place

| Field | Default | Purpose |
|---|---|---|
| `base_dir` | `"data"` | Root output directory |
| `symbol` | `"XAUUSD"` | Default symbol for current_price.py |
| `poll_seconds` | `0.3` | Tick polling interval |
| `status_print_seconds` | `5.0` | Console log interval |
| `lock_hhmm_mt5` | `"00:00"` | Start price lock window (MT5 time) |
| `allow_bootstrap_lock` | `True` | Allow lock on mid-day restart |
| `server_tz` | `UTC+3` | Broker server timezone |
| `local_tz` | `UTC+5:30` | IST local timezone |
| `stale_after_seconds` | `20` | Seconds before tick marked stale |
| `pretty_json` | `False` | Pretty print JSON (False in prod) |

### `storage.py`
- All file IO goes through here — no direct `open()` elsewhere
- `atomic_write_json`: tmp write → `os.replace` (15 retries) → direct fallback → `.txt` snapshot
- `append_jsonl`: appends one JSON object per line for history
- `append_line`: append-only emergency log
- `read_json`: returns `None` on missing/corrupt file, never raises
- Path resolvers:
  - `resolve_day_path` → `data/<SYMBOL>/<DATE>.json`
  - `resolve_start_root_path` → `data/start_price/<SYMBOL>.json`
  - `resolve_price_assembly_root_path` → `data/price_assembly/<SYMBOL>.json`
  - `resolve_start_emergency_path` → `data/start_price/_emergency_<SYMBOL>.log`

### `start_price.py`
- One thread per symbol (launched from `__main__` or orchestrator)
- **Lock policy**: locks start price at/after `lock_hhmm_mt5` (default `00:00`)
  - Only locks if: day file exists OR within midnight grace (10 min) OR `allow_bootstrap_lock=True`
  - Once locked, never re-locks for that MT5 date
- **Rollover detection**: detects MT5 date change mid-run, resets start block, logs to emergency file
- **Stale detection**: uses `cfg.stale_after_seconds` — skips write if stale and not status-print interval
- Emergency log at `data/start_price/_emergency_<SYMBOL>.log` records every lock and rollover

### `price_assembly.py`
- `build_price_packet(symbol, cfg)` → `Dict` or `None`
- Reads live tick from MT5 via `_get_current_from_tick`
- Reads ROOT start file from disk
- Returns packet structure:
```json
{
  "symbol": "XAUUSD",
  "start": { "status": "LOCKED", "price": 5140.73, ... },
  "current": { "mid": 5129.24, "bid": 5129.10, "ask": 5129.38, ... },
  "meta": { "date_mt5": "2026-03-05", "start_is_for_today": true, ... }
}
```
- `start` is `null` if start price not yet locked for today
- All exceptions caught — returns `None` on any failure

### `price_runner.py`
- **Main entry point** for multi-symbol live operation
- `run_price_runner(cfg, enabled_symbols)` spawns one daemon thread per symbol
- Per thread (`_symbol_thread`):
  - Calls `build_price_packet` every `poll_seconds`
  - Tracks intraday high/low in memory, **recovered from disk on restart**
  - Stale detection: compares `tick_time_epoch` across polls
  - Writes 3 outputs per tick: LIVE JSON, daily JSON, JSONL history
  - Heartbeat write (`NO_TICK`) when MT5 returns no data
  - Diagnostics printed when no tick received

### `current_price.py`
- Standalone single-symbol price writer (debug / legacy)
- Not called by `price_runner.py` — run directly if needed
- Writes to `data/<SYMBOL>/<DATE>.json`

---

## Tick Packet Schema (strategy input)

Strategy chips read `data/price_assembly/<SYMBOL>.json`:

```json
{
  "symbol": "XAUUSD",
  "start": {
    "status": "LOCKED",
    "price": 5140.73,
    "source": "tick_lock_existing_dayfile_at_or_after_00:00",
    "date_mt5": "2026-03-05",
    "locked_tick_time_utc": "2026-03-05T00:00:03Z",
    "locked_server_time": "2026-03-05T03:00:03+03:00",
    "locked_local_time": "2026-03-05T05:30:03+05:30"
  },
  "current": {
    "mid": 5129.245,
    "bid": 5129.10,
    "ask": 5129.39,
    "tick_time_epoch": 1741190700,
    "mt5_ui_utc": "2026-03-05T16:05:00Z",
    "server_time": "2026-03-05T19:05:00+03:00",
    "local_time": "2026-03-05T21:35:00+05:30"
  },
  "high": {
    "since_day_start": 5142.10,
    "mt5_ui_utc": "2026-03-05T08:12:00Z",
    "server_time": "2026-03-05T11:12:00+03:00",
    "date_mt5": "2026-03-05"
  },
  "low": {
    "since_day_start": 5118.04,
    "mt5_ui_utc": "2026-03-05T14:33:00Z",
    "server_time": "2026-03-05T17:33:00+03:00",
    "date_mt5": "2026-03-05"
  },
  "meta": {
    "date_mt5": "2026-03-05",
    "hhmm_mt5": "16:05",
    "mt5_ui_human": "2026-03-05 16:05",
    "updated_utc": "2026-03-05T16:05:01Z",
    "start_is_for_today": true,
    "stale_seconds": 0,
    "is_stale": false
  }
}
```

---

## Running

### Start price locker only
```bash
python start_price.py
```

### Full multi-symbol price runner (production)
```bash
python price_runner.py
```

### Add/remove symbols
Edit `enabled_symbols` in `price_runner.py __main__`:
```python
enabled_symbols = ["XAUUSD", "EURUSD"]
```

---

## Bugs Fixed (patch log)

| # | File | Issue | Fix |
|---|---|---|---|
| 1 | `price_assembly.py` | `start_is_for_today` `NameError` when `start_root` is `None` | Initialize to `False` before `if` block |
| 2 | `current_price.py` | `ImportError` — `resolve_path` doesn't exist in `storage.py` | Changed to `resolve_day_path` |
| 3 | `start_price.py` | Module-level `STALE_AFTER_SECONDS` constant shadowed `cfg.stale_after_seconds` | Removed constant, use `cfg` throughout |
| 4 | `price_runner.py` | Intraday H/L lost on process restart | `_try_recover_hl()` reads daily JSON on day change |
| 5 | `settings.py` | `symbol` field missing (required by `current_price.py`) | Added `symbol: str = "XAUUSD"` |
| 6 | `settings.py` | `pretty_json=True` causing high IO at 0.3s poll | Defaulted to `False` for prod |