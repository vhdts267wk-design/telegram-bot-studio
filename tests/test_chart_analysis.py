"""Public compatibility entry points never restore the retired M15 proposal."""

from copy import deepcopy
import unittest

from bot import chart_analysis, mtf_presentation, mtf_runtime, paper_signals
from tests import test_mtf_runtime as fixtures


class ChartAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.feed, self.candidate, self.now = fixtures.synthetic_case()

    def assert_no_trade(self, text):
        self.assertNotRegex(text, r"(?i)\b(?:BUY|SELL)\b")
        self.assertNotIn("SL:", text)
        self.assertNotIn("الدخول المقترح:", text)

    def test_public_names_are_the_three_timeframe_formatters(self):
        self.assertIs(chart_analysis.format_chart_analysis, mtf_presentation.format_analysis)
        self.assertIs(chart_analysis.format_chart_proposal, mtf_presentation.format_proposal)

    def test_legacy_m15_buy_or_sell_never_displays_a_trade(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            for final_close in (2010, 1990):
                bars = deepcopy(self.feed["candles"][-22:])
                for bar in bars[:-1]: bar.update(open=2000, high=2001, low=1999, close=2000)
                bars[-1].update(open=final_close, high=final_close + 1, low=final_close - 1, close=final_close)
                legacy = paper_signals.analyze_paper_signal(bars, self.now)
                self.assertEqual(legacy["state"], "signal", legacy)
                self.assert_no_trade(chart_analysis.format_chart_proposal(legacy, execution_enabled=True))
                self.assert_no_trade(chart_analysis.format_chart_analysis(self.feed, legacy, self.now))

    def test_public_api_requires_qualified_fresh_three_frame_result(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            self.assert_no_trade(chart_analysis.format_chart_proposal(self.candidate))
            result = mtf_runtime.evaluate_feed(self.feed, self.now)
            text = chart_analysis.format_chart_analysis(self.feed, result, self.now)
            self.assertIn("M15 / M5 / M1", text)
            self.assertEqual(text.count("شراء BUY"), 1)
            self.assertIn("موافقتك اليدوية لكل صفقة", text)

    def test_old_m15_only_feed_has_no_actionable_levels(self):
        old = {key: value for key, value in self.feed.items() if key != "timeframes"}
        text = chart_analysis.format_chart_analysis(old, self.candidate, self.now)
        self.assert_no_trade(text)
        self.assertIn("بيانات مكتملة غير متاحة", text)

    def test_observations_only_cannot_claim_unqualified_signal_passed_gate(self):
        with fixtures.pinned_synthetic_evidence(self.feed, self.now):
            text = chart_analysis.format_chart_analysis(self.feed, self.candidate, self.now, include_proposal=False)
            self.assert_no_trade(text)
            self.assertNotIn("اجتازت بوابة الأدلة", text)
            self.assertIn("لا توجد فرصة مؤكدة الشروط", text)


if __name__ == "__main__": unittest.main()
