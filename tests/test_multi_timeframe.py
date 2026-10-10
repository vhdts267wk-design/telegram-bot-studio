from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from bot import multi_timeframe as mtf


class MultiTimeframeTests(unittest.TestCase):
    now = datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)

    def feed(self, *, now=None, sell=False, count=64, broker_offset=180):
        now = self.now if now is None else now
        frames = {}
        for name, seconds in mtf.TIMEFRAME_SECONDS.items():
            end = datetime.fromtimestamp((int(now.timestamp()) + broker_offset * 60) // seconds * seconds
                                         - broker_offset * 60, timezone.utc)
            step = {"M15": 0.2, "M5": 0.1, "M1": 0.02, "H1": .4, "H4": .8}[name]
            body = {"M15": 0.1, "M5": 0.04, "M1": 0.01, "H1": .2, "H4": .4}[name]
            wick = {"M15": 0.15, "M5": 0.1, "M1": 0.015, "H1": .3, "H4": .6}[name]
            bars = []
            for index in range(count):
                close = round(2000 - step * (count - 1 - index), 6)
                opening = round(close - body, 6)
                bars.append({"time": (end - timedelta(seconds=(count - index) * seconds)).isoformat(),
                             "open": opening, "high": round(close + wick, 6),
                             "low": round(opening - wick, 6), "close": close,
                             "tick_volume": 100})
            if name == "M5" and count >= 2:
                bars[-2].update(open=1999.7, high=1999.95, low=1999.3, close=1999.8)
                bars[-1].update(open=1999.8, high=2000.15, low=1999.65, close=2000.0)
            if sell:
                for bar in bars:
                    bar.update(open=round(4000 - bar["open"], 6),
                               high=round(4000 - bar["low"], 6),
                               low=round(4000 - bar["high"], 6),
                               close=round(4000 - bar["close"], 6))
            frames[name] = bars
        return {
            "schema_version": 2, "source": "MetaTrader 5", "symbol": "XAUUSD",
            "timeframe": "M15", "as_of": now.isoformat(), "broker_utc_offset_minutes": broker_offset,
            "quote": {"bid": 1999.997 if sell else 2000.001,
                      "ask": 1999.999 if sell else 2000.003, "time": now.isoformat()},
            "execution": {"tick_size": 0.001, "point": 0.001, "digits": 3, "stops_level": 5},
            "timeframes": frames, "candles": deepcopy(frames["M15"]),
            "risk_context": {
                "account_mode": "demo", "volume": 0.01, "equity": 10000,
                "open_positions": 0, "pending_orders": 0,
                "loss_cash_per_price_unit": 1, "profit_cash_per_price_unit": 1,
                "commission_round_turn": 0.02, "slippage_price": 0.002,
                "costs_verified": True, "as_of": now.isoformat(),
                "free_margin": 9000, "margin_required": 25,
                "broker_fingerprint": "a" * 64,
            },
        }

    def analyze(self, feed=None, *, now=None, research_only=False):
        return mtf.analyze_multi_timeframe(self.feed() if feed is None else feed,
                                           self.now if now is None else now,
                                           research_only=research_only)

    def assert_waiting(self, result, state, reason):
        self.assertEqual((result["state"], result["reason"]), (state, reason), result)
        self.assertNotIn("direction", result)
        self.assertNotIn("entry", result)

    def test_buy_uses_current_executable_entry_and_closed_three_frame_references(self):
        feed = self.feed()
        original = deepcopy(feed)
        result = self.analyze(feed)
        self.assertEqual(result["state"], "signal", result)
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["entry"], feed["quote"]["ask"])
        self.assertNotEqual(result["entry"], feed["timeframes"]["M1"][-1]["close"])
        self.assertEqual(result["bar_time"], feed["timeframes"]["M1"][-1]["time"])
        self.assertEqual(result["direction_bar_time"], feed["timeframes"]["M15"][-1]["time"])
        self.assertEqual(result["confirmation_bar_time"], feed["timeframes"]["M5"][-1]["time"])
        self.assertEqual(result["display_timeframe"], "M1")
        self.assertEqual(result["deadline"], (self.now + timedelta(hours=1)).isoformat())
        self.assertEqual(result["horizon_seconds"], 3600)
        self.assertEqual(result["broker_fingerprint"], "a" * 64)
        self.assertIs(result["provisional"], False)
        self.assertNotIn("qualification_id", result)
        self.assertEqual(feed, original)
        json.dumps(result, allow_nan=False)

    def test_sell_is_mirrored_without_changing_fixed_protection_or_target_semantics(self):
        feed = self.feed(sell=True)
        result = self.analyze(feed)
        self.assertEqual(result["state"], "signal", result)
        self.assertEqual(result["direction"], "SELL")
        self.assertEqual(result["entry"], feed["quote"]["bid"])
        self.assertLess(result["target2"], result["target"])
        self.assertLess(result["target"], result["entry_zone_low"])
        self.assertLess(result["entry_zone_high"], result["stop"])
        risk = Decimal(str(result["original_stop_distance"]))
        self.assertEqual(Decimal(str(result["stop"])) - Decimal(str(result["entry"])), risk)
        self.assertEqual(Decimal(str(result["entry"])) - Decimal(str(result["target"])), 2 * risk)
        self.assertEqual(result["nominal_reward_risk"], 2)

    def test_five_frame_context_is_qualitative_and_h4_levels_use_only_recent_closed_range(self):
        for sell in (False, True):
            feed = self.feed(sell=sell)
            result = self.analyze(feed)
            context = result["timeframe_context"]
            self.assertEqual(context["trends"], dict.fromkeys(mtf.TIMEFRAME_SECONDS, "SELL" if sell else "BUY"))
            self.assertEqual((context["alignment"], context["confidence"], context["counter_trend"]),
                             ("aligned", "aligned", False))
            recent = feed["timeframes"]["H4"][-20:]
            self.assertEqual(context["support"], min(bar["low"] for bar in recent))
            self.assertEqual(context["resistance"], max(bar["high"] for bar in recent))
            self.assertEqual(result["context_bar_times"], {key: feed["timeframes"][key][-1]["time"] for key in ("H1", "H4")})
            self.assertNotIn("probability", context)
            feed["timeframes"]["H4"][0].update(low=1000, high=3000)
            self.assertEqual(self.analyze(feed)["timeframe_context"], context)

    def test_higher_context_conflict_blocks_buy_and_sell_without_actionable_fields(self):
        for sell in (False, True):
            for frame in ("H1", "H4"):
                with self.subTest(sell=sell, frame=frame):
                    feed = self.feed(sell=sell)
                    feed["timeframes"][frame] = self.feed(sell=not sell)["timeframes"][frame]
                    result = self.analyze(feed)
                    self.assert_waiting(result, "no_signal", "higher_timeframe_conflict")
                    self.assertEqual(result["timeframe_context"]["trends"][frame], "BUY" if sell else "SELL")
                    self.assertEqual((result["timeframe_context"]["alignment"], result["timeframe_context"]["confidence"],
                                      result["timeframe_context"]["counter_trend"]), ("counter_trend", "reduced", True))
                    for key in ("stop", "target", "target2", "entry_zone_low", "effective_reward_risk"):
                        self.assertNotIn(key, result)

    def test_neutral_higher_context_waits_and_never_implies_confidence_percentage(self):
        for frame in ("H1", "H4"):
            feed = self.feed()
            for bar in feed["timeframes"][frame]:
                bar.update(open=2000, high=2000.1, low=1999.9, close=2000)
            result = self.analyze(feed)
            self.assert_waiting(result, "no_signal", "higher_timeframe_neutral")
            self.assertEqual(result["timeframe_context"]["trends"][frame], "NEUTRAL")
            self.assertEqual((result["timeframe_context"]["alignment"], result["timeframe_context"]["confidence"]),
                             ("unconfirmed", "unconfirmed"))
            self.assertIs(result["timeframe_context"]["counter_trend"], False)

    def test_three_frame_legacy_missing_higher_history_and_bad_clock_offsets_fail_closed(self):
        feed = self.feed()
        feed["timeframes"] = {key: feed["timeframes"][key] for key in ("M1", "M5", "M15")}
        self.assert_waiting(self.analyze(feed), "warmup", "missing_timeframes")
        for offset in (None, True, "180", -735, 855, 181):
            feed = self.feed()
            feed["broker_utc_offset_minutes"] = offset
            self.assert_waiting(self.analyze(feed), "invalid", "invalid_broker_utc_offset")

    def test_h4_grid_uses_explicit_broker_offset_instead_of_utc_midnight(self):
        for offset in (-720, -345, 0, 180, 345, 840):
            feed = self.feed(broker_offset=offset)
            result = self.analyze(feed)
            self.assertEqual(result["state"], "signal", result)
            for frame, reference in result["context_bar_times"].items():
                stamp = datetime.fromisoformat(reference)
                self.assertEqual((stamp.timestamp() + offset * 60) % mtf.TIMEFRAME_SECONDS[frame], 0)
            if offset == 180:
                self.assertNotEqual(datetime.fromisoformat(result["context_bar_times"]["H4"]).timestamp() % 14400, 0)
        feed = self.feed()
        feed["broker_utc_offset_minutes"] = 0
        self.assert_waiting(self.analyze(feed), "invalid", "unaligned_candles_H4")

    def test_higher_bars_closed_at_receipt_but_after_m1_decision_are_lookahead(self):
        clock = self.now.replace(hour=13, minute=0)
        for target in ("H1", "H4"):
            feed = self.feed(now=clock)
            for frame in ("M1", "M5", "M15", "H1", "H4"):
                if frame == target:
                    continue
                for bar in feed["timeframes"][frame]:
                    bar["time"] = (datetime.fromisoformat(bar["time"]) - timedelta(seconds=mtf.TIMEFRAME_SECONDS[frame])).isoformat()
            self.assert_waiting(self.analyze(feed, now=clock), "invalid", "lookahead_" + target)

    def test_tick_grid_rounds_entry_adversely_and_zone_inward(self):
        for sell in (False, True):
            with self.subTest(sell=sell):
                feed = self.feed(sell=sell)
                feed["quote"].update(bid=1999.9974 if sell else 2000.0014,
                                     ask=1999.9994 if sell else 2000.0034)
                result = self.analyze(feed)
                self.assertEqual(result["state"], "signal", result)
                self.assertEqual(result["entry"], 1999.997 if sell else 2000.004)
                tick = Decimal(".001")
                for field in ("entry", "stop", "target", "target2", "entry_zone_low", "entry_zone_high"):
                    self.assertEqual(Decimal(str(result[field])) % tick, 0)
                entry = Decimal(str(result["entry"]))
                width = Decimal(str(result["original_stop_distance"])) * Decimal(".1")
                self.assertGreaterEqual(Decimal(str(result["entry_zone_low"])), entry - width)
                self.assertLessEqual(Decimal(str(result["entry_zone_high"])), entry + width)

    def test_costs_are_round_trip_cash_and_per_side_slippage_without_double_spread(self):
        result = self.analyze()
        risk = Decimal(str(result["original_stop_distance"]))
        cost = Decimal(".02") + 2 * Decimal(".002")
        self.assertEqual(result["estimated_cost_cash"], float(cost))
        self.assertAlmostEqual(result["estimated_loss_cash"], float(risk + cost), places=6)
        self.assertAlmostEqual(result["effective_reward_risk"], float((2 * risk - cost) / (risk + cost)), places=6)
        self.assertEqual(result["cost_context"], {"commission_round_turn": .02, "slippage_price": .002,
                                                 "loss_cash_per_price_unit": 1.0, "profit_cash_per_price_unit": 1.0})

    def test_22_actual_contiguous_bars_are_sufficient(self):
        feed = self.feed(count=22)
        result = self.analyze(feed)
        self.assertEqual(result["state"], "signal", result)
        self.assertEqual(result["required_bars"], 22)
        self.assertEqual(result["contiguous_counts"], dict.fromkeys(mtf.TIMEFRAME_SECONDS, 22))

    def test_gap_resets_indicators_without_filling_or_using_older_regime(self):
        feed = self.feed()
        for frame, seconds in mtf.TIMEFRAME_SECONDS.items():
            for bar in feed["timeframes"][frame][:-22]:
                bar["time"] = (datetime.fromisoformat(bar["time"]) - timedelta(seconds=seconds)).isoformat()
                bar.update(open=1000, high=1001, low=999, close=1000)
        result = self.analyze(feed)
        suffix = deepcopy(feed)
        suffix["timeframes"] = {key: values[-22:] for key, values in feed["timeframes"].items()}
        suffix_result = self.analyze(suffix)
        self.assertEqual(result["state"], "signal", result)
        self.assertEqual(result["contiguous_counts"], dict.fromkeys(mtf.TIMEFRAME_SECONDS, 22))
        self.assertEqual(result["indicators"], suffix_result["indicators"])
        self.assertEqual(result["stop"], suffix_result["stop"])
        self.assertEqual(len(feed["timeframes"]["M15"]), 64)

    def test_gap_with_only_21_recent_bars_waits_even_when_total_64(self):
        feed = self.feed()
        for bar in feed["timeframes"]["M15"][:-21]:
            bar["time"] = (datetime.fromisoformat(bar["time"]) - timedelta(minutes=15)).isoformat()
        result = self.analyze(feed)
        self.assert_waiting(result, "warmup", "insufficient_contiguous_history")
        self.assertEqual(result["candle_counts"]["M15"], 64)
        self.assertEqual(result["contiguous_counts"]["M15"], 21)

    def test_each_timeframe_requires_22_and_missing_frames_wait(self):
        for key in mtf.TIMEFRAME_SECONDS:
            with self.subTest(key=key):
                feed = self.feed()
                feed["timeframes"][key] = feed["timeframes"][key][-21:]
                self.assert_waiting(self.analyze(feed), "warmup", "insufficient_contiguous_history")
        feed = self.feed()
        del feed["timeframes"]["M1"]
        self.assert_waiting(self.analyze(feed), "warmup", "missing_timeframes")

    def test_excess_history_duplicates_reverse_order_and_unaligned_bars_rejected(self):
        feed = self.feed()
        feed["timeframes"]["M1"].insert(0, deepcopy(feed["timeframes"]["M1"][0]))
        self.assert_waiting(self.analyze(feed), "invalid", "excess_history")
        for modification, reason in (("duplicate", "non_monotonic_M5"), ("reverse", "non_monotonic_M5"),
                                     ("unaligned", "unaligned_candles_M5")):
            with self.subTest(modification=modification):
                feed = self.feed()
                values = feed["timeframes"]["M5"]
                if modification == "duplicate":
                    values[-1]["time"] = values[-2]["time"]
                elif modification == "reverse":
                    values[-2], values[-1] = values[-1], values[-2]
                else:
                    values[-1]["time"] = (datetime.fromisoformat(values[-1]["time"]) + timedelta(seconds=1)).isoformat()
                self.assert_waiting(self.analyze(feed), "invalid", reason)

    def test_forming_bars_and_snapshot_before_close_rejected(self):
        for key in mtf.TIMEFRAME_SECONDS:
            with self.subTest(key=key):
                feed = self.feed()
                seconds = mtf.TIMEFRAME_SECONDS[key]
                forming = datetime.fromtimestamp((int(self.now.timestamp()) + 180 * 60) // seconds * seconds
                                                 - 180 * 60, timezone.utc)
                feed["timeframes"][key][-1]["time"] = forming.isoformat()
                self.assert_waiting(self.analyze(feed), "invalid", "forming_bar_" + key)
        feed = self.feed()
        feed["as_of"] = (self.now - timedelta(seconds=1)).isoformat()
        self.assert_waiting(self.analyze(feed), "invalid", "snapshot_precedes_bar_close")

    def test_stale_quote_bounds_and_future_skew(self):
        for seconds, expected in ((10, "signal"), (11, "stale"), (-5, "signal"), (-6, "stale")):
            with self.subTest(seconds=seconds):
                feed = self.feed()
                feed["quote"]["time"] = (self.now - timedelta(seconds=seconds)).isoformat()
                self.assertEqual(self.analyze(feed)["state"], expected)

    def test_snapshot_and_risk_context_stay_fresh_independently(self):
        feed = self.feed()
        feed["as_of"] = (self.now - timedelta(seconds=31)).isoformat()
        self.assert_waiting(self.analyze(feed), "stale", "stale_snapshot")
        for seconds in (31, -1):
            feed = self.feed()
            feed["risk_context"]["as_of"] = (self.now - timedelta(seconds=seconds)).isoformat()
            self.assert_waiting(self.analyze(feed), "blocked", "stale_risk_context")

    def test_m1_timing_75_seconds_boundary_and_higher_frame_no_lookahead(self):
        for seconds, state in ((75, "signal"), (76, "stale")):
            clock = self.now + timedelta(seconds=seconds)
            feed = self.feed()
            feed["as_of"] = clock.isoformat()
            feed["risk_context"]["as_of"] = clock.isoformat()
            feed["quote"]["time"] = clock.isoformat()
            result = self.analyze(feed, now=clock)
            self.assertEqual(result["state"], state, result)
        feed = self.feed()
        for bar in feed["timeframes"]["M1"]:
            bar["time"] = (datetime.fromisoformat(bar["time"]) - timedelta(minutes=1)).isoformat()
        self.assert_waiting(self.analyze(feed), "invalid", "lookahead_M15")

    def test_stale_higher_frame_is_not_reused(self):
        for key, seconds in (("M15", 900), ("M5", 300), ("H1", 3600), ("H4", 14400)):
            with self.subTest(key=key):
                feed = self.feed()
                for bar in feed["timeframes"][key]:
                    bar["time"] = (datetime.fromisoformat(bar["time"]) - timedelta(seconds=seconds)).isoformat()
                self.assert_waiting(self.analyze(feed), "stale", "stale_" + key)

    def test_flat_m15_trend_does_not_force_direction(self):
        feed = self.feed()
        for bar in feed["timeframes"]["M15"]:
            bar.update(open=2000, high=2000.1, low=1999.9, close=2000)
        self.assert_waiting(self.analyze(feed), "no_signal", "flat_m15_trend")

    def test_m5_requires_actual_pullback_and_closed_recovery(self):
        for missing in ("pullback", "recovery", "aligned"):
            with self.subTest(missing=missing):
                feed = self.feed()
                if missing == "pullback":
                    feed["timeframes"]["M5"][-2].update(low=1999.7)
                elif missing == "recovery":
                    feed["timeframes"]["M5"][-1].update(open=2000.0, close=1999.9)
                else:
                    for bar in feed["timeframes"]["M5"]:
                        bar.update(open=2000, high=2000.15, low=1999.3, close=2000)
                self.assert_waiting(self.analyze(feed), "no_signal", "m5_pullback_not_confirmed")

    def test_m1_breakout_is_a_close_beyond_previous_range_not_merely_wick(self):
        feed = self.feed()
        feed["timeframes"]["M1"][-1].update(high=2000.03, close=1999.99)
        self.assert_waiting(self.analyze(feed), "no_signal", "m1_breakout_not_confirmed")

    def test_tick_activity_floor_uses_previous20_not_last_or_real_volume(self):
        for last_volume, expected in ((50, "signal"), (49, "blocked")):
            feed = self.feed()
            feed["timeframes"]["M1"][-1]["tick_volume"] = last_volume
            for bar in feed["timeframes"]["M1"]:
                bar["real_volume"] = 0
            self.assertEqual(self.analyze(feed)["state"], expected)
        feed = self.feed()
        for bar in feed["timeframes"]["M1"][-21:-1]:
            bar["tick_volume"] = 0
        self.assert_waiting(self.analyze(feed), "blocked", "low_tick_activity")

    def test_latest_extreme_true_range_blocks_each_frame(self):
        for key in mtf.TIMEFRAME_SECONDS:
            with self.subTest(key=key):
                feed = self.feed()
                feed["timeframes"][key][-1]["high"] = 2050
                self.assert_waiting(self.analyze(feed), "blocked", "extreme_true_range")

    def test_spread_unknown_or_excessive_is_blocked_and_crossed_quote_invalid(self):
        for bid, ask, state, reason in ((2000.003, 2000.003, "blocked", "excessive_or_unknown_spread"),
                                       (2000.001, 2000.05, "blocked", "excessive_or_unknown_spread"),
                                       (2000.004, 2000.003, "invalid", "crossed_quote")):
            feed = self.feed()
            feed["quote"].update(bid=bid, ask=ask)
            self.assert_waiting(self.analyze(feed), state, reason)

    def test_broker_minimum_distance_is_checked_against_executable_side(self):
        feed = self.feed()
        feed["execution"]["stops_level"] = 2000
        self.assert_waiting(self.analyze(feed), "blocked", "broker_protection_distance")

    def test_any_existing_position_or_order_blocks_manual_candidate(self):
        for key in ("open_positions", "pending_orders"):
            feed = self.feed()
            feed["risk_context"][key] = 1
            self.assert_waiting(self.analyze(feed), "blocked", "existing_exposure")

    def test_demo_fixed_volume_and_complete_risk_data_are_required(self):
        for key in mtf.RISK_FIELDS:
            with self.subTest(missing=key):
                feed = self.feed()
                del feed["risk_context"][key]
                self.assert_waiting(self.analyze(feed), "blocked", "missing_risk_context")
        for key, value in (("account_mode", "real"), ("volume", .02)):
            feed = self.feed()
            feed["risk_context"][key] = value
            self.assert_waiting(self.analyze(feed), "blocked", "unsupported_demo_configuration")

    def test_unknown_costs_cannot_qualify_but_explicit_research_is_provisional(self):
        feed = self.feed()
        feed["risk_context"]["costs_verified"] = False
        self.assert_waiting(self.analyze(feed), "blocked", "unverified_costs")
        result = self.analyze(feed, research_only=True)
        self.assertEqual(result["state"], "signal", result)
        self.assertIs(result["provisional"], True)
        self.assertNotIn("qualification_id", result)
        verified_result = self.analyze(self.feed(), research_only=True)
        self.assertIs(verified_result["provisional"], True)
        for value in (None, 1, "true"):
            feed["risk_context"]["costs_verified"] = value
            self.assert_waiting(self.analyze(feed, research_only=True), "blocked", "unverified_costs")
        self.assert_waiting(self.analyze(research_only=1), "invalid", "invalid_research_mode")

    def test_research_mode_does_not_bypass_exposure_freshness_or_risk_budget(self):
        feed = self.feed()
        feed["risk_context"].update(costs_verified=False, open_positions=1)
        self.assert_waiting(self.analyze(feed, research_only=True), "blocked", "existing_exposure")
        feed["risk_context"].update(open_positions=0, equity=10)
        self.assert_waiting(self.analyze(feed, research_only=True), "blocked", "equity_risk_limit")
        feed["quote"]["time"] = (self.now - timedelta(seconds=11)).isoformat()
        self.assert_waiting(self.analyze(feed, research_only=True), "stale", "stale_quote")

    def test_stop_cost_risk_is_capped_at_one_percent_and_margin_requires_twice_requirement(self):
        feed = self.feed()
        feed["risk_context"]["equity"] = 10
        self.assert_waiting(self.analyze(feed), "blocked", "equity_risk_limit")
        for margin, expected in ((49.99, "blocked"), (50, "signal")):
            feed = self.feed()
            feed["risk_context"]["free_margin"] = margin
            self.assertEqual(self.analyze(feed)["state"], expected)

    def test_effective_reward_risk_accounts_for_commission_and_cash_conversion(self):
        for field, value in (("commission_round_turn", 2), ("profit_cash_per_price_unit", .1),
                             ("slippage_price", .3)):
            feed = self.feed()
            feed["risk_context"][field] = value
            self.assert_waiting(self.analyze(feed), "blocked", "insufficient_reward_after_costs")

    def test_session_requires_weekday_and_whole60_minute_horizon_before19utc(self):
        for stamp, expected in (("2026-10-06T05:59:00+00:00", "blocked"),
                                ("2026-10-06T06:00:00+00:00", "signal"),
                                ("2026-10-06T17:59:50+00:00", "signal"),
                                ("2026-10-06T17:59:51+00:00", "blocked"),
                                ("2026-10-06T18:00:00+00:00", "blocked"),
                                ("2026-10-10T12:30:00+00:00", "blocked")):
            clock = datetime.fromisoformat(stamp)
            result = self.analyze(self.feed(now=clock), now=clock)
            self.assertEqual(result["state"], expected, result)

    def test_broker_identity_numeric_metadata_and_nonfinite_inputs_are_rejected(self):
        for identity in (None, "A" * 64, "a" * 63, "z" * 64):
            feed = self.feed()
            feed["risk_context"]["broker_fingerprint"] = identity
            self.assert_waiting(self.analyze(feed), "blocked", "invalid_broker_fingerprint")
        for key, value, reason in (("point", .01, "invalid_execution_grid"),
                                   ("tick_size", .0015, "invalid_execution_grid"),
                                   ("digits", True, "invalid_execution_metadata"),
                                   ("stops_level", -1, "invalid_execution_metadata")):
            feed = self.feed()
            feed["execution"][key] = value
            self.assert_waiting(self.analyze(feed), "invalid", reason)
        for value in (float("nan"), float("inf"), True, "2000"):
            feed = self.feed()
            feed["quote"]["ask"] = value
            self.assert_waiting(self.analyze(feed), "invalid", "invalid_number")

    def test_naive_timestamp_bad_ohlc_and_negative_volume_are_rejected(self):
        feed = self.feed()
        feed["quote"]["time"] = "2026-10-06T12:30:00"
        self.assert_waiting(self.analyze(feed), "invalid", "invalid_timestamp")
        feed = self.feed()
        feed["timeframes"]["M1"][-1]["low"] = 2001
        self.assert_waiting(self.analyze(feed), "invalid", "invalid_ohlc_M1")
        feed = self.feed()
        feed["timeframes"]["M1"][-1]["tick_volume"] = -1
        self.assert_waiting(self.analyze(feed), "invalid", "invalid_tick_volume_M1")

    def test_feed_confidence_or_certificate_never_grants_qualification(self):
        feed = self.feed()
        feed.update(confidence=100, evidence={"qualified": True, "lower": .99, "count": 10000})
        result = self.analyze(feed)
        self.assertEqual(result["state"], "signal", result)
        self.assertNotIn("qualified", result)
        self.assertNotIn("qualification_id", result)
        self.assertFalse(mtf.qualification_allowed(None, now=self.now))
        self.assertFalse(mtf.qualification_allowed(feed["evidence"], now=self.now))

    def test_policy_identity_is_detached_and_hash_ignores_crlf_conversion(self):
        identity = mtf.strategy_identity()
        self.assertEqual(identity["strategy_id"], "mtf-ema-pullback-60m-v2")
        self.assertEqual(identity["policy_id"], "mtf-manual-demo-cost-risk-v2")
        self.assertRegex(identity["fingerprint"], r"^[0-9a-f]{64}$")
        identity["strategy_id"] = "forged"
        self.assertEqual(mtf.strategy_identity()["strategy_id"], mtf.STRATEGY_ID)
        with self.assertRaises(TypeError):
            mtf.POLICY["target_r"] = "100"
        original_read = Path.read_bytes
        baseline = mtf.strategy_identity()["fingerprint"]
        def crlf_read(path):
            return original_read(path).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        mtf._fingerprint.cache_clear()
        try:
            with patch.object(Path, "read_bytes", crlf_read):
                self.assertEqual(mtf.strategy_identity()["fingerprint"], baseline)
        finally:
            mtf._fingerprint.cache_clear()


if __name__ == "__main__":
    unittest.main()
