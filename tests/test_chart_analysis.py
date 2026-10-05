from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from bot import chart_analysis, paper_signals


class ChartAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def feed(self, closes=None):
        closes = [2700.0] * 21 + [2710.0] if closes is None else closes
        start = self.now - timedelta(minutes=15 * len(closes))
        bars = [
            {
                "time": (start + timedelta(minutes=15 * index)).isoformat(),
                "open": close, "high": close + 1, "low": close - 1,
                "close": close, "tick_volume": 123,
            }
            for index, close in enumerate(closes)
        ]
        return {
            "symbol": "XAUUSD", "timeframe": "M15", "source": "MetaTrader 5",
            "quote": {"bid": closes[-1], "ask": closes[-1] + 0.2, "time": self.now.isoformat()},
            "candles": bars,
        }

    def report(self, feed=None, *, result=None, now=None, **options):
        feed = self.feed() if feed is None else feed
        result = paper_signals.analyze_paper_signal(feed["candles"], self.now) if result is None else result
        return chart_analysis.format_chart_analysis(feed, result, self.now if now is None else now, **options)

    def test_buy_proposal_reuses_real_cross_and_existing_atr_levels(self):
        feed = self.feed()
        before = deepcopy(feed)
        result = paper_signals.analyze_paper_signal(feed["candles"], self.now)
        report = self.report(feed, result=result)
        for text in (
            "XAUUSD M15", "MT5", "تجريبي", "صاعد", "شراء BUY", "EMA9/21", "ATR14",
            f"دخول مرجعي {result['entry']:.2f}", f"وقف {result['stop']:.2f}",
            f"هدف {result['target']:.2f}", "دعم محتمل 2699.00", "مقاومة محتملة 2711.00",
            "آخر 4 ساعات", "موافقتك", "قراءة احتمالية", "UTC",
        ):
            self.assertIn(text, report)
        self.assertNotIn("http", report)
        self.assertEqual(feed, before)

    def test_sell_proposal_has_inverse_trend_and_preserves_accepted_levels(self):
        feed = self.feed([2700.0] * 21 + [2690.0])
        result = paper_signals.analyze_paper_signal(feed["candles"], self.now)
        report = self.report(feed, result=result)
        self.assertIn("هابط", report)
        self.assertIn("بيع SELL", report)
        self.assertIn(f"وقف {result['stop']:.2f}", report)
        self.assertIn(f"هدف {result['target']:.2f}", report)
        self.assertIn("دعم محتمل 2689.00", report)
        self.assertIn("مقاومة محتملة 2701.00", report)

    def test_continuing_trend_explains_waiting_without_inventing_new_entry(self):
        feed = self.feed([2700.0] * 21 + [2710.0, 2711.0])
        result = paper_signals.analyze_paper_signal(feed["candles"], self.now)
        self.assertEqual(result["state"], "no_signal")
        report = self.report(feed, result=result)
        self.assertIn("صاعد", report)
        self.assertIn("الإغلاق ارتفع بمقدار 1.00", report)
        self.assertIn("لم يتكوّن تقاطع جديد", report)
        for text in ("دخول مرجعي", "اقتراح شراء", "اقتراح بيع"):
            self.assertNotIn(text, report)
        self.assertNotRegex(report, r"(?:وقف|هدف) \d")

    def test_quote_receipt_and_closed_bar_staleness_suppress_all_trade_levels(self):
        cases = []
        stale_quote = self.feed()
        stale_quote["quote"]["time"] = (self.now - timedelta(seconds=181)).isoformat()
        cases.append((stale_quote, {}))
        future_quote = self.feed()
        future_quote["quote"]["time"] = (self.now + timedelta(seconds=6)).isoformat()
        cases.append((future_quote, {}))
        cases.append((self.feed(), {"received_at": self.now - timedelta(seconds=181)}))
        cases.append((self.feed(), {"received_at": self.now + timedelta(seconds=1)}))
        old_bars = self.feed()
        for candle in old_bars["candles"]:
            candle["time"] = (datetime.fromisoformat(candle["time"]) - timedelta(minutes=30)).isoformat()
        cases.append((old_bars, {}))
        for feed, options in cases:
            with self.subTest(options=options, quote=feed["quote"]["time"]):
                report = self.report(feed, **options)
                self.assertIn("ننتظر", report)
                for text in ("دخول مرجعي", "اقتراح شراء", "اقتراح بيع", "EMA9 "):
                    self.assertNotIn(text, report)

    def test_missing_source_history_and_warmup_explain_waiting(self):
        report = chart_analysis.format_chart_analysis(None, {"state": "warmup"}, self.now)
        self.assertIn("غير متصل", report)
        report = self.report(self.feed([2700.0] * 5))
        self.assertIn("5/22", report)
        self.assertIn("السجل غير كافٍ", report)
        self.assertNotIn("دخول مرجعي", report)
        self.assertNotIn("الاتجاه حسب", report)
        feed = self.feed()
        feed["candles"] = []
        self.assertIn("غير صالحة", self.report(feed, result={"state": "warmup"}))

    def test_observed_levels_exclude_older_bars_outside_four_hour_window(self):
        feed = self.feed()
        feed["candles"][0]["high"] = 2900.0
        feed["candles"][0]["low"] = 2500.0
        report = self.report(feed)
        self.assertIn("دعم محتمل 2699.00", report)
        self.assertIn("مقاومة محتملة 2711.00", report)
        self.assertNotIn("دعم محتمل 2500.00", report)
        self.assertNotIn("مقاومة محتملة 2900.00", report)

    def test_gap_restarts_history_instead_of_filling_missing_candles(self):
        feed = self.feed()
        del feed["candles"][10]
        report = self.report(feed, result={"state": "warmup"})
        self.assertIn("11/22", report)
        self.assertNotIn("دخول مرجعي", report)

    def test_bad_or_forming_candle_does_not_get_chart_indicators(self):
        for field, value in (("high", 2690.0), ("time", self.now.isoformat()), ("close", float("nan"))):
            feed = self.feed()
            feed["candles"][-1][field] = value
            report = self.report(feed, result={"state": "signal"})
            self.assertIn("غير صالحة", report)
            self.assertNotIn("الاتجاه حسب", report)
            self.assertNotIn("دخول مرجعي", report)

    def test_invalid_stale_or_mismatched_accepted_signal_cannot_make_a_proposal(self):
        feed = self.feed()
        original = paper_signals.analyze_paper_signal(feed["candles"], self.now)
        for change in (
            {"bar_time": feed["candles"][-2]["time"]}, {"stop": 2800.0},
            {"entry": 9999.0}, {"direction": "unknown"},
            {"target": True}, {"strategy_id": "unknown"}, {"state": "stale"}, {"state": "invalid"},
        ):
            with self.subTest(change=change):
                report = self.report(feed, result={**original, **change})
                self.assertIn("ننتظر", report)
                self.assertNotIn("دخول مرجعي", report)
                self.assertNotIn("اقتراح شراء", report)

    def test_chart_only_output_keeps_analysis_and_waiting_but_omits_proposal_levels(self):
        report = self.report(include_proposal=False)
        self.assertIn("تكوّن تقاطع جديد", report)
        self.assertIn("دعم محتمل", report)
        self.assertNotIn("دخول مرجعي", report)
        self.assertNotIn("BUY", report)
        self.assertNotIn("SELL", report)
        waiting = self.report(self.feed([2700.0] * 22), include_proposal=False)
        self.assertIn("لم يتكوّن تقاطع جديد", waiting)

    def test_flat_candles_state_why_no_valid_atr_trade_exists(self):
        feed = self.feed([2700.0] * 22)
        for bar in feed["candles"]:
            bar["high"] = bar["low"] = bar["close"]
        report = self.report(feed)
        self.assertIn("الحركة شبه معدومة", report)
        self.assertIn("ATR14: 0.00", report)
        self.assertNotIn("دخول مرجعي", report)


class ChartProposalTests(unittest.TestCase):
    def result(self):
        now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        closes = [2700.0] * 21 + [2710.0]
        start = now - timedelta(minutes=15 * len(closes))
        bars = [
            {
                "time": (start + timedelta(minutes=15 * index)).isoformat(),
                "open": close, "high": close + 1, "low": close - 1, "close": close,
            }
            for index, close in enumerate(closes)
        ]
        return paper_signals.analyze_paper_signal(bars, now)

    def test_demo_report_keeps_reference_levels_and_requires_offered_human_approval(self):
        result = self.result()
        before = deepcopy(result)
        report = chart_analysis.format_chart_proposal(result, symbol="GOLD.r", execution_enabled=True)
        for text in (
            "اقتراح صفقة تجريبي", "GOLD.r M15", "MT5", "شراء BUY", "EMA9/21", "ATR14",
            f"الدخول المرجعي: {result['entry']:.2f}", f"الوقف: {result['stop']:.2f}",
            f"الهدف: {result['target']:.2f}", "2026-10-05 11:45 UTC", "1.5×ATR", "3×ATR",
            "تنفيذ Demo فقط", "طلب قابل للتنفيذ وقبولك", "قد يصل السعر إلى الوقف",
        ):
            self.assertIn(text, report)
        for text in ("بلا أوامر حقيقية", "GoldAPI", "http", "دعم محتمل", "السعر الحالي: Bid"):
            self.assertNotIn(text, report)
        self.assertEqual(result, before)

    def test_paper_report_does_not_claim_demo_execution(self):
        report = chart_analysis.format_chart_proposal(self.result())
        self.assertIn("اختبار ورقي", report)
        self.assertIn("لا يُرسل أمر تداول", report)
        self.assertNotIn("تنفيذ Demo", report)

    def test_waiting_states_explain_actual_reason_and_never_make_levels(self):
        cases = (
            ({"state": "warmup", "candle_count": 5}, "5/22"),
            ({"state": "stale"}, "الشموع قديمة"),
            ({"state": "invalid"}, "غير صالحة"),
            ({"state": "no_signal", "reason": "zero_atr"}, "الحركة شبه معدومة"),
            ({"state": "no_signal", "reason": "no_crossover"}, "لم يتكوّن تقاطع جديد"),
            ({"state": "no_signal", "reason": "rounded_levels_collapsed"}, "غير صالحة بعد التقريب"),
        )
        for result, reason in cases:
            with self.subTest(result=result):
                report = chart_analysis.format_chart_proposal(result, execution_enabled=True)
                self.assertIn(reason, report)
                self.assertNotIn("الدخول المرجعي:", report)
                self.assertNotIn("شراء BUY", report)
                self.assertNotIn("بيع SELL", report)
                self.assertNotIn("تنفيذ Demo", report)

    def test_invalid_missing_or_inverted_setup_cannot_present_an_actionable_proposal(self):
        original = self.result()
        for change in (
            {"entry": float("nan")}, {"stop": True}, {"target": 0},
            {"direction": "SELL"}, {"strategy_id": "unknown"},
            {"bar_time": "bad-time"}, {"atr": 0},
        ):
            with self.subTest(change=change):
                report = chart_analysis.format_chart_proposal({**original, **change}, execution_enabled=True)
                self.assertIn("غير صالحة", report)
                self.assertNotIn("الدخول المرجعي:", report)
                self.assertNotIn("شراء BUY", report)
                self.assertNotIn("تنفيذ Demo", report)
        missing = self.result()
        del missing["target"]
        self.assertIn("غير صالحة", chart_analysis.format_chart_proposal(missing))
        self.assertIn("غير صالحة", chart_analysis.format_chart_proposal(None))


if __name__ == "__main__":
    unittest.main()
