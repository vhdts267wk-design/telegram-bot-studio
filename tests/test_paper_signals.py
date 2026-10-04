from datetime import datetime, timedelta, timezone
import json
import math
import unittest

from bot import paper_signals as paper


class PaperSignalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

    def candles(self, closes=None, *, spread=1.0):
        closes = [100.0] * 21 + [110.0] if closes is None else closes
        start = self.now - timedelta(minutes=15 * len(closes))
        return [
            {
                "time": (start + timedelta(minutes=15 * index)).isoformat().replace("+00:00", "Z"),
                "open": close, "high": close + spread,
                "low": close - spread, "close": close,
            }
            for index, close in enumerate(closes)
        ]

    def analyze(self, candles=None, *, now=None):
        return paper.analyze_paper_signal(
            self.candles() if candles is None else candles,
            self.now if now is None else now,
        )

    def test_buy_cross_uses_sma_seed_and_wilder_atr_levels(self):
        result = self.analyze()
        self.assertEqual(result["state"], "signal")
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["strategy_id"], "ema9-21-atr14-v1")
        self.assertEqual(result["bar_time"], "2026-10-04T11:45:00Z")
        self.assertEqual(result["previous_ema_fast"], 100.0)
        self.assertEqual(result["previous_ema_slow"], 100.0)
        self.assertEqual(result["ema_fast"], 102.0)
        self.assertAlmostEqual(result["ema_slow"], 100 + 10 / 11, places=7)
        # First 14 TR values are 2. The last TR is 11: ATR=(2*13+11)/14.
        self.assertAlmostEqual(result["atr"], 37 / 14, places=7)
        self.assertEqual((result["entry"], result["stop"], result["target"]), (110.0, 106.04, 117.93))
        self.assertEqual(result["reward_risk"], 2.0)
        self.assertEqual(result["stop_atr_multiple"], 1.5)
        self.assertEqual(result["target_atr_multiple"], 3.0)
        json.dumps(result, allow_nan=False)

    def test_sell_cross_is_inverse_and_levels_keep_direction(self):
        result = self.analyze(self.candles([100.0] * 21 + [90.0]))
        self.assertEqual(result["state"], "signal")
        self.assertEqual(result["direction"], "SELL")
        self.assertEqual((result["entry"], result["stop"], result["target"]), (90.0, 93.96, 82.07))
        self.assertLess(result["target"], result["entry"])
        self.assertLess(result["entry"], result["stop"])

    def test_existing_trend_does_not_force_another_signal(self):
        candles = self.candles([100.0] * 21 + [110.0, 111.0])
        result = self.analyze(candles)
        self.assertEqual(result["state"], "no_signal")
        self.assertEqual(result["reason"], "no_crossover")
        self.assertNotIn("entry", result)
        self.assertNotIn("direction", result)

    def test_flat_prices_and_zero_atr_suppress_signal(self):
        result = self.analyze(self.candles([100.0] * 22, spread=0.0))
        self.assertEqual(result["state"], "no_signal")
        self.assertEqual(result["reason"], "zero_atr")
        self.assertEqual(result["atr"], 0)
        self.assertNotIn("entry", result)

    def test_flat_ema_with_nonzero_range_is_still_not_a_cross(self):
        result = self.analyze(self.candles([100.0] * 22))
        self.assertEqual(result["state"], "no_signal")
        self.assertEqual(result["reason"], "no_crossover")

    def test_insufficient_history_reports_exact_warmup(self):
        for count in (0, 1, 14, 20, 21):
            with self.subTest(count=count):
                result = self.analyze(self.candles([100.0] * count))
                self.assertEqual(result["state"], "warmup")
                self.assertEqual(result["candle_count"], count)
                self.assertEqual(result["required_bars"], 22)
                self.assertEqual(result["remaining_bars"], 22 - count)
                self.assertNotIn("entry", result)

    def test_last_closed_bar_is_stale_after_twenty_minutes(self):
        self.assertEqual(self.analyze(now=self.now + timedelta(minutes=20))["state"], "signal")
        result = self.analyze(now=self.now + timedelta(minutes=20, seconds=1))
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["reason"], "stale_bars")
        self.assertEqual(result["bar_time"], "2026-10-04T11:45:00Z")
        self.assertNotIn("entry", result)

    def test_forming_bar_and_future_bar_are_never_analyzed(self):
        for stamp in (self.now, self.now + timedelta(minutes=15)):
            with self.subTest(stamp=stamp):
                candles = self.candles()
                candles[-1]["time"] = stamp.isoformat().replace("+00:00", "Z")
                result = self.analyze(candles)
                self.assertEqual(result["state"], "invalid")
                self.assertEqual(result["reason"], "forming_bar")

    def test_bad_non_utc_fractional_or_misaligned_timestamps_are_rejected(self):
        for stamp in (
            "2026-10-04T11:45:00", "2026-10-04T14:45:00+03:00",
            "2026-10-04T11:45:00.001Z", "2026-10-04T11:46:00Z",
            "private-malformed-time", None, True,
        ):
            with self.subTest(stamp=stamp):
                candles = self.candles()
                candles[-1]["time"] = stamp
                result = self.analyze(candles)
                self.assertEqual(result["state"], "invalid")
                self.assertNotIn("private", result["details"])

    def test_gaps_duplicates_and_reversed_order_are_rejected(self):
        candles = self.candles()
        for data in (candles[:10] + candles[11:], candles[:-1] + [candles[-2]], list(reversed(candles))):
            with self.subTest(data=data):
                result = self.analyze(data)
                self.assertEqual(result["state"], "invalid")
                self.assertEqual(result["reason"], "non_contiguous")

    def test_inadequate_sampled_coverage_is_rejected(self):
        for change in ({"coverage_ok": False}, {"coverage_ok": 1},
                       {"sampled": True}, {"sampled": True, "coverage_ok": False}):
            with self.subTest(change=change):
                candles = self.candles()
                candles[-1].update(change)
                self.assertEqual(self.analyze(candles)["reason"], "insufficient_coverage")
        candles = self.candles()
        for candle in candles:
            candle.update(sampled=True, coverage_ok=True)
        self.assertEqual(self.analyze(candles)["state"], "signal")

    def test_invalid_bool_nonfinite_huge_zero_or_negative_prices_are_rejected(self):
        for price in (None, True, "110", math.nan, math.inf, 0, -1, 100_000_001):
            for field in ("open", "high", "low", "close"):
                with self.subTest(price=price, field=field):
                    candles = self.candles()
                    candles[-1][field] = price
                    self.assertEqual(self.analyze(candles)["state"], "invalid")

    def test_ohlc_range_and_missing_candle_fields_are_rejected(self):
        for change in ({"high": 109.0}, {"low": 111.0}, {"open": 112.0}):
            with self.subTest(change=change):
                candles = self.candles()
                candles[-1].update(change)
                self.assertEqual(self.analyze(candles)["state"], "invalid")
        candles = self.candles()
        del candles[-1]["close"]
        self.assertEqual(self.analyze(candles)["state"], "invalid")
        self.assertEqual(self.analyze([None])["state"], "invalid")
        self.assertEqual(paper.analyze_paper_signal({}, self.now)["state"], "invalid")

    def test_tiny_atr_cannot_produce_identical_rounded_levels(self):
        candles = self.candles([100.0] * 21 + [100.00001], spread=0.00001)
        result = self.analyze(candles)
        self.assertEqual(result["state"], "no_signal")
        self.assertEqual(result["reason"], "rounded_levels_collapsed")
        self.assertNotIn("entry", result)

    def test_atr_that_would_make_buy_stop_negative_suppresses_setup(self):
        candles = self.candles([1.0] * 21 + [2.0], spread=0.1)
        for candle in candles:
            candle["high"] = 100.0
        result = self.analyze(candles)
        self.assertEqual(result["state"], "no_signal")
        self.assertEqual(result["reason"], "invalid_levels")
        self.assertNotIn("stop", result)

    def test_non_utc_aware_clock_normalizes_but_naive_clock_is_invalid(self):
        local_now = self.now.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(self.analyze(now=local_now), self.analyze())
        self.assertEqual(self.analyze(now=self.now.replace(tzinfo=None))["state"], "invalid")

    def test_source_bar_identity_and_result_are_deterministic_and_serializable(self):
        first, second = self.analyze(), self.analyze()
        self.assertEqual(first, second)
        restored = json.loads(json.dumps(first, allow_nan=False))
        self.assertEqual(restored, first)
        self.assertEqual(restored["strategy_id"], paper.STRATEGY_ID)
        self.assertEqual(restored["bar_time"], self.candles()[-1]["time"])

    def test_arabic_report_declares_paper_reference_and_execution_assumptions(self):
        report = paper.format_paper_signal(self.analyze(), source="GoldAPI reference prices")
        for phrase in ("اختبار ورقي فقط", "BUY", "110.00", "106.04", "117.93",
                       "EMA9/21", "ATR14", "UTC", "مرجعي", "السبريد", "الانزلاق",
                       "العمولات", "بلا أوامر حقيقية", "بلا ادعاء نتائج تاريخية", "قبل التقريب"):
            self.assertIn(phrase, report)
        self.assertIn("GoldAPI reference prices", report)

    def test_waiting_reports_do_not_invent_trade_levels_or_profit_claims(self):
        for result in (
            self.analyze(self.candles([100.0] * 5)),
            self.analyze(self.candles([100.0] * 22)),
            self.analyze(now=self.now + timedelta(hours=1)),
            self.analyze([{}]),
        ):
            with self.subTest(state=result["state"]):
                report = paper.format_paper_signal(result)
                self.assertIn("اختبار ورقي فقط", report)
                self.assertNotIn("دخول مرجعي:", report)
                self.assertNotIn("BUY", report)
                self.assertNotIn("SELL", report)
        self.assertIn("5.5", paper.format_paper_signal(self.analyze([])))


if __name__ == "__main__":
    unittest.main()
