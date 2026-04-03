from __future__ import annotations

# =============================================================================
# SYMBOL INSPECTOR — reads contract spec from MT5 and computes all
# pip/lot/dollar values needed for the threshold strategy.
#
# Usage:
#   python symbol_inspector.py            # inspects XAUUSD
#   python symbol_inspector.py XAGUSD     # inspects silver
#   python symbol_inspector.py EURUSD GBPUSD XAUUSD XAGUSD
#
# Output shows exact backtest command to run for that symbol.
# =============================================================================

import sys

try:
    import MetaTrader5 as mt5
except ImportError:
    print("pip install MetaTrader5")
    sys.exit(1)


def inspect_symbol(symbol: str, account_size: float = 50_000,
                   sl_dollar: float = 200, rr: float = 3.0,
                   ref_price: float | None = None) -> dict:
    """
    Pull full contract spec from MT5 and compute strategy parameters.

    Returns dict with all values needed for backtest command.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return {"error": f"Symbol {symbol} not found in MT5. Add it to MarketWatch first."}

    tick = mt5.symbol_info_tick(symbol)

    # ── Raw MT5 contract spec ─────────────────────────────────────────────
    contract_size  = info.trade_contract_size   # oz, units, etc.
    point          = info.point                 # smallest price move
    digits         = info.digits                # decimal places
    tick_size      = info.trade_tick_size       # min price move
    tick_value     = info.trade_tick_value      # $ per tick per lot
    currency_profit= info.currency_profit       # e.g. USD
    volume_min     = info.volume_min
    volume_step    = info.volume_step

    # ── Current price ─────────────────────────────────────────────────────
    if tick:
        bid = float(tick.bid)
        ask = float(tick.ask)
        mid = (bid + ask) / 2.0
        spread_price = round(ask - bid, digits + 1)   # in price units
        spread_points = round(spread_price / point, 1) # in raw MT5 points
    elif ref_price:
        mid = ref_price
        bid = ask = mid
        spread_price = 0.0
        spread_points = 0
    else:
        mid = 0.0
        bid = ask = 0.0
        spread_price = 0.0
        spread_points = 0

    # ── Pip value per lot ─────────────────────────────────────────────────
    # dollar_per_price_unit = $ gained/lost per 1.0 price move per lot
    # Formula: tick_value / tick_size (ticks_per_unit × $ per tick)
    #
    # XAUUSD: 0.1 / 0.01 = $10/lot per $1 gold move  (MetaQuotes demo = 10x low)
    # XAGUSD: 0.5 / 0.001 = $500/lot per $1 silver move
    # EURUSD: 1.0 / 0.00001 = $100,000/lot per $1 EUR move
    #
    # NOTE: MetaQuotes demo has a known 10x undervalued tick_value for metals.
    # Real XAUUSD should be $100/lot not $10/lot. The backtest is still consistent
    # because lot_size auto-scales (4.0 lots × $10 = same as 0.4 lots × $100).
    pip_value_per_lot = round(tick_value / tick_size, 4)

    # Dollar per full pip (1.0 price unit move) — same as pip_value_per_lot here
    dollar_per_unit  = pip_value_per_lot

    # ── Strategy parameter derivation ─────────────────────────────────────
    # Target: threshold proportional to ~0.4% of current price
    # XAUUSD $4700 × 0.0043 ≈ 20 pips → same ratio for other symbols
    if mid > 0:
        threshold_pips = round(mid * 0.0043, 5)  # ~0.43% of price
        entry_offset   = round(threshold_pips * 1.25, 5)
        exit_offset    = round(threshold_pips * 2.0, 5)
        sl_pips        = round(entry_offset - threshold_pips, 5)
        tp_pips        = round(exit_offset - entry_offset, 5)
    else:
        threshold_pips = 0
        sl_pips = tp_pips = 0

    # Lot size from dollar risk
    tp_dollar = sl_dollar * rr
    if sl_pips > 0 and pip_value_per_lot > 0:
        lot_size = round(sl_dollar / (sl_pips * pip_value_per_lot), 2)
        # Round to nearest valid step
        if volume_step > 0:
            lot_size = round(round(lot_size / volume_step) * volume_step, 2)
        lot_size = max(lot_size, volume_min)
    else:
        lot_size = 0

    # Verify dollar amounts
    sl_verify = round(sl_pips * pip_value_per_lot * lot_size, 2) if lot_size else 0
    tp_verify = round(tp_pips * pip_value_per_lot * lot_size, 2) if lot_size else 0
    spread_cost = round(spread_points * pip_value_per_lot * lot_size / point * tick_size, 4) if lot_size else 0

    return {
        "symbol":          symbol,
        "price":           round(mid, digits),
        "bid":             round(bid, digits),
        "ask":             round(ask, digits),
        "digits":          digits,
        "point":           point,
        "contract_size":   contract_size,
        "currency_profit": currency_profit,
        "tick_size":       tick_size,
        "tick_value":      tick_value,
        "pip_value_per_lot": pip_value_per_lot,
        "dollar_per_unit": dollar_per_unit,
        "spread_points":   spread_points,
        "spread_price":    locals().get("spread_price", 0),
        "volume_min":      volume_min,
        "volume_step":     volume_step,
        # Strategy params
        "threshold_pips":  threshold_pips,
        "entry_offset":    entry_offset,
        "exit_offset":     exit_offset,
        "sl_pips":         sl_pips,
        "tp_pips":         tp_pips,
        "sl_dollar":       sl_dollar,
        "tp_dollar":       tp_dollar,
        "lot_size":        lot_size,
        "sl_verify":       sl_verify,
        "tp_verify":       tp_verify,
        "account_size":    account_size,
    }


def print_report(d: dict):
    if "error" in d:
        print(f"\n❌ {d['error']}\n")
        return

    sym    = d["symbol"]
    digits = d["digits"]

    print(f"\n{'='*65}")
    print(f"  SYMBOL INSPECTOR — {sym}")
    print(f"{'='*65}")

    print(f"\n{'─'*65}")
    print(f"  MT5 CONTRACT SPEC")
    print(f"{'─'*65}")
    print(f"  Current price    : {d['bid']:.{digits}f} / {d['ask']:.{digits}f}  (bid/ask)")
    print(f"  Mid              : {d['price']:.{digits}f}")
    print(f"  Contract size    : {d['contract_size']:,}  (units/lot)")
    print(f"  Point (1 tick)   : {d['point']}")
    print(f"  Digits           : {d['digits']}")
    print(f"  Tick size        : {d['tick_size']}")
    print(f"  Tick value       : ${d['tick_value']}  (per lot per tick)")
    print(f"  Profit currency  : {d['currency_profit']}")
    print(f"  Min volume       : {d['volume_min']} lot")
    print(f"  Volume step      : {d['volume_step']} lot")

    print(f"\n{'─'*65}")
    print(f"  PIP / DOLLAR VALUES")
    print(f"{'─'*65}")
    spread_cost = round(d.get('spread_price', 0) * d['pip_value_per_lot'] * d['lot_size'], 2)
    print(f"  $ per price unit/lot: ${d['pip_value_per_lot']:,.1f}")
    print(f"  Spread (price)  : {d.get('spread_price', 0):.{digits}f}  ({d['spread_points']:.0f} points)")
    print(f"  Spread cost/trade: ${spread_cost:.2f}  (at {d['lot_size']} lot)")
    if 'XAUUSD' in d['symbol'] or 'XAU' in d['symbol']:
        print(f"  ⚠️  MetaQuotes demo: tick_value 10x low → real pip_value ~${d['pip_value_per_lot']*10:,.0f}/lot")

    print(f"\n{'─'*65}")
    print(f"  STRATEGY PARAMETERS  (SL=${d['sl_dollar']}, RR=1:{int(d['tp_dollar']/d['sl_dollar'])}, account=${d['account_size']:,})")
    print(f"{'─'*65}")
    print(f"  Threshold (1.0×) : ±{d['threshold_pips']}  → S±{d['threshold_pips']:.{digits}f}")
    print(f"  Entry     (1.25×): ±{d['entry_offset']}  → S±{d['entry_offset']:.{digits}f}")
    print(f"  TP        (2.0×) : ±{d['exit_offset']}  → S±{d['exit_offset']:.{digits}f}")
    print(f"  SL pips          : {d['sl_pips']}  (entry→SL)")
    print(f"  TP pips          : {d['tp_pips']}  (entry→TP)")
    print(f"  Lot size         : {d['lot_size']}")
    print(f"  SL dollar verify : ${d['sl_verify']}  (target=${d['sl_dollar']})")
    print(f"  TP dollar verify : ${d['tp_verify']}  (target=${d['tp_dollar']})")

    # Backtest command
    sl_t  = d['sl_dollar']
    tp_t  = d['tp_dollar']
    sl_p  = d['sl_pips']
    pv    = d['pip_value_per_lot']
    sp    = d['spread_points']
    thr   = d['threshold_pips']
    acc   = d['account_size']

    print(f"\n{'─'*65}")
    print(f"  BACKTEST COMMAND")
    print(f"{'─'*65}")
    print(
        f"  python backtest.py \\\n"
        f"    --capital {acc:.0f} \\\n"
        f"    --sl-target {sl_t:.0f} --tp-target {tp_t:.0f} \\\n"
        f"    --sl-pips {sl_p} \\\n"
        f"    --daily-loss {sl_t:.0f} --daily-profit {tp_t:.0f} \\\n"
        f"    --close-confirm --trend-filter --months 3 \\\n"
        f"    --symbol {sym} \\\n"
        f"    --pip-value {pv} \\\n"
        f"    --spread {sp} \\\n"
        f"    --threshold-pips {thr}"
    )
    print(f"\n{'='*65}\n")


def main():
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["XAUUSD"]

    if not mt5.initialize():
        print(f"❌ MT5 init failed: {mt5.last_error()}")
        print("Make sure MT5 is running and logged in.")
        sys.exit(1)

    print(f"\n[Inspector] MT5 connected | Checking {len(symbols)} symbol(s)...")

    for sym in symbols:
        # Ensure symbol is visible in MarketWatch
        mt5.symbol_select(sym, True)
        result = inspect_symbol(sym)
        print_report(result)

    mt5.shutdown()


if __name__ == "__main__":
    main()