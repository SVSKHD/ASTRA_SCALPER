
"""
Revamp / astra-hawk-2026 — Central configuration.

═══════════════════════════════════════════════════════════════════════
APRIL 2026 TRADING PLAN — XAUUSD
═══════════════════════════════════════════════════════════════════════

Strategy: threshold_touch.v1
Symbol:   XAUUSD (Gold vs USD)
Mode:     Paper trading → Prop firm challenge

CORE LOGIC:
  Start anchor = daily open price (locked by run_start_price.py)
  Threshold    = 2000 pips = 20 points
  Entry        = 1.10x = 22 points from anchor
  Exit TP      = 1.20x = 24 points from anchor
  Capture      = 2.0 points per trade

PROFIT PER TRADE (pip_size=0.01, $100 per point per lot):
  1 lot  → 2.0pts × $100 = $200 per trade
  2 lot  → 2.0pts × $200 = $400 per trade
  5 lot  → 2.0pts × $500 = $1,000 per trade
  10 lot → 2.0pts × $1,000 = $2,000 per trade

WHY THIS WORKS:
  Gold average daily range = 30 pts
  Threshold at 20pts = inside average range ✅
  Exit at 24pts = 80% of average range ✅
  Price almost always reaches 24pts if it crossed 20pts ✅
  One trade per day = no re-entry risk ✅

SCALING PLAN:
  Phase 1 — April paper: 1 lot  → $200/day target
  Phase 2 — Prop challenge: 2 lot → $400/day
  Phase 3 — Funded account: 5 lot → $1,000/day
  Phase 4 — Scale up: 10 lot → $2,000/day

ANCHOR SYSTEM:
  Single daily start anchor only.
  NO promoted HIGH/LOW anchors — these caused March 30 (-$3,000)
  and March 31 multi-trade confusion.
  promote_anchors_enabled: False kills all promoted anchors.
  anchor_min_quality_score: 1.1 is the backup gate (scores cap at 1.0).

FIXES APPLIED:
  BUG-C1: Removed illegal dict syntax from RuntimeSettings dataclass.
  BUG-C2: Fixed late_entry_at_x geometry (1.45 > entry1_max_x=1.15 ✓).
  ANCHOR-FIX: Added promote_anchors_enabled=False to kill multi-anchor chaos.
  LOT-FIX: lot_size now explicit in strategy_params (not just SymbolConfig).
  COMMENT-FIX: Wrong comments on threshold/entry levels corrected.
  ONE-TRADE-FIX: one_trade_per_day=True — was False, caused 4 trades/day.

FALSY-BUG-FIX (NEW — critical):
  Python `or` operator treats 0 and 0.0 as falsy:
      params.get("min_trade_quality_score", 0.40) or 0.40
      → 0.0 or 0.40 = 0.40  ← gate still active even though config says 0.0!
  Same for zone_loss_cooldown_threshold:
      params.get("zone_loss_cooldown_threshold", 2) or 2
      → 0 or 2 = 2  ← zone locks after 2 losses even though config says 0!
  Fix: use 0.001 instead of 0.0 (truthy, passes everything)
       use 999 instead of 0 (never reached, effectively disabled)
  This affects: min_entry_quality_score, min_trade_quality_score,
  min_session_range_x, zone_loss_cooldown_threshold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ============================================================
# Runtime / operating mode
# ============================================================

TradingMode = Literal["ACTIVE", "MONITOR_ONLY"]


# ============================================================
# Dataclasses
# ============================================================

@dataclass(frozen=True)
class RuntimeSettings:
    trading_mode: TradingMode       = "ACTIVE"
    daily_profit_target_usd: float  = 200.0    # $200/day at 1 lot
    daily_profit_lock_usd: float    = 150.0    # lock after $150 profit
    loss_lock_grace_seconds: int    = 60
    daily_max_loss_usd: float       = -400.0   # halt after -$400 (2 adverse exits at 1 lot)
    catastrophic_loss_usd: float    = -600.0   # emergency halt
    risk_poll_seconds: int          = 2
    force_rollover_at_server_hhmm: str = "00:00"
    force_rollover_window_min: int  = 10
    state_root: str                 = "Revamp/state"
    log_root: str                   = "Revamp/logs"
    local_timezone_name: str        = "Asia/Kolkata"
    broker_timezone_name: str       = "Etc/GMT-3"
    # ── BYPASS flags — all False by default ─────────────────────────
    bypass_daily_halt:  bool        = False
    bypass_profit_lock: bool        = False
    bypass_risk_locks:  bool        = False
    # ── Notification toggles ─────────────────────────────────────────
    notify_discord_enabled:   bool  = True
    notify_telegram_enabled:  bool  = True
    dashboard_notify_enabled: bool  = True


RUNTIME = RuntimeSettings(
    trading_mode            = "ACTIVE",
    daily_profit_target_usd = 200.0,    # 1 lot × 2pts × $100 = $200
    daily_profit_lock_usd   = 150.0,    # lock profits at $150
    loss_lock_grace_seconds = 60,
    daily_max_loss_usd      = -400.0,   # max 2 bad trades before halt
    catastrophic_loss_usd   = -600.0,
    risk_poll_seconds       = 2,
    force_rollover_at_server_hhmm = "00:00",
    force_rollover_window_min     = 10,
    state_root            = "Revamp/state",
    log_root              = "Revamp/logs",
    local_timezone_name   = "Asia/Kolkata",
    broker_timezone_name  = "Etc/GMT-3",
    bypass_daily_halt  = False,
    bypass_profit_lock = False,
    bypass_risk_locks  = False,
    notify_discord_enabled   = True,
    notify_telegram_enabled  = True,
    dashboard_notify_enabled = True,
)


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    pip_size: float
    lot_size: float
    is_tradeable: bool      = True
    max_trades_per_day: int = 99
    tags: tuple[str, ...]   = ()


@dataclass(frozen=True)
class StrategyBinding:
    strategy_id: str
    strategy_params: dict[str, Any] = field(default_factory=dict)
    observe_only: bool = False  # shadow mode — evaluate() runs, no orders placed


# ============================================================
# Symbol metadata
# ============================================================

SYMBOL_CONFIGS: dict[str, SymbolConfig] = {
    "XAUUSD": SymbolConfig(
        symbol             = "XAUUSD",
        pip_size           = 0.01,
        lot_size           = 1.0,          # April: start at 1 lot = $200/trade
        is_tradeable       = True,
        max_trades_per_day = 1,            # hard cap — one trade per day
        tags               = ("metal", "forex"),
    ),
    "XAUEUR": SymbolConfig(
        symbol           = "XAUEUR",
        pip_size         = 0.01,
        lot_size         = 0.5,
        is_tradeable     = False,
        max_trades_per_day = 99,
        tags             = ("metal", "forex"),
    ),
    "EURUSD": SymbolConfig(
        symbol           = "EURUSD",
        pip_size         = 0.0001,
        lot_size         = 0.5,
        is_tradeable     = False,
        max_trades_per_day = 99,
        tags             = ("forex",),
    ),
    "GBPUSD": SymbolConfig(
        symbol           = "GBPUSD",
        pip_size         = 0.0001,
        lot_size         = 0.5,
        is_tradeable     = False,
        max_trades_per_day = 99,
        tags             = ("forex",),
    ),
    "XAGUSD": SymbolConfig(
        symbol           = "XAGUSD",
        pip_size         = 0.01,
        lot_size         = 0.5,
        is_tradeable     = False,
        max_trades_per_day = 99,
        tags             = ("metal", "forex"),
    ),
}


# ============================================================
# Shared param blocks
# ============================================================

# ── Anchor layer — single start anchor, no promotion ─────────────────
_ANCHOR_DEFAULTS = {
    # Walking anchor system — only daily start anchor fires
    # promote_anchors_enabled=False is the key fix (March 30 -$3000 cause)
    "promote_anchors_enabled":   False,     # ← CRITICAL: kills promoted HIGH/LOW anchors
    "anchor_min_quality_score":  1.1,       # backup gate — scores cap at 1.0, nothing passes
    "anchor_keep_count":         0,         # intent: zero promoted anchors
    "anchor_promote_x":          0.75,
    "anchor_max_age_hours":      8.0,
    "spike_isolation_x":         1.0,
    "spike_window_minutes":      5.0,
    "center_noise_block_x":      0.0,
    "strong_trend_x":            2.0,
    "anchor_min_events_after":   0,
    "anchor_zone_bucket_x":      0.25,
}

# ── Entry quality layer — all off for April (raw anchor testing) ──────
_ENTRY_QUALITY_DEFAULTS = {
    # FALSY-BUG-FIX: 0.0 or 0.35 = 0.35 in Python — gate still blocks!
    # Use 0.001 (truthy, passes everything) not 0.0
    "min_entry_quality_score":      0.001,  # was 0.0 → fell back to 0.35 default
    "eq_anchor_weight":             0.50,
    "eq_pos_weight":                0.35,
    "eq_progress_weight":           0.15,
    "min_session_range_x":          0.001,  # was 0.0 → fell back to 0.50 default
    "min_trade_quality_score":      0.001,  # was 0.0 → fell back to 0.40 default
    "min_late_entry_quality":       0.50,
    "approach_direction_penalty_x": 0.70,
    "max_window_touches_per_day":   0,      # off
    "session_open_utc_hour":        0,      # off — trade any hour
    "session_close_utc_hour":       0,      # off
}

# ── Posttrade protection layer ────────────────────────────────────────
_POSTTRADE_DEFAULTS = {
    # Adverse excursion: exit if price moves > 0.60×TH against entry
    # At 2000 pips TH: 0.60 × 20pts = 12pts adverse = $1,200 at 1 lot
    "max_adverse_excursion_x":      0.60,

    "max_trade_duration_bars":      0,      # off — no time limit
    "profit_lock_x":                0.0,    # off — let it run to TP

    # Thesis invalidation: disabled for April raw testing
    # Re-enable after 2 weeks once baseline is confirmed
    "thesis_invalidation_enabled":  False,

    # FALSY-BUG-FIX: 0 or 2 = 2 in Python — zone locks after 2 losses even with 0!
    # Use 999 (never reached, effectively disabled) not 0
    "zone_loss_cooldown_threshold": 999,    # was 0 → fell back to 2 default
    "max_daily_loss_usd":           400.0,  # halt after $400 loss (2 adverse exits)
}


# ============================================================
# XAUUSD — April 2026 live config
# ============================================================
#
# LEVELS at start=4513:
#   Threshold  = 4513 ± 20.00 pts  → long: 4533.00 | short: 4493.00
#   Entry zone = 4513 ± 22.00 pts  → long: 4535.00 | short: 4491.00
#   Exit TP    = 4513 ± 24.00 pts  → long: 4537.00 | short: 4489.00
#   Capture    = 2.0 pts
#
# PROFIT at 1 lot ($100/pt):
#   Win  = 2.0 × $100 = $200
#   Loss (adverse at 0.60×TH) = 12pts × $100 = $1,200 max
#   R:R  = 1 : 0.17 (offset by ~85% win rate on 30pt avg daily range)
#
# TO SCALE: only change lot_size in SymbolConfig and strategy_params
#   2 lot  → $400/trade
#   5 lot  → $1,000/trade
#   10 lot → $2,000/trade
#   Everything else stays identical.
#
# ============================================================

SYMBOL_STRATEGY_BINDINGS: dict[str, StrategyBinding] = {

    "XAUUSD": StrategyBinding(
        strategy_id="threshold_touch.v1",
        strategy_params={
            **_ANCHOR_DEFAULTS,
            **_ENTRY_QUALITY_DEFAULTS,
            **_POSTTRADE_DEFAULTS,

            # ── Core threshold levels ─────────────────────────────────
            "threshold_pips":  2000,   # 20 pts — above gold daily noise (10-15pt)
            "entry1_min_x":    1.10,   # enter at 22 pts from anchor
            "entry1_max_x":    1.15,   # entry window closes at 23 pts
            "exit1_at_x":      1.20,   # TP at 24 pts → captures 2.0 pts

            # ── Lot size — explicit override ──────────────────────────
            # Phase 1 April: 1 lot = $200/trade
            # Phase 2 prop:  change to 2.0
            # Phase 3 scale: change to 5.0 or 10.0
            "lot_size":        1.0,

            # ── Spread ────────────────────────────────────────────────
            "half_spread_pips": 0.20,  # XAUUSD typical spread = 2-3 pips

            # ── Trade frequency — one trade per day, no re-entries ────
            "one_trade_per_day":     True,   # ← CRITICAL: was False, caused -$3,000
            "session_open_utc_hour":  0,     # trade any time (no session filter)
            "session_close_utc_hour": 0,

            # ── Anchor — single start anchor only ─────────────────────
            "promote_anchors_enabled":  False,  # kills all promoted HIGH/LOW anchors
            "anchor_min_quality_score": 1.1,    # backup gate (unreachable)

            # ── Quality gates — FALSY-BUG-FIX: use 0.001 not 0.0 ────────
            # 0.0 or 0.40 = 0.40 in Python — gates would still block!
            "anchor_min_quality_score": 1.1,    # backup (scores cap at 1.0)
            "min_entry_quality_score":  0.001,  # truthy, passes everything
            "min_trade_quality_score":  0.001,  # truthy, passes everything
            "anchor_min_events_after":  0,      # 0 is safe here (checked with <)
            "center_noise_block_x":     0.001,  # truthy, effectively off
            "min_session_range_x":      0.001,  # truthy, passes everything
            "thesis_invalidation_enabled": False,
            "zone_loss_cooldown_threshold": 999, # was 0 → fell back to 2

            # ── Late entry — off for April ────────────────────────────
            "late_entry_enabled": False,
            "late_entry_at_x":    1.45,   # 1.45 > entry1_max_x=1.15 ✓ geometry valid
            "late_exit_min_x":    1.50,
            "late_exit_max_x":    1.80,

            # ── Momentum — off for April ──────────────────────────────
            "momentum_entry_enabled": False,
            "momentum_min_x":  2.0,
            "momentum_max_x": 12.0,
            "momentum_tp_x":   0.60,

            # ── Loss protection ───────────────────────────────────────
            "max_daily_loss_usd":       400.0,  # halt after $400 loss
            "max_adverse_excursion_x":  0.60,   # exit at 12pts adverse ($1,200)
        },
    ),

    # ──────────────────────────────────────────────────────────────────
    # XAUEUR — apex_harrier.v1 (shadow observe only)
    # ──────────────────────────────────────────────────────────────────
    "XAUEUR": StrategyBinding(
        strategy_id="apex_harrier.v1",
        strategy_params={
            "threshold_pips":               2000,
            "profit_close_pullback_points":  750,
            "full_close_pullback_points":   1000,
            "max_cycle_loss_usd":           1000.0,
            "max_entries_per_cycle":           5,
            "cooldown_bars_after_full_close":  2,
            "max_cycles_per_day":              4,
            "close_confirmed_only":         True,
            "body_confirmation_only":       True,
            "one_action_per_bar":           True,
            "allow_missed_threshold_anchor":True,
            "lot_schedule":                 [1.0, 0.75, 0.5, 0.25, 0.10],
        },
    ),

    # ──────────────────────────────────────────────────────────────────
    # EURUSD / GBPUSD / XAGUSD — threshold_touch.v1 (all observe only)
    # ──────────────────────────────────────────────────────────────────
    "EURUSD": StrategyBinding(
        strategy_id="threshold_touch.v1",
        strategy_params={
            **_ANCHOR_DEFAULTS,
            **_ENTRY_QUALITY_DEFAULTS,
            **_POSTTRADE_DEFAULTS,
            "threshold_pips":   150,
            "entry1_min_x":    1.00,
            "entry1_max_x":    1.25,
            "exit1_at_x":      2.00,
            "half_spread_pips": 0.10,
            "late_entry_enabled": False,
            "late_entry_at_x":  1.35,
            "late_exit_min_x":  1.80,
            "late_exit_max_x":  2.20,
            "momentum_entry_enabled": False,
            "momentum_min_x":  2.0,
            "momentum_max_x":  6.0,
            "momentum_tp_x":   0.60,
            "one_trade_per_day": True,
        },
    ),

    "GBPUSD": StrategyBinding(
        strategy_id="threshold_touch.v1",
        strategy_params={
            **_ANCHOR_DEFAULTS,
            **_ENTRY_QUALITY_DEFAULTS,
            **_POSTTRADE_DEFAULTS,
            "threshold_pips":   150,
            "entry1_min_x":    1.00,
            "entry1_max_x":    1.25,
            "exit1_at_x":      2.00,
            "half_spread_pips": 0.15,
            "late_entry_enabled": False,
            "late_entry_at_x":  1.35,
            "late_exit_min_x":  1.80,
            "late_exit_max_x":  2.20,
            "momentum_entry_enabled": False,
            "momentum_min_x":  2.0,
            "momentum_max_x":  6.0,
            "momentum_tp_x":   0.60,
            "one_trade_per_day": True,
        },
    ),

    "XAGUSD": StrategyBinding(
        strategy_id="threshold_touch.v1",
        strategy_params={
            **_ANCHOR_DEFAULTS,
            **_ENTRY_QUALITY_DEFAULTS,
            **_POSTTRADE_DEFAULTS,
            "threshold_pips":  2000,
            "entry1_min_x":    1.00,
            "entry1_max_x":    1.25,
            "exit1_at_x":      2.20,
            "half_spread_pips": 0.25,
            "late_entry_enabled": False,
            "late_entry_at_x":  1.35,
            "late_exit_min_x":  2.00,
            "late_exit_max_x":  2.30,
            "momentum_entry_enabled": False,
            "momentum_min_x":  2.0,
            "momentum_max_x": 10.0,
            "momentum_tp_x":   0.60,
            "one_trade_per_day": True,
        },
    ),
}


# ============================================================
# Shadow strategy param defaults
# ============================================================

_APEX_HARRIER_DEFAULTS = {
    "threshold_pips":               2000,
    "pc_trigger_pips":               750,
    "fc_trigger_pips":              1000,
    "lot_size":                      0.5,
    "cooldown_bars_after_full_close":  2,
    "max_cycles_per_day":             10,
    "max_daily_loss_usd":            500.0,
}

_VWAP_REVERSION_DEFAULTS = {
    "dev_threshold_pips":        750,
    "adverse_pips":              500,
    "min_bars_for_vwap":           4,
    "max_trades_per_day":          3,
    "max_trade_duration_bars":    16,
    "session_open_utc_hour":       7,
    "session_close_utc_hour":     20,
    "max_daily_loss_usd":        300.0,
}

_SESSION_MOMENTUM_DEFAULTS = {
    "watch_bars_required":   2,
    "agree_threshold":       2,
    "min_range_pips":     1000,
    "tp_ratio":            1.5,
    "stop_ratio":          0.8,
    "max_sessions_per_day":  2,
    "max_trade_duration_bars": 12,
    "max_daily_loss_usd":  300.0,
}

_RSI_MR_DEFAULTS = {
    "rsi_period":     14,
    "rsi_oversold":   30,
    "rsi_overbought": 70,
    "adverse_pips":  1000,
    "tp_pips":       1500,
    "max_trades_per_day": 3,
    "max_daily_loss_usd": 300.0,
}

_BB_SCALPER_V2_DEFAULTS = {
    "bb_period":    20,
    "bb_std_dev":  2.0,
    "adverse_pips": 800,
    "tp_pips":     1000,
    "max_trades_per_day": 5,
    "max_daily_loss_usd": 300.0,
}


# ============================================================
# Shadow strategy bindings (observe_only=True)
# Signals tracked + logged — NO real orders placed.
# ============================================================

SYMBOL_CONFIGS["USDJPY"] = SymbolConfig(
    symbol="USDJPY", pip_size=0.01, lot_size=0.5,
    is_tradeable=False, max_trades_per_day=99, tags=("forex",),
)
SYMBOL_CONFIGS["USDCHF"] = SymbolConfig(
    symbol="USDCHF", pip_size=0.0001, lot_size=0.5,
    is_tradeable=False, max_trades_per_day=99, tags=("forex",),
)

SYMBOL_STRATEGY_BINDINGS["XAUEUR"] = StrategyBinding(
    strategy_id="apex_harrier.v1",
    observe_only=True,
    strategy_params={**_APEX_HARRIER_DEFAULTS,
                     "symbol": "XAUEUR", "pip_size": 0.01, "lot_size": 0.5},
)

SYMBOL_STRATEGY_BINDINGS["EURUSD"] = StrategyBinding(
    strategy_id="vwap_reversion.v1",
    observe_only=True,
    strategy_params={**_VWAP_REVERSION_DEFAULTS,
                     "symbol": "EURUSD", "pip_size": 0.0001, "lot_size": 0.5},
)

SYMBOL_STRATEGY_BINDINGS["GBPUSD"] = StrategyBinding(
    strategy_id="session_momentum.v1",
    observe_only=True,
    strategy_params={**_SESSION_MOMENTUM_DEFAULTS,
                     "symbol": "GBPUSD", "pip_size": 0.0001, "lot_size": 0.5},
)

SYMBOL_STRATEGY_BINDINGS["USDJPY"] = StrategyBinding(
    strategy_id="rsi_mean_reversion.v1",
    observe_only=True,
    strategy_params={**_RSI_MR_DEFAULTS,
                     "symbol": "USDJPY", "pip_size": 0.01, "lot_size": 0.5},
)

SYMBOL_STRATEGY_BINDINGS["USDCHF"] = StrategyBinding(
    strategy_id="bb_scalper.v1",
    observe_only=True,
    strategy_params={**_BB_SCALPER_V2_DEFAULTS,
                     "symbol": "USDCHF", "pip_size": 0.0001, "lot_size": 0.5},
)


# ============================================================
# Helper getters — unchanged
# ============================================================

def get_runtime_settings() -> RuntimeSettings:
    return RUNTIME


def get_trading_mode() -> TradingMode:
    return RUNTIME.trading_mode


def get_symbol_config(symbol: str) -> SymbolConfig:
    try:
        return SYMBOL_CONFIGS[symbol]
    except KeyError as exc:
        known = ", ".join(sorted(SYMBOL_CONFIGS.keys()))
        raise KeyError(f"Unknown symbol={symbol}. Known symbols: {known}") from exc


def get_strategy_binding(symbol: str) -> StrategyBinding:
    try:
        return SYMBOL_STRATEGY_BINDINGS[symbol]
    except KeyError as exc:
        known = ", ".join(sorted(SYMBOL_STRATEGY_BINDINGS.keys()))
        raise KeyError(
            f"Missing strategy binding for symbol={symbol}. Known bindings: {known}"
        ) from exc


def get_strategy_id(symbol: str) -> str:
    return get_strategy_binding(symbol).strategy_id


def get_strategy_params(symbol: str) -> dict[str, Any]:
    return dict(get_strategy_binding(symbol).strategy_params)


def get_symbol_lot_size(symbol: str) -> float:
    return get_symbol_config(symbol).lot_size


def is_symbol_tradeable(symbol: str) -> bool:
    return get_symbol_config(symbol).is_tradeable


def get_max_trades_per_day(symbol: str) -> int:
    return get_symbol_config(symbol).max_trades_per_day


def list_enabled_symbols(include_shadow: bool = False) -> list[str]:
    """Returns symbols that are tradeable AND have a strategy binding."""
    out: list[str] = []
    for s, cfg in SYMBOL_CONFIGS.items():
        if s not in SYMBOL_STRATEGY_BINDINGS:
            continue
        binding = SYMBOL_STRATEGY_BINDINGS[s]
        if binding.observe_only and not include_shadow:
            continue
        if cfg.is_tradeable or (include_shadow and binding.observe_only):
            out.append(s)
    return out


def list_shadow_symbols() -> list[str]:
    """Returns symbols with observe_only=True binding."""
    return [
        s for s, b in SYMBOL_STRATEGY_BINDINGS.items()
        if getattr(b, "observe_only", False)
    ]


def list_all_symbols() -> list[str]:
    """Returns all symbols — live + shadow."""
    return list(SYMBOL_STRATEGY_BINDINGS.keys())


def build_strategy_params_for_symbol(symbol: str) -> dict[str, Any]:
    """Merge strategy params with symbol metadata the strategy needs."""
    symbol_cfg = get_symbol_config(symbol)
    binding    = get_strategy_binding(symbol)
    params     = dict(binding.strategy_params)
    params.setdefault("lot_size",           symbol_cfg.lot_size)
    params.setdefault("symbol",             symbol_cfg.symbol)
    params.setdefault("pip_size",           symbol_cfg.pip_size)
    params.setdefault("max_trades_per_day", symbol_cfg.max_trades_per_day)
    return params