"""Closed-bar provenance binds the four-frame profile, never volatile quotes."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from unittest.mock import patch

from bridge.mt5_history_contract import FRAMES, closed_history_sha256
from tests import test_multi_timeframe as fixtures


class HistoryContractTests(unittest.TestCase):
    def setUp(self):
        self.feed = fixtures.MultiTimeframeTests().feed()

    def test_four_frame_canonical_hash_uses_closed_ohlcv_and_utc_epochs(self):
        self.assertEqual(FRAMES, ("M1", "M15", "M30", "M5"))
        rows = [[frame, [[int(datetime.fromisoformat(bar["time"]).timestamp()),
                         *[float(bar[key]) for key in ("open", "high", "low", "close")], bar["tick_volume"]]
                        for bar in self.feed["timeframes"][frame]]] for frame in FRAMES]
        expected = hashlib.sha256(json.dumps(rows, separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()
        self.assertEqual(closed_history_sha256(self.feed), expected)
        changed = deepcopy(self.feed)
        changed["quote"]["ask"] += .01
        for bars in changed["timeframes"].values():
            for bar in bars:
                bar["time"] = datetime.fromisoformat(bar["time"]).astimezone(timezone(timedelta(hours=3))).isoformat()
        self.assertEqual(closed_history_sha256(changed), expected)

    def test_server_demo_publication_uses_the_same_actual_m30_history_hash(self):
        from bot import mtf_runtime
        with patch.object(mtf_runtime, "signal_mode", return_value="experimental_demo"), patch.object(
            mtf_runtime, "demo_orders_enabled", return_value=True,
        ):
            result = mtf_runtime.evaluate_feed(self.feed, fixtures.MultiTimeframeTests().now)
        self.assertEqual(result["state"], "signal", result)
        self.assertEqual(result["closed_history_sha256"], closed_history_sha256(self.feed))

    def test_changed_m30_or_m1_history_invalidates_hash_and_old_frame_set_is_rejected(self):
        baseline = closed_history_sha256(self.feed)
        for frame in ("M30", "M1"):
            changed = deepcopy(self.feed)
            changed["timeframes"][frame][-1]["tick_volume"] += 1
            self.assertNotEqual(closed_history_sha256(changed), baseline)
        for legacy in (True, False):
            changed = deepcopy(self.feed)
            if legacy:
                changed["timeframes"]["H4"] = []
            else:
                del changed["timeframes"]["M30"]
            with self.assertRaises(ValueError):
                closed_history_sha256(changed)

    def test_actual_m30_ohlcv_changes_hash_and_prior_h1_contract_is_rejected(self):
        baseline = closed_history_sha256(self.feed)
        for field in ("open", "high", "low", "close", "tick_volume"):
            changed = deepcopy(self.feed)
            changed["timeframes"]["M30"][-1][field] += 1
            with self.subTest(field=field):
                self.assertNotEqual(closed_history_sha256(changed), baseline)
        old = deepcopy(self.feed)
        old["timeframes"]["H1"] = old["timeframes"].pop("M30")
        with self.assertRaises(ValueError):
            closed_history_sha256(old)
