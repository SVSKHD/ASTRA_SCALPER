from __future__ import annotations

# =============================================================================
# EXECUTOR — MT5 order placement
#   Fill policy : ORDER_FILLING_FOK (Fill or Kill)
#   Retries     : up to MAX_RETRIES on transient errors
#   Retcode 0   : treated as success (MetaQuotes demo quirk)
# =============================================================================

import time
import logging
from datetime import datetime, timezone

import MetaTrader5 as mt5

from config import cfg, StrategyConfig
from trade_signal import Signal

log = logging.getLogger("executor")

RETCODE_OK    = {0, 10009}

RETCODE_RETRY = {
    10004,  # Requote
    10006,  # Request rejected
    10007,  # Request canceled by trader
    10010,  # Only part of request completed
    10012,  # Request canceled by timeout
    10013,  # Invalid request
    10018,  # Market is closed
    10035,  # Busy
}

MAX_RETRIES   = 3
RETRY_DELAY_S = 1.0


def _fill_price(symbol: str, direction: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return tick.ask if direction == "LONG" else tick.bid


def _build_request(
    signal: Signal,
    strategy_cfg: StrategyConfig,
    price: float,
) -> dict:
    """
    Build MT5 DEAL request.
    type_filling = ORDER_FILLING_FOK:
        Full volume must fill at price within deviation, else cancel.
        No partial fills. Broker must accept the entire lot at once.
    """
    order_type = mt5.ORDER_TYPE_BUY if signal.direction == "LONG" else mt5.ORDER_TYPE_SELL
    return {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       strategy_cfg.symbol,
        "volume":       strategy_cfg.lot_size,
        "type":         order_type,
        "price":        price,
        "sl":           signal.sl_price,
        "tp":           signal.tp_price,
        "deviation":    strategy_cfg.deviation_points,
        "magic":        strategy_cfg.magic_number,
        "comment":      f"{strategy_cfg.order_comment}_{signal.direction}",
        "type_time":    mt5.ORDER_TIME_DAY,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }


def place_order(
    signal:       Signal,
    strategy_cfg: StrategyConfig = cfg,
    max_retries:  int   = MAX_RETRIES,
    retry_delay:  float = RETRY_DELAY_S,
) -> dict:
    """
    Place market order with FOK + retry.
    Refreshes price on each attempt.
    Returns success dict with attempts count.
    """
    symbol = strategy_cfg.symbol

    for attempt in range(1, max_retries + 1):
        price = _fill_price(symbol, signal.direction)
        if price is None:
            err = f"No tick on attempt {attempt}"
            log.warning(f"[{symbol}] {err}")
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            return {"retcode": -1, "error": err, "success": False, "attempts": attempt}

        request = _build_request(signal, strategy_cfg, price)
        log.info(
            f"[{symbol}] ORDER {attempt}/{max_retries} | "
            f"{signal.direction} | price={price:.2f} | "
            f"sl={signal.sl_price:.2f} | tp={signal.tp_price:.2f} | FOK"
        )

        result = mt5.order_send(request)
        if result is None:
            err = f"order_send None: {mt5.last_error()}"
            log.warning(f"[{symbol}] attempt {attempt}: {err}")
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            return {"retcode": -1, "error": err, "success": False, "attempts": attempt}

        if result.retcode in RETCODE_OK:
            log.info(
                f"[{symbol}] ✅ FILLED | ticket={result.order} | "
                f"fill={result.price:.2f} | attempt={attempt}"
            )
            return {
                "retcode":    result.retcode,
                "order":      result.order,
                "fill_price": result.price,
                "volume":     result.volume,
                "comment":    result.comment,
                "success":    True,
                "attempts":   attempt,
            }

        if result.retcode in RETCODE_RETRY and attempt < max_retries:
            log.warning(
                f"[{symbol}] ⚠️ retcode={result.retcode} ({result.comment}) | "
                f"retry {attempt}/{max_retries} in {retry_delay}s"
            )
            time.sleep(retry_delay)
            continue

        log.error(
            f"[{symbol}] ❌ FAILED | retcode={result.retcode} | "
            f"{result.comment} | attempts={attempt}"
        )
        return {
            "retcode":  result.retcode,
            "order":    getattr(result, "order", 0),
            "comment":  result.comment,
            "success":  False,
            "attempts": attempt,
            "error":    f"retcode={result.retcode}: {result.comment}",
        }

    return {"retcode": -1, "error": "Retries exhausted", "success": False, "attempts": max_retries}


def close_all_by_magic(
    strategy_cfg: StrategyConfig = cfg,
    max_retries:  int   = MAX_RETRIES,
    retry_delay:  float = RETRY_DELAY_S,
) -> list[dict]:
    """FOK close all open positions by magic. With retries."""
    symbol    = strategy_cfg.symbol
    positions = mt5.positions_get(symbol=symbol) or []
    results   = []

    for pos in positions:
        if pos.magic != strategy_cfg.magic_number:
            continue
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

        for attempt in range(1, max_retries + 1):
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                time.sleep(retry_delay)
                continue
            close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "position":     pos.ticket,
                "price":        close_price,
                "deviation":    strategy_cfg.deviation_points,
                "magic":        strategy_cfg.magic_number,
                "comment":      "force_close_eod",
                "type_filling": mt5.ORDER_FILLING_FOK,
            }
            result = mt5.order_send(req)
            if result and result.retcode in RETCODE_OK:
                results.append({"ticket": pos.ticket, "retcode": result.retcode,
                                 "success": True, "attempts": attempt})
                break
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                results.append({"ticket": pos.ticket,
                                 "retcode": result.retcode if result else -1,
                                 "success": False, "attempts": attempt})
    return results


def get_open_positions(strategy_cfg: StrategyConfig = cfg) -> list:
    return [p for p in (mt5.positions_get(symbol=strategy_cfg.symbol) or [])
            if p.magic == strategy_cfg.magic_number]


def calculate_open_pnl(strategy_cfg: StrategyConfig = cfg) -> float:
    return sum(p.profit for p in get_open_positions(strategy_cfg))


def calculate_day_pnl(strategy_cfg: StrategyConfig = cfg) -> float:
    now       = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    deals     = mt5.history_deals_get(day_start, now) or []
    return sum(d.profit for d in deals
               if d.magic == strategy_cfg.magic_number and d.symbol == strategy_cfg.symbol)
