"""Real historical warmup and rolling closed snapshots, without market I/O."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from bot import multi_timeframe as mtf
from bot import mtf_presentation
from bridge import mt5_chart_overlay as chart
from bridge import mt5_market_bridge as market
from tests import test_multi_timeframe as fixtures


def preload_feed(now=None):
    now = now or datetime(2026, 10, 14, 23, 6, tzinfo=timezone.utc)
    feed = fixtures.MultiTimeframeTests().feed(now=now)
    # Actual-bar fixture with a two-hour daily gap. No calendar is inferred,
    # no candle is filled, and every real OHLCV record retains its timestamp.
    for frame, seconds in mtf.TIMEFRAME_SECONDS.items():
        boundary = market.latest_closed_bar(now.timestamp(), seconds, 180)
        candidate = datetime.fromtimestamp(boundary, timezone.utc)
        stamps = []
        while len(stamps) < 64:
            broker_second = (int(candidate.timestamp()) + 180 * 60) % 86400
            if 3600 <= broker_second < 82800:
                stamps.append(candidate.isoformat())
            candidate -= timedelta(seconds=seconds)
        for row, stamp in zip(feed["timeframes"][frame], reversed(stamps)):
            row["time"] = stamp
    feed["candles"] = deepcopy(feed["timeframes"]["M15"])
    return feed, now


class SessionWarmupIntegrationTests(unittest.TestCase):
    def test_startup_preloads_64_real_closed_bars_and_keeps_risk_session_guard(self):
        feed, now = preload_feed()
        before = deepcopy(feed)
        result = mtf.analyze_multi_timeframe(feed, now)
        self.assertNotEqual(result["state"], "warmup", result)
        self.assertEqual(result["required_bars"], 22)
        self.assertEqual(result["usable_counts"], dict.fromkeys(mtf.TIMEFRAME_SECONDS, 64))
        self.assertEqual(result["contiguous_counts"], {"M15": 4, "M5": 13, "M1": 64, "M30": 2})
        self.assertEqual(chart.feed_readiness(feed, symbol="XAUUSD", broker_offset_minutes=180,
                                              observed_at=now)[0], [64] * 4)
        self.assertEqual(chart.current_timeframe_context(feed, observed_at=now, broker_offset_minutes=180),
                         result["timeframe_context"])
        self.assertEqual(feed, before)
        self.assertNotIn("quote_sessions", feed)
        self.assertEqual((result["state"], result["reason"]), ("blocked", "outside_full_horizon_session"))
        self.assertNotIn("entry", result)

    def test_usable_counts_never_fabricate_a_missing_historical_record(self):
        for frame in mtf.TIMEFRAME_SECONDS:
            with self.subTest(frame=frame):
                feed, now = preload_feed()
                del feed["timeframes"][frame][-2]
                before = deepcopy(feed)
                result = mtf.analyze_multi_timeframe(feed, now)
                self.assertNotEqual(result["state"], "warmup", result)
                self.assertEqual(result["usable_counts"][frame], 63)
                self.assertEqual(result["contiguous_counts"][frame], 1)
                counts, _ = chart.feed_readiness(feed, symbol="XAUUSD", broker_offset_minutes=180,
                                                  observed_at=now)
                self.assertEqual(counts[list(chart.FRAME_SECONDS).index(frame)], 63)
                self.assertEqual(chart.current_timeframe_context(feed, observed_at=now, broker_offset_minutes=180),
                                 result["timeframe_context"])
                self.assertEqual(feed, before)

    def test_twentyone_actual_bars_still_need_history_in_every_frame(self):
        for frame in mtf.TIMEFRAME_SECONDS:
            with self.subTest(frame=frame):
                feed, now = preload_feed()
                feed["timeframes"][frame] = feed["timeframes"][frame][-21:]
                result = mtf.analyze_multi_timeframe(feed, now)
                self.assertEqual((result["state"], result["reason"]), ("warmup", "insufficient_closed_history"))
                self.assertEqual(result["usable_counts"][frame], 21)
                with self.assertRaises(chart.OverlayError):
                    chart.current_timeframe_context(feed, observed_at=now, broker_offset_minutes=180)
        feed, now = preload_feed()
        del feed["timeframes"]["M1"]
        self.assertEqual(mtf.analyze_multi_timeframe(feed, now)["reason"], "missing_timeframes")

    def test_preloaded_history_keeps_refreshing_with_new_closed_candles(self):
        previous_latest = {}
        for elapsed in (0, 60, 300, 1800):
            feed, now = preload_feed(datetime(2026, 10, 14, 23, 6, tzinfo=timezone.utc) + timedelta(seconds=elapsed))
            self.assertEqual(chart.feed_readiness(feed, symbol="XAUUSD", broker_offset_minutes=180,
                                                  observed_at=now)[0], [64] * 4)
            result = mtf.analyze_multi_timeframe(feed, now)
            self.assertNotEqual(result["state"], "warmup", result)
            self.assertEqual(result["usable_counts"], dict.fromkeys(mtf.TIMEFRAME_SECONDS, 64))
            self.assertEqual(chart.current_timeframe_context(feed, observed_at=now, broker_offset_minutes=180),
                             result["timeframe_context"])
            for frame, seconds in mtf.TIMEFRAME_SECONDS.items():
                latest = feed["timeframes"][frame][-1]["time"]
                expected = datetime.fromtimestamp(market.latest_closed_bar(now.timestamp(), seconds, 180),
                                                  timezone.utc).isoformat()
                self.assertEqual(latest, expected)
                if frame in previous_latest:
                    self.assertGreaterEqual(latest, previous_latest[frame])
                    if frame == "M1":
                        self.assertGreater(latest, previous_latest[frame])
                previous_latest[frame] = latest
            display = mtf_presentation.format_analysis(feed, result, now)
            self.assertNotIn("insufficient_closed_history", display)

    def test_forming_duplicate_and_stale_latest_history_do_not_become_usable(self):
        feed, now = preload_feed()
        feed["timeframes"]["M1"][-1]["time"] = now.isoformat()
        self.assertEqual(mtf.analyze_multi_timeframe(feed, now)["reason"], "forming_bar_M1")
        with self.assertRaises(chart.OverlayError):
            chart.feed_readiness(feed, symbol="XAUUSD", broker_offset_minutes=180, observed_at=now)
        for frame in mtf.TIMEFRAME_SECONDS:
            with self.subTest(frame=frame):
                feed, now = preload_feed()
                feed["timeframes"][frame][-2]["time"] = feed["timeframes"][frame][-1]["time"]
                self.assertEqual(mtf.analyze_multi_timeframe(feed, now)["reason"], "non_monotonic_" + frame)
                with self.assertRaises(chart.OverlayError):
                    chart.feed_readiness(feed, symbol="XAUUSD", broker_offset_minutes=180, observed_at=now)
                feed, now = preload_feed()
                del feed["timeframes"][frame][-(2 if frame == "M1" else 1):]
                reason = "stale_m1_timing" if frame == "M1" else "stale_" + frame
                self.assertEqual(mtf.analyze_multi_timeframe(feed, now)["reason"], reason)
                with self.assertRaises(chart.OverlayError):
                    chart.feed_readiness(feed, symbol="XAUUSD", broker_offset_minutes=180, observed_at=now)

    def test_historical_preload_keeps_existing_exposure_and_unverified_costs_blocked(self):
        now = datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)
        feed, _ = preload_feed(now)
        feed["risk_context"]["open_positions"] = 1
        result = mtf.analyze_multi_timeframe(feed, now)
        self.assertEqual(result["reason"], "existing_exposure")
        self.assertEqual(result["usable_counts"], dict.fromkeys(mtf.TIMEFRAME_SECONDS, 64))
        self.assertNotIn("entry", result)
        feed, _ = preload_feed(now)
        feed["risk_context"]["costs_verified"] = False
        result = mtf.analyze_multi_timeframe(feed, now)
        self.assertEqual(result["reason"], "unverified_costs")
        self.assertNotIn("entry", result)


if __name__ == "__main__":
    unittest.main()
