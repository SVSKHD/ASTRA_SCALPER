"""
run_start_price.py — standalone launcher for pricing/start_price.py

Place in: ASTRA_HAWK_SCALPER_20265/  (root, same level as pricing/)
Run:      python run_start_price.py
Or:       python run_start_price.py XAUUSD XAUEUR

Handles both versions of start_price.py:
  - Old: uses `from settings import ...` (non-relative)
  - New: uses `from .config import ...` (relative, needs package context)
"""
from __future__ import annotations

import sys
import os
import types
import threading
import time

ROOT_DIR   = os.path.dirname(os.path.abspath(__file__))
PRICING_DIR = os.path.join(ROOT_DIR, "pricing")

# ── DEFAULT SYMBOLS ───────────────────────────────────────────────────────────
DEFAULT_SYMBOLS = ["XAUUSD"]

# ── STUB HELPERS ─────────────────────────────────────────────────────────────

def _noop(*args, **kwargs):
    pass

def _make_notify_module():
    m = types.ModuleType("pricing.notify")
    m.send_runner_card = _noop
    m.embed_field      = lambda *a, **kw: {}
    return m

def _make_config_module(symbols):
    m = types.ModuleType("pricing.config")
    m.list_enabled_symbols = lambda: symbols
    m.list_shadow_symbols  = lambda: []
    m.get_tradeable_symbols = lambda: symbols
    return m


def _inject_stubs(symbols):
    """
    Register all stub modules so that no matter how start_price.py imports
    (relative or absolute, via Revamp or pricing package), it resolves.
    """
    config_mod = _make_config_module(symbols)
    notify_mod = _make_notify_module()

    # --- Revamp stubs (absolute import style) --------------------------------
    revamp = types.ModuleType("Revamp")
    revamp.config = config_mod
    revamp.notify = notify_mod
    sys.modules.setdefault("Revamp",        revamp)
    sys.modules.setdefault("Revamp.config", config_mod)
    sys.modules.setdefault("Revamp.notify", notify_mod)

    # --- pricing package stub (relative import style) -------------------------
    # When start_price.py does `from .config import list_enabled_symbols`,
    # Python looks for pricing.config. We pre-register pricing as a package
    # with its sub-modules already set.
    pricing_pkg = types.ModuleType("pricing")
    pricing_pkg.__path__    = [PRICING_DIR]   # marks it as a package
    pricing_pkg.__package__ = "pricing"
    pricing_pkg.config      = config_mod
    pricing_pkg.notify      = notify_mod

    # Load the real submodules from disk into the pricing namespace
    # so relative imports inside start_price.py work
    _load_real_submodule("settings", pricing_pkg)
    _load_real_submodule("clock",    pricing_pkg)
    _load_real_submodule("storage",  pricing_pkg)

    sys.modules.setdefault("pricing",        pricing_pkg)
    sys.modules.setdefault("pricing.config", config_mod)
    sys.modules.setdefault("pricing.notify", notify_mod)

    # Also expose flat names so `from settings import ...` works
    sys.path.insert(0, PRICING_DIR)
    sys.path.insert(0, ROOT_DIR)


def _load_real_submodule(name: str, pkg):
    """Load pricing/<name>.py into sys.modules as pricing.<name>."""
    path = os.path.join(PRICING_DIR, f"{name}.py")
    if not os.path.exists(path):
        return
    import importlib.util
    full_name = f"pricing.{name}"
    if full_name in sys.modules:
        setattr(pkg, name, sys.modules[full_name])
        return
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None:
        return
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "pricing"
    sys.modules[full_name] = mod
    sys.modules[name]      = mod   # flat alias too
    try:
        spec.loader.exec_module(mod)
        setattr(pkg, name, mod)
    except Exception as e:
        print(f"  [warn] Could not load pricing/{name}.py: {e}")
        del sys.modules[full_name]
        sys.modules.pop(name, None)


def _load_start_price_loop():
    """
    Import run_start_price_loop from pricing/start_price.py.
    Tries both as a package member and as a flat import.
    """
    path = os.path.join(PRICING_DIR, "start_price.py")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Not found: {path}")

    import importlib.util
    # Load as pricing.start_price so relative imports resolve
    spec = importlib.util.spec_from_file_location(
        "pricing.start_price", path,
        submodule_search_locations=[]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "pricing"
    sys.modules["pricing.start_price"] = mod
    sys.modules["start_price"]         = mod
    spec.loader.exec_module(mod)
    return mod.run_start_price_loop


def main():
    symbols = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SYMBOLS

    print("=" * 50)
    print("  START PRICE RUNNER")
    print("=" * 50)
    print(f"  Symbols   : {symbols}")
    print(f"  Writes to : data/start_price/<symbol>.json")
    print(f"  Day files : data/XAUUSD/<date>.json")
    print(f"  Lock at   : 00:00 UTC  (lock_hhmm_mt5)")
    print()

    # Inject all stubs before any import from pricing
    _inject_stubs(symbols)

    # Load the real loop function
    try:
        run_loop = _load_start_price_loop()
    except Exception as e:
        print(f"❌ Failed to load pricing/start_price.py: {e}")
        print()
        print("Check that pricing/start_price.py exists and is intact.")
        sys.exit(1)

    # Load settings
    try:
        from settings import PriceSettings
        cfg = PriceSettings()
    except Exception as e:
        print(f"❌ Failed to load PriceSettings: {e}")
        sys.exit(1)

    print(f"  base_dir  : {cfg.base_dir}")
    print(f"  poll_s    : {cfg.poll_seconds}")
    print(f"  lock_hhmm : {cfg.lock_hhmm_mt5}")
    print()

    # Start one thread per symbol
    for symbol in symbols:
        t = threading.Thread(
            target=run_loop,
            args=(symbol, cfg),
            name=f"start_price_{symbol}",
            daemon=True,
        )
        t.start()
        print(f"  ✅ {symbol} thread started")

    print()
    print("Running... Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")


if __name__ == "__main__":
    main()