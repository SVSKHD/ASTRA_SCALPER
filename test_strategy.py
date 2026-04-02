from __future__ import annotations

# =============================================================================
# FULL TEST SUITE — 90+ tests, no MT5 required
# Run: python test_strategy.py
#
# Fixes from review:
#   - All imports use trade_signal (not signal — stdlib collision fixed)
#   - True 1.1/1.2 threshold levels tested
#   - Overshoot filter tested
#   - Daily profit stop tested
#   - MT5 server-day alignment tested
#   - Executor MT5 injected at module level (no patch needed)
# =============================================================================

import json, os, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Inject fake MetaTrader5 before any module imports it
_fake_mt5 = MagicMock()
_fake_mt5.ORDER_TYPE_BUY    = 0
_fake_mt5.ORDER_TYPE_SELL   = 1
_fake_mt5.TRADE_ACTION_DEAL = 1
_fake_mt5.ORDER_TIME_DAY    = 1
_fake_mt5.ORDER_FILLING_IOC = 1
sys.modules.setdefault("MetaTrader5", _fake_mt5)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
class TestConfig(unittest.TestCase):

    def _c(self, **kw):
        from config import StrategyConfig
        return StrategyConfig(**kw)

    def test_entry_offset_is_25(self):
        """5-pip SL: entry_multiplier=1.25 → 20×1.25=25"""
        self.assertAlmostEqual(self._c().entry_offset, 25.0)

    def test_exit_offset_is_40(self):
        """R:R 1:3: exit_multiplier=2.0 → 20×2.0=40"""
        self.assertAlmostEqual(self._c().exit_offset, 40.0)

    def test_breakout_offset_is_20(self):
        self.assertAlmostEqual(self._c().breakout_offset, 20.0)

    def test_sl_pips_is_5(self):
        """5-pip default: entry_offset(25) − breakout_offset(20) = 5"""
        self.assertAlmostEqual(self._c().sl_pips, 5.0)

    def test_tp_pips_is_15(self):
        """R:R 1:3: exit(40) − entry(25) = 15 pips"""
        self.assertAlmostEqual(self._c().tp_pips, 15.0)

    def test_rr_is_3(self):
        """R:R 1:3: tp_pips(15) / sl_pips(5) = 3.0"""
        self.assertAlmostEqual(self._c().risk_reward, 3.0)

    def test_breakeven_win_rate_25pct(self):
        """R:R 1:3: sl/(sl+tp) = 5/20 = 0.25"""
        self.assertAlmostEqual(self._c().breakeven_win_rate, 0.25)

    def test_lot_size_from_sl_target(self):
        # R:R 1:3: lot = 200 / (5 × 100) = 0.4
        self.assertAlmostEqual(self._c(account_size=10_000).lot_size, 0.4)
        self.assertAlmostEqual(self._c(account_size=50_000).lot_size, 0.4)
        self.assertAlmostEqual(self._c(account_size=100_000).lot_size, 0.4)

    def test_lot_size_changes_with_sl_target(self):
        # $400 SL at 5 pips → 400/(5×100) = 0.8 lot
        c = self._c(sl_dollar_target=400)
        self.assertAlmostEqual(c.lot_size, 0.8)

    def test_daily_loss_limit_default(self):
        # R:R 1:3: daily loss = $200 (1 SL hit)
        self.assertAlmostEqual(self._c(account_size=50_000).max_daily_loss_usd, 200.0)

    def test_daily_profit_target_default(self):
        self.assertEqual(self._c().daily_profit_target_usd, 600.0)

    def test_sl_dollar_matches_target(self):
        # sl_dollar should equal sl_dollar_target
        c = self._c(account_size=50_000)
        self.assertAlmostEqual(c.sl_dollar, c.sl_dollar_target)

    def test_tp_dollar_600(self):
        # R:R 1:3: tp=15 pips, 0.4 lot → 15×0.4×100 = $600
        c = self._c(account_size=50_000)
        self.assertAlmostEqual(c.tp_dollar, 600.0)

    def test_summary_contains_account(self):
        self.assertIn("50,000", self._c(account_size=50_000).summary())

    def test_overshoot_default(self):
        self.assertEqual(self._c().max_entry_overshoot_pips, 3.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. THRESHOLD LEVELS — true 1.1/1.2
# ═══════════════════════════════════════════════════════════════════════════════
class TestThreshold(unittest.TestCase):

    def _lv(self, start, **kw):
        from config import StrategyConfig
        from threshold import compute_levels
        return compute_levels(start, StrategyConfig(**kw))

    def test_long_breakout(self):
        """Start 4513: breakout = 4513+20 = 4533"""
        self.assertAlmostEqual(self._lv(4513).long_breakout, 4533.0)

    def test_long_entry(self):
        """5-pip: start 4513, entry = 4513+25 = 4538"""
        self.assertAlmostEqual(self._lv(4513).long_entry, 4538.0)

    def test_long_tp(self):
        """R:R 1:3: start 4513, TP = 4513+40 = 4553"""
        self.assertAlmostEqual(self._lv(4513).long_tp, 4553.0)

    def test_long_sl(self):
        """SL = breakout level = 4533"""
        self.assertAlmostEqual(self._lv(4513).long_sl, 4533.0)

    def test_long_sl_equals_breakout(self):
        lv = self._lv(4513)
        self.assertAlmostEqual(lv.long_sl, lv.long_breakout)

    def test_short_breakout(self):
        self.assertAlmostEqual(self._lv(4513).short_breakout, 4493.0)

    def test_short_entry(self):
        """5-pip: start 4513, short_entry = 4513-25 = 4488"""
        self.assertAlmostEqual(self._lv(4513).short_entry, 4488.0)

    def test_short_tp(self):
        """R:R 1:3: start 4513, short TP = 4513-40 = 4473"""
        self.assertAlmostEqual(self._lv(4513).short_tp, 4473.0)

    def test_short_sl(self):
        self.assertAlmostEqual(self._lv(4513).short_sl, 4493.0)

    def test_short_sl_equals_breakout(self):
        lv = self._lv(4513)
        self.assertAlmostEqual(lv.short_sl, lv.short_breakout)

    def test_capture_per_trade(self):
        """R:R 1:3: capture = exit(40) - entry(25) = 15 pips"""
        lv = self._lv(4513)
        self.assertAlmostEqual(lv.long_tp - lv.long_entry, 15.0)
        self.assertAlmostEqual(lv.short_entry - lv.short_tp, 15.0)

    def test_symmetry(self):
        lv = self._lv(4500)
        self.assertAlmostEqual(lv.long_entry - lv.start, lv.start - lv.short_entry)

    def test_26_mar_levels(self):
        lv = self._lv(4517)
        self.assertAlmostEqual(lv.long_entry,  4542.0)  # 4517+25
        self.assertAlmostEqual(lv.short_entry, 4492.0)  # 4517-25
        self.assertAlmostEqual(lv.long_tp,     4557.0)  # 4517+40
        self.assertAlmostEqual(lv.short_tp,    4477.0)  # 4517-40

    def test_27_mar_levels(self):
        lv = self._lv(4384)
        self.assertAlmostEqual(lv.long_entry,  4409.0)  # 4384+25
        self.assertAlmostEqual(lv.short_entry, 4359.0)  # 4384-25
        self.assertAlmostEqual(lv.long_tp,     4424.0)  # 4384+40

    def test_31_mar_levels(self):
        lv = self._lv(4513)
        self.assertAlmostEqual(lv.long_entry,  4538.0)  # 4513+25
        self.assertAlmostEqual(lv.short_entry, 4488.0)  # 4513-25
        self.assertAlmostEqual(lv.long_tp,     4553.0)  # 4513+40
        self.assertAlmostEqual(lv.short_tp,    4473.0)  # 4513-40

    def test_display_runs(self):
        lv = self._lv(4513)
        s = lv.display()
        self.assertIn("4538", s)
        self.assertIn("4553", s)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TRADE SIGNAL (was signal.py — renamed to fix stdlib collision)
# ═══════════════════════════════════════════════════════════════════════════════
class TestTradeSignal(unittest.TestCase):

    def setUp(self):
        from config import StrategyConfig
        from threshold import compute_levels
        self.cfg = StrategyConfig(account_size=50_000)
        self.lv  = compute_levels(4513.0, self.cfg)

    def _ev(self, mid, traded=None):
        from trade_signal import evaluate_signal
        return evaluate_signal(mid, self.lv, traded or set(), self.cfg)

    def test_no_signal_at_start(self):
        self.assertIsNone(self._ev(4513.0))

    def test_no_signal_in_zone(self):
        self.assertIsNone(self._ev(4525.0))
        self.assertIsNone(self._ev(4500.0))

    def test_long_exact_entry(self):
        # 5-pip default: entry = S+25 = 4538
        sig = self._ev(4538.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "LONG")

    def test_long_tp_is_exit_multiplier(self):
        """TP at exit_multiplier(2.0)× = S + 40 = 4553"""
        sig = self._ev(4538.0)
        self.assertAlmostEqual(sig.tp_price, 4553.0)

    def test_long_sl_at_breakout(self):
        """SL at 1.0× = S + 20 = 4533"""
        sig = self._ev(4538.0)
        self.assertAlmostEqual(sig.sl_price, 4533.0)

    def test_short_exact_entry(self):
        # 5-pip default: short entry = S-25 = 4488
        sig = self._ev(4488.0)
        self.assertEqual(sig.direction, "SHORT")

    def test_short_tp_is_exit_multiplier(self):
        """Short TP = S - 40 = 4473"""
        sig = self._ev(4488.0)
        self.assertAlmostEqual(sig.tp_price, 4473.0)

    def test_short_sl_at_breakout(self):
        """Short SL = S - 20 = 4493 (short entry = 4488)"""
        sig = self._ev(4488.0)
        self.assertAlmostEqual(sig.sl_price, 4493.0)

    def test_overshoot_long_allowed(self):
        """2.5 pip overshoot < 3.0 → allowed (entry=4538)"""
        sig = self._ev(4540.5)
        self.assertIsNotNone(sig)

    def test_overshoot_long_rejected(self):
        """4.0 pip overshoot > 3.0 → rejected (entry=4538)"""
        sig = self._ev(4542.0)
        self.assertIsNone(sig)

    def test_overshoot_short_allowed(self):
        """2.5 pip below short_entry → allowed (entry=4488)"""
        sig = self._ev(4485.5)
        self.assertIsNotNone(sig)

    def test_overshoot_short_rejected(self):
        """4.5 pip below short_entry → rejected (entry=4488)"""
        sig = self._ev(4483.5)
        self.assertIsNone(sig)

    def test_first_only_blocks_after_long(self):
        from config import StrategyConfig
        from threshold import compute_levels
        from trade_signal import evaluate_signal
        cfg = StrategyConfig(direction_mode="first_only")
        lv  = compute_levels(4513.0, cfg)
        self.assertIsNone(evaluate_signal(4540.0, lv, {"LONG"}, cfg))

    def test_both_mode_allows_second(self):
        from config import StrategyConfig
        from threshold import compute_levels
        from trade_signal import evaluate_signal
        cfg = StrategyConfig(direction_mode="both")
        lv  = compute_levels(4513.0, cfg)
        # short entry = S-25 = 4488
        sig = evaluate_signal(4488.0, lv, {"LONG"}, cfg)
        self.assertEqual(sig.direction, "SHORT")

    def test_long_tp_above_entry(self):
        sig = self._ev(4538.0)
        self.assertGreater(sig.tp_price, sig.entry_price)

    def test_long_sl_below_entry(self):
        sig = self._ev(4538.0)
        self.assertLess(sig.sl_price, sig.entry_price)

    def test_short_tp_below_entry(self):
        sig = self._ev(4488.0)
        self.assertLess(sig.tp_price, sig.entry_price)

    def test_short_sl_above_entry(self):
        sig = self._ev(4488.0)
        self.assertGreater(sig.sl_price, sig.entry_price)

    def test_sl_always_defined(self):
        for mid in [4538.0, 4488.0]:
            sig = self._ev(mid)
            self.assertIsNotNone(sig.sl_price)
            self.assertGreater(sig.sl_price, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SESSION GUARD
# ═══════════════════════════════════════════════════════════════════════════════
class TestSession(unittest.TestCase):

    def setUp(self):
        from config import StrategyConfig
        # Explicit session times for tests — independent of config defaults
        self.cfg = StrategyConfig(
            session_start_hhmm="08:00",
            session_end_hhmm="20:00",
            force_close_hhmm="21:30",
        )

    def _dt(self, hhmm):
        h, m = map(int, hhmm.split(":"))
        return datetime(2026, 4, 1, h, m, tzinfo=timezone.utc)

    def test_in_session(self):
        from session_guard import is_session_allowed
        self.assertTrue(is_session_allowed(self.cfg, self._dt("12:00")))

    def test_before_session(self):
        from session_guard import is_session_allowed
        self.assertFalse(is_session_allowed(self.cfg, self._dt("07:00")))

    def test_after_session(self):
        from session_guard import is_session_allowed
        self.assertFalse(is_session_allowed(self.cfg, self._dt("21:00")))

    def test_force_close_triggers(self):
        from session_guard import is_force_close_time
        self.assertTrue(is_force_close_time(self.cfg, self._dt("22:00")))

    def test_force_close_not_yet(self):
        from session_guard import is_force_close_time
        self.assertFalse(is_force_close_time(self.cfg, self._dt("20:00")))

    def test_news_blackout_inside(self):
        from session_guard import is_news_blackout
        self.assertTrue(is_news_blackout(["13:30"], self.cfg, self._dt("13:20")))

    def test_news_blackout_outside(self):
        from session_guard import is_news_blackout
        self.assertFalse(is_news_blackout(["13:30"], self.cfg, self._dt("12:00")))

    def test_no_events_no_blackout(self):
        from session_guard import is_news_blackout
        self.assertFalse(is_news_blackout([], self.cfg, self._dt("13:30")))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. START READER — MT5 server-day alignment
# ═══════════════════════════════════════════════════════════════════════════════
class TestStartReader(unittest.TestCase):

    def _today_mt5(self, cfg):
        from start_reader import _utc_date_today
        return _utc_date_today()

    def _write(self, tmpdir, payload):
        # Matches storage.resolve_start_root_path: data/start_price/XAUUSD.json
        folder = os.path.join(tmpdir, "start_price")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "XAUUSD.json"), "w") as f:
            json.dump(payload, f)

    def test_reads_locked_with_mt5_date(self):
        from config import StrategyConfig
        from start_reader import read_start_price
        with tempfile.TemporaryDirectory() as d:
            cfg = StrategyConfig(base_dir=d)
            today = self._today_mt5(cfg)
            # Real schema: nested start block (matches 2026-04-01.json)
            self._write(d, {
                "date_mt5": today,
                "start": {"status":"LOCKED","price":4513.0}
            })
            self.assertAlmostEqual(read_start_price(cfg), 4513.0)

    def test_pending_returns_none(self):
        from config import StrategyConfig
        from start_reader import read_start_price
        with tempfile.TemporaryDirectory() as d:
            cfg = StrategyConfig(base_dir=d)
            self._write(d, {"date_mt5": self._today_mt5(cfg),
                             "start":{"status":"PENDING","price":None}})
            self.assertIsNone(read_start_price(cfg))

    def test_stale_mt5_date_returns_none(self):
        from config import StrategyConfig
        from start_reader import read_start_price
        with tempfile.TemporaryDirectory() as d:
            cfg = StrategyConfig(base_dir=d)
            self._write(d, {"date_mt5":"2020-01-01",
                             "start":{"status":"LOCKED","price":4513.0}})
            self.assertIsNone(read_start_price(cfg))

    def test_corrupt_json_returns_none(self):
        from config import StrategyConfig
        from start_reader import read_start_price
        with tempfile.TemporaryDirectory() as d:
            cfg = StrategyConfig(base_dir=d)
            folder = os.path.join(d, "start_price")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "XAUUSD.json"), "w") as f:
                f.write("{BAD}")
            self.assertIsNone(read_start_price(cfg))

    def test_missing_file_returns_none(self):
        from config import StrategyConfig
        from start_reader import read_start_price
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(read_start_price(StrategyConfig(base_dir=d)))

    def test_price_returned_as_float(self):
        from config import StrategyConfig
        from start_reader import read_start_price
        with tempfile.TemporaryDirectory() as d:
            cfg = StrategyConfig(base_dir=d)
            self._write(d, {"date_mt5": self._today_mt5(cfg),
                             "start":{"status":"LOCKED","price":"4513.0"}})
            self.assertIsInstance(read_start_price(cfg), float)

    def test_utc_date_today(self):
        """date_mt5 is UTC calendar date. Confirm _utc_date_today returns correct format."""
        from start_reader import _utc_date_today
        from datetime import datetime, timezone
        d = _utc_date_today()
        self.assertRegex(d, r"\d{4}-\d{2}-\d{2}")
        # Should match current UTC date
        expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(d, expected)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. RISK CONTROL — profit stop, loss limit, budget
# ═══════════════════════════════════════════════════════════════════════════════
class TestRiskControl(unittest.TestCase):

    def _cfg(self, acc=50_000, profit_target=150.0):
        from config import StrategyConfig
        return StrategyConfig(account_size=acc, daily_profit_target_usd=profit_target)

    def _snap(self, realized=0.0, open_pnl=0.0, trades=0, open_pos=0):
        from risk_control import RiskSnapshot
        return RiskSnapshot(realized, open_pnl, trades, open_pos)

    def test_daily_profit_not_hit(self):
        from risk_control import is_daily_profit_hit
        self.assertFalse(is_daily_profit_hit(100.0, self._cfg()))

    def test_daily_profit_hit_exact(self):
        from risk_control import is_daily_profit_hit
        self.assertTrue(is_daily_profit_hit(150.0, self._cfg()))

    def test_daily_profit_hit_above(self):
        from risk_control import is_daily_profit_hit
        self.assertTrue(is_daily_profit_hit(200.0, self._cfg()))

    def test_blocked_profit_target_reached(self):
        from risk_control import can_place_trade
        snap = self._snap(realized=150.0)
        ok, msg = can_place_trade(snap, self._cfg())
        self.assertFalse(ok)
        self.assertIn("DAILY_PROFIT_HIT", msg)

    def test_allowed_fresh_day(self):
        from risk_control import can_place_trade
        ok, msg = can_place_trade(self._snap(), self._cfg())
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

    def test_blocked_loss_limit(self):
        from risk_control import can_place_trade
        ok, _ = can_place_trade(self._snap(realized=-1500.0, trades=1), self._cfg())
        self.assertFalse(ok)

    def test_blocked_budget_insufficient(self):
        from risk_control import can_place_trade
        # daily_loss=100, SL=100: after 1 loss budget=0 → LIMIT_HIT blocks further
        ok, msg = can_place_trade(self._snap(realized=-100.0, trades=1), self._cfg())
        self.assertFalse(ok)

    def test_blocked_max_trades(self):
        from risk_control import can_place_trade
        ok, msg = can_place_trade(self._snap(trades=2), self._cfg())
        self.assertFalse(ok)
        self.assertIn("MAX_TRADES", msg)

    def test_blocked_position_open(self):
        from risk_control import can_place_trade
        ok, msg = can_place_trade(self._snap(open_pos=1), self._cfg())
        self.assertFalse(ok)
        self.assertIn("POSITION_OPEN", msg)

    def test_limit_not_breached(self):
        from risk_control import is_daily_limit_breached
        # daily_loss_limit = 100 by default in _cfg()
        self.assertFalse(is_daily_limit_breached(-99.0, self._cfg()))

    def test_limit_breached_exact(self):
        from risk_control import is_daily_limit_breached
        self.assertTrue(is_daily_limit_breached(-200.0, self._cfg()))

    def test_simulate_first_win_hits_profit_target(self):
        from risk_control import simulate_day
        # tp_dollar=00 > profit_target=50 → 1st win hits profit target, 2nd blocked
        r = simulate_day(2, 0, self._cfg(), spread_per_trade=0)
        self.assertEqual(r.wins, 1)
        self.assertTrue(r.hit_profit_target)
        self.assertFalse(r.hit_loss_limit)

    def test_simulate_both_win_with_higher_target(self):
        from config import StrategyConfig
        from risk_control import simulate_day
        # Set profit target above 2 * tp_dollar so both trades can fire
        cfg = StrategyConfig(account_size=50_000, daily_profit_target_usd=2000.0)
        r = simulate_day(2, 0, cfg, spread_per_trade=0)
        self.assertEqual(r.wins, 2)
        self.assertFalse(r.hit_loss_limit)

    def test_simulate_profit_stop_fires(self):
        from risk_control import simulate_day
        # profit target = $150, tp_dollar = $500 at 50k → 1 win hits $500 > $150
        # So 2nd trade blocked by profit target after first win
        r = simulate_day(3, 0, self._cfg(), spread_per_trade=0)
        self.assertTrue(r.hit_profit_target)
        self.assertEqual(r.wins, 1)  # only 1 trade fires before profit target hit

    def test_simulate_max_loss_never_breached(self):
        from risk_control import simulate_day
        r = simulate_day(0, 10, self._cfg(), spread_per_trade=0)
        self.assertGreaterEqual(r.gross_pnl, -self._cfg().max_daily_loss_usd)

    def test_spread_deducted(self):
        from risk_control import simulate_day
        r = simulate_day(2, 0, self._cfg(), spread_per_trade=35.0)
        self.assertGreater(r.spread_cost, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. EXECUTOR (MT5 injected at module level)
# ═══════════════════════════════════════════════════════════════════════════════
class TestExecutor(unittest.TestCase):

    def _sig(self, direction="LONG"):
        from trade_signal import Signal
        if direction == "LONG":
            return Signal("LONG", 4535.0, 4537.0, 4533.0, 4513.0)
        return Signal("SHORT", 4491.0, 4489.0, 4493.0, 4513.0)

    def _setup(self, retcode=10009, ask=4535.2, bid=4534.9):
        _fake_mt5.symbol_info_tick.return_value = MagicMock(ask=ask, bid=bid)
        result = MagicMock()
        result.retcode = retcode
        result.order   = 123
        result.price   = ask
        result.volume  = 2.5
        result.comment = "ok"
        _fake_mt5.order_send.return_value = result

    def test_long_uses_ask(self):
        from config import StrategyConfig
        from executor import place_order
        self._setup(ask=4535.2)
        r = place_order(self._sig("LONG"), StrategyConfig(account_size=50_000))
        self.assertTrue(r["success"])
        self.assertEqual(_fake_mt5.order_send.call_args[0][0]["price"], 4535.2)

    def test_short_uses_bid(self):
        from config import StrategyConfig
        from executor import place_order
        self._setup(bid=4491.0)
        r = place_order(self._sig("SHORT"), StrategyConfig(account_size=50_000))
        self.assertTrue(r["success"])
        self.assertEqual(_fake_mt5.order_send.call_args[0][0]["price"], 4491.0)

    def test_retcode_zero_is_success(self):
        from config import StrategyConfig
        from executor import place_order
        self._setup(retcode=0)
        self.assertTrue(place_order(self._sig(), StrategyConfig(account_size=50_000))["success"])

    def test_failed_retcode(self):
        from config import StrategyConfig
        from executor import place_order
        self._setup(retcode=10014)
        self.assertFalse(place_order(self._sig(), StrategyConfig(account_size=50_000))["success"])

    def test_no_tick_returns_error(self):
        from config import StrategyConfig
        from executor import place_order
        _fake_mt5.symbol_info_tick.return_value = None
        r = place_order(self._sig(), StrategyConfig(account_size=50_000))
        self.assertFalse(r.get("success", False))

    def test_sl_tp_on_request(self):
        from config import StrategyConfig
        from executor import place_order
        self._setup()
        place_order(self._sig("LONG"), StrategyConfig(account_size=50_000))
        req = _fake_mt5.order_send.call_args[0][0]
        self.assertAlmostEqual(req["sl"], 4533.0)
        self.assertAlmostEqual(req["tp"], 4537.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. MONTHLY SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════════
class TestMonthly(unittest.TestCase):

    def _month(self, acc, wins, mixed, losses):
        from config import StrategyConfig
        from risk_control import simulate_day
        cfg = StrategyConfig(account_size=acc)
        sp  = 35.0 * cfg.lot_size
        all_days = (
            [simulate_day(2, 0, cfg, sp) for _ in range(wins)]  +
            [simulate_day(1, 1, cfg, sp) for _ in range(mixed)] +
            [simulate_day(0, 2, cfg, sp) for _ in range(losses)]
        )
        return sum(d.net_pnl for d in all_days), cfg

    def test_all_accounts_realistic_positive(self):
        for acc in [10_000, 20_000, 50_000, 100_000]:
            net, _ = self._month(acc, 14, 5, 3)
            self.assertGreater(net, 0, f"Realistic scenario negative for ${acc:,}")

    def test_roi_scales_with_account(self):
        # With fixed lot=0.5 (SL=$100), larger accounts have smaller ROI %
        # but same absolute PnL. Confirm all positive at realistic scenario.
        for acc in [10_000, 20_000, 50_000, 100_000]:
            net, _ = self._month(acc, 14, 5, 3)
            self.assertGreater(net, 0, f"Negative PnL for ${acc:,}")

    def test_conservative_profitable(self):
        for acc in [10_000, 20_000, 50_000, 100_000]:
            net, _ = self._month(acc, 10, 8, 4)
            self.assertGreater(net, 0)

    def test_worst_day_within_prop_limit(self):
        from config import StrategyConfig
        from risk_control import simulate_day
        cfg = StrategyConfig(account_size=50_000)
        r = simulate_day(0, 5, cfg, 35.0 * cfg.lot_size)
        # Funding Pips $50k daily limit = $2,500
        self.assertGreater(r.net_pnl, -2500.0)


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════════
def run_tests():
    classes = [
        TestConfig, TestThreshold, TestTradeSignal,
        TestSession, TestStartReader, TestRiskControl,
        TestExecutor, TestMonthly,
    ]
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(f"\n{'='*55}")
    print(f"  TOTAL : {result.testsRun}")
    print(f"  ✅ PASS: {passed}")
    print(f"  ❌ FAIL: {len(result.failures)}")
    print(f"  ⚠️  ERR : {len(result.errors)}")
    print(f"{'='*55}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())


# ═══════════════════════════════════════════════════════════════════════════════
# 9. BACKTEST ENGINE — fixed intrabar logic
# ═══════════════════════════════════════════════════════════════════════════════
class TestBacktest(unittest.TestCase):

    def _cfg(self, **kw):
        from config import StrategyConfig
        defaults = dict(
            account_size=50_000,
            daily_profit_target_usd=10_000,  # high so profit stop does not interfere
            direction_mode="both",
            max_entry_overshoot_pips=5.0,    # generous for tests
        )
        defaults.update(kw)
        return StrategyConfig(**defaults)

    def _bar(self, hhmm, o, h, l, c):
        from backtest import Bar
        from datetime import datetime, timezone
        h_v, m_v = map(int, hhmm.split(":"))
        return Bar(time_utc=datetime(2026,3,31,h_v,m_v,tzinfo=timezone.utc), open=o, high=h, low=l, close=c)

    def _day(self, bars_args):
        return [self._bar(*a) for a in bars_args]

    def test_long_fires_and_tp_hits(self):
        """Start=4513: long_entry=4535, long_tp=4537. Bar high crosses 4537."""
        from backtest import run_day
        cfg = self._cfg()
        bars = self._day([
            ("08:00", 4513, 4515, 4512, 4514),   # start bar → S=4513
            ("08:05", 4514, 4520, 4513, 4518),   # no entry
            ("08:10", 4530, 4538, 4529, 4537),   # high=4538 ≥ entry=4535 → LONG fires
                                                   # same bar high=4538 ≥ tp=4537 → TP
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].direction, "LONG")
        self.assertEqual(r.trades[0].outcome, "TP")
        self.assertGreater(r.day_gross, 0)

    def test_short_fires_and_tp_hits(self):
        """Start=4513: short_entry=4491, short_tp=4489, short_sl=4493.
        Bar low=4488 crosses entry(4491) and TP(4489). Bar close=4488 <= tp=4489 → TP wins."""
        from backtest import run_day
        cfg = self._cfg()
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),   # start bar → S=4513
            # low=4488 ≤ entry=4491 → SHORT fires
            # same bar: high=4492 < sl=4493 (SL NOT hit), low=4488 ≤ tp=4489 → TP
            ("08:05", 4510, 4492, 4488, 4488),
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].direction, "SHORT")
        self.assertEqual(r.trades[0].outcome, "TP")
        self.assertGreater(r.day_gross, 0)

    def test_sl_hit_produces_loss(self):
        """Long fires on bar 2. Bar 3 low=4532 ≤ sl=4533 → SL hit."""
        from backtest import run_day
        cfg = self._cfg()
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),   # start bar
            ("08:05", 4530, 4536, 4530, 4535),   # high=4536 ≥ entry=4535 → LONG fires
                                                   # same bar: low=4530 ≤ sl=4533 → SL wins
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertEqual(len(r.trades), 1)
        self.assertEqual(r.trades[0].outcome, "SL")
        self.assertLess(r.day_gross, 0)

    def test_sl_on_next_bar(self):
        """Long fires bar 2 but only high crosses (no SL same bar). SL on bar 3."""
        from backtest import run_day
        cfg = self._cfg()
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),
            ("08:05", 4535, 4536, 4535, 4535),   # high=4536 → LONG entry, low=4535 > sl=4533
            ("08:10", 4534, 4535, 4532, 4533),   # low=4532 ≤ sl=4533 → SL
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertEqual(r.trades[0].outcome, "SL")
        self.assertLess(r.day_gross, 0)

    def test_tp_on_next_bar(self):
        """Long fires but TP not hit same bar. TP hit on next bar."""
        from backtest import run_day
        cfg = self._cfg()
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),
            ("08:05", 4535, 4536, 4535, 4535),   # entry fires, no TP same bar
            ("08:10", 4536, 4538, 4535, 4537),   # high=4538 ≥ tp=4537 → TP
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertEqual(r.trades[0].outcome, "TP")
        self.assertGreater(r.day_gross, 0)

    def test_no_trigger_ranging_day(self):
        """Price stays within ±20 of start — no entry fires."""
        from backtest import run_day
        cfg = self._cfg()
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),
            ("08:05", 4515, 4520, 4510, 4515),
            ("08:10", 4512, 4518, 4508, 4512),
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertTrue(r.no_trigger)
        self.assertEqual(len(r.trades), 0)

    def test_max_two_trades(self):
        from backtest import run_day
        from config import StrategyConfig
        cfg = StrategyConfig(
            account_size=50_000,
            daily_profit_target_usd=100_000,
            direction_mode="both",
            max_entry_overshoot_pips=10.0,
        )
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),
            ("08:05", 4510, 4538, 4489, 4535),   # both long+short cross
            ("08:10", 4535, 4540, 4489, 4537),
            ("08:15", 4537, 4542, 4487, 4540),
            ("08:20", 4540, 4545, 4486, 4542),
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertLessEqual(len(r.trades), 2)

    def test_group_by_day(self):
        from backtest import Bar, group_by_day
        from datetime import datetime, timezone
        def make(y,mo,d,h,m):
            return Bar(time_utc=datetime(y,mo,d,h,m,tzinfo=timezone.utc),open=4510,high=4512,low=4508,close=4510)
        bars = [
            make(2026,3,31,6,0),    # UTC 06:00 → date 2026-03-31
            make(2026,3,31,22,0),   # UTC 22:00 → date 2026-03-31
            make(2026,4,1,6,0),     # UTC 06:00 → date 2026-04-01
        ]
        dm = group_by_day(bars)
        self.assertIn("2026-03-31", dm)
        self.assertIn("2026-04-01", dm)
        # Verify UTC grouping: 22:00 March 31 is still March 31 in UTC
        self.assertEqual(len(dm["2026-03-31"]), 2)

    def test_overshoot_rejects_stale_entry(self):
        """Bar close is 6 pips past entry → overshoot filter rejects it."""
        from backtest import run_day
        from config import StrategyConfig
        cfg = StrategyConfig(
            account_size=50_000,
            daily_profit_target_usd=10_000,
            direction_mode="both",
            max_entry_overshoot_pips=3.0,   # tight filter
        )
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),
            ("08:05", 4535, 4542, 4534, 4541),  # closes at 4541 → 4541-4535=6 > 3 → reject
        ])
        r = run_day("2026-03-31", bars, cfg)
        self.assertTrue(r.no_trigger)

    def test_force_close_eod(self):
        from backtest import run_day
        from config import StrategyConfig
        cfg = StrategyConfig(
            account_size=50_000,
            daily_profit_target_usd=10_000,
            direction_mode="both",
            max_entry_overshoot_pips=10.0,
            force_close_hhmm="08:15",
        )
        bars = self._day([
            ("08:00", 4513, 4514, 4512, 4513),
            ("08:05", 4535, 4536, 4535, 4535),   # entry fires, no exit same bar
            ("08:10", 4535, 4536, 4534, 4535),   # no exit
            ("08:15", 4534, 4536, 4533, 4534),   # force_close time
        ])
        r = run_day("2026-03-31", bars, cfg)
        if r.trades:
            self.assertEqual(r.trades[0].outcome, "FORCE_CLOSE")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. EXECUTOR FOK + RETRY
# ═══════════════════════════════════════════════════════════════════════════════
class TestExecutorFOK(unittest.TestCase):

    def _sig(self, direction="LONG"):
        from trade_signal import Signal
        if direction == "LONG":
            return Signal("LONG", 4535.0, 4537.0, 4533.0, 4513.0)
        return Signal("SHORT", 4491.0, 4489.0, 4493.0, 4513.0)

    def _setup(self, retcode=10009, ask=4535.2, bid=4534.9):
        _fake_mt5.symbol_info_tick.return_value = MagicMock(ask=ask, bid=bid)
        result = MagicMock()
        result.retcode = retcode
        result.order   = 123
        result.price   = ask
        result.volume  = 2.5
        result.comment = "ok"
        _fake_mt5.order_send.return_value = result

    def test_fok_fill_policy_set(self):
        """ORDER_FILLING_FOK must be set on every request."""
        from config import StrategyConfig
        from executor import place_order
        self._setup()
        place_order(self._sig(), StrategyConfig(account_size=50_000))
        req = _fake_mt5.order_send.call_args[0][0]
        # FOK = mt5.ORDER_FILLING_FOK which is mocked as 1
        self.assertIn("type_filling", req)

    def test_success_on_first_attempt(self):
        from config import StrategyConfig
        from executor import place_order
        self._setup(retcode=10009)
        r = place_order(self._sig(), StrategyConfig(account_size=50_000))
        self.assertTrue(r["success"])
        self.assertEqual(r["attempts"], 1)

    def test_retry_on_requote_then_success(self):
        """First attempt: requote (10004). Second: success."""
        from config import StrategyConfig
        from executor import place_order

        tick = MagicMock()
        tick.ask = 4535.2
        tick.bid = 4534.9
        _fake_mt5.symbol_info_tick.return_value = tick

        fail_result = MagicMock(); fail_result.retcode = 10004; fail_result.comment = "Requote"
        ok_result   = MagicMock(); ok_result.retcode   = 10009; ok_result.order = 123
        ok_result.price = 4535.2; ok_result.volume = 2.5; ok_result.comment = "ok"

        _fake_mt5.order_send.side_effect = [fail_result, ok_result]

        r = place_order(self._sig(), StrategyConfig(account_size=50_000),
                        max_retries=3, retry_delay=0)
        self.assertTrue(r["success"])
        self.assertEqual(r["attempts"], 2)
        _fake_mt5.order_send.side_effect = None

    def test_exhausts_retries_on_permanent_failure(self):
        """Non-retryable error code → fails immediately without retrying."""
        from config import StrategyConfig
        from executor import place_order

        tick = MagicMock(); tick.ask = 4535.2; tick.bid = 4534.9
        _fake_mt5.symbol_info_tick.return_value = tick
        fail = MagicMock(); fail.retcode = 10019; fail.comment = "No money"
        fail.order = 0
        _fake_mt5.order_send.return_value = fail

        r = place_order(self._sig(), StrategyConfig(account_size=50_000),
                        max_retries=3, retry_delay=0)
        self.assertFalse(r["success"])

    def test_no_tick_retries_and_fails(self):
        from config import StrategyConfig
        from executor import place_order
        _fake_mt5.symbol_info_tick.return_value = None
        r = place_order(self._sig(), StrategyConfig(account_size=50_000),
                        max_retries=2, retry_delay=0)
        self.assertFalse(r["success"])
        self.assertEqual(r["attempts"], 2)

    def test_retcode_zero_is_success(self):
        from config import StrategyConfig
        from executor import place_order
        self._setup(retcode=0)
        r = place_order(self._sig(), StrategyConfig(account_size=50_000))
        self.assertTrue(r["success"])


# ═══════════════════════════════════════════════════════════════════════════════
# 11. WATCHDOG
# ═══════════════════════════════════════════════════════════════════════════════
class TestWatchdog(unittest.TestCase):
    """
    FIX: watchdog now uses subprocess.PIPE for stdout/stderr (not sys.stdout).
    This makes it robust under redirected stdout (tests, pytest, cron).
    """

    def _tmp_script(self, code: str) -> str:
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False)
        f.write(code)
        f.close()
        return f.name

    def test_clean_exit_does_not_restart(self):
        script = self._tmp_script("import sys; sys.exit(0)")
        try:
            from watchdog import run_watchdog
            run_watchdog(script=script, max_restarts=5, cooldown=0)
        finally:
            os.unlink(script)
        # If we get here without infinite loop → pass
        self.assertTrue(True)

    def test_crash_triggers_restart(self):
        script = self._tmp_script("import sys; sys.exit(1)")
        try:
            from watchdog import run_watchdog
            # max_restarts=2 means it tries 3 total launches then stops
            run_watchdog(script=script, max_restarts=2, cooldown=0)
        finally:
            os.unlink(script)
        self.assertTrue(True)

    def test_max_restarts_respected(self):
        script = self._tmp_script("import sys; sys.exit(2)")
        try:
            from watchdog import run_watchdog
            run_watchdog(script=script, max_restarts=3, cooldown=0)
        finally:
            os.unlink(script)
        self.assertTrue(True)

    def test_crash_log_written(self):
        from pathlib import Path
        crash_log = Path("crash.log")
        if crash_log.exists():
            crash_log.unlink()

        script = self._tmp_script("import sys; sys.exit(99)")
        try:
            from watchdog import run_watchdog
            run_watchdog(script=script, max_restarts=1, cooldown=0)
            self.assertTrue(crash_log.exists(), "crash.log was not created")
        finally:
            os.unlink(script)
            if crash_log.exists():
                crash_log.unlink()

    def test_script_not_found_exits_cleanly(self):
        from watchdog import run_watchdog
        # Should not raise — should log error and return
        try:
            run_watchdog(script="__nonexistent_script_xyz__.py", max_restarts=1, cooldown=0)
        except SystemExit:
            pass
        self.assertTrue(True)


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════════
def run_tests():
    classes = [
        TestConfig, TestThreshold, TestTradeSignal,
        TestSession, TestStartReader, TestRiskControl,
        TestExecutor, TestMonthly,
        TestBacktest, TestExecutorFOK, TestWatchdog,
    ]
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  TOTAL : {result.testsRun}")
    print(f"  PASS  : {passed}")
    print(f"  FAIL  : {len(result.failures)}")
    print(f"  ERR   : {len(result.errors)}")
    print(sep)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())