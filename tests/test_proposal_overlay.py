"""Frozen chart prices and eligibility, with no terminal or network calls."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import unittest
import os
from unittest.mock import patch
from uuid import UUID

from bot import mtf_runtime, proposal_overlay
from tests import test_mtf_runtime as evidence_fixtures
from tests import test_multi_timeframe as candle_fixtures

NOW = candle_fixtures.MultiTimeframeTests.now
DEVICE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OFFER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLAIM = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def device():
    return {"device_id": DEVICE, "owner_user_id": 11, "owner_chat_id": 11,
            "symbol": "XAUUSD", "account_mode": "demo", "volume": 0.01, "last_seen_at": NOW}


def feed(*, sell=False):
    value = candle_fixtures.MultiTimeframeTests().feed(now=NOW, sell=sell)
    value["device_id"] = str(DEVICE)
    return value


def offer(status="offered", *, sell=False):
    # The real loader, pin and strategy gate establish synthetic authorization.
    # The synthetic outcomes do not claim measured trading performance.
    candidate = mtf_runtime.evaluate_feed(feed(sell=sell), NOW)
    payload = {**candidate, "workflow": "manual_ticket", "source_identity": f"mt5:XAUUSD:{DEVICE}"}
    value = {
        "id": OFFER, "bot_id": 991, "device_id": DEVICE, "chat_id": 11, "user_id": 11,
        "message_id": 77, "status": status, "created_at": NOW, "published_at": NOW,
        "updated_at": NOW, "expires_at": NOW + timedelta(minutes=5),
        "decided_at": None, "preparing_at": None, "completed_at": None, "claim_id": None, "result": None,
        "payload": payload,
    }
    if status in {"requested", "preparing", "prepared"}:
        value["decided_at"] = NOW
    if status in {"preparing", "prepared"}:
        value.update(preparing_at=NOW, claim_id=CLAIM)
    if status == "prepared":
        value.update(completed_at=NOW, result={"status": "prepared"})
    return value


class ProposalOverlayTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(evidence_fixtures.pinned_synthetic_evidence(feed(), NOW))

    def test_buy_and_sell_keep_original_protections_and_only_public_fields(self):
        for sell in (False, True):
            original = offer(sell=sell)
            frozen = deepcopy(original)
            dto = proposal_overlay.build_chart_overlay(original, device(), feed(sell=sell), NOW)
            payload = original["payload"]
            self.assertEqual(dto, {
                "version": 2, "workflow": "chart_overlay", "offer_id": str(OFFER), "status": "offered",
                "symbol": "XAUUSD", "timeframe": "M1", "direction": payload["direction"],
                **{key: payload[key] for key in ("entry", "entry_zone_low", "entry_zone_high", "stop", "target", "price_digits", "execution", "bar_time",
                                               "strategy_id", "strategy_version", "policy_id", "horizon_seconds", "strategy_fingerprint", "qualification_id",
                                               "direction_bar_time", "confirmation_bar_time")},
                "expires_at": (NOW + timedelta(seconds=10)).isoformat(),
            })
            self.assertEqual(original, frozen)
            self.assertFalse({"claim_id", "device_id", "chat_id", "user_id", "account_mode", "volume"} & dto.keys())

    def test_zone_rounds_inward_on_broker_tick_without_float_drift(self):
        payload = offer()["payload"]
        payload.update(entry=2000.025, stop=1997, target=2006.1, price_digits=3, original_stop_distance=3.025)
        payload["execution"] = {"tick_size": 0.025, "point": 0.001, "digits": 3, "stops_level": 0}
        result = proposal_overlay.reference_zone(payload)
        self.assertEqual(result, {"entry_zone_low": 1999.725, "entry_zone_high": 2000.325})
        for value in result.values():
            self.assertEqual(Decimal(str(value)) % Decimal("0.025"), 0)
        self.assertLess(Decimal(str(result["entry_zone_high"])) - Decimal("2000.025"), Decimal("0.3025"))

    def test_chart_deadline_never_renews_from_fresh_quotes_or_long_queue_expiry(self):
        original = offer()
        later = NOW + timedelta(seconds=4)
        updated = feed()
        updated["as_of"] = later.isoformat()
        updated["quote"]["time"] = later.isoformat()
        updated["risk_context"]["as_of"] = later.isoformat()
        with patch.object(mtf_runtime, "_clock", return_value=later):
            dto = proposal_overlay.build_chart_overlay(original, device(), updated, later)
        self.assertEqual(dto["expires_at"], (NOW + timedelta(seconds=10)).isoformat())
        self.assertEqual(original["expires_at"], NOW + timedelta(minutes=5))

    def test_all_four_lifecycle_states_are_visible_without_authorizing_a_claim(self):
        for status in ("offered", "requested", "preparing", "prepared"):
            with self.subTest(status=status):
                self.assertEqual(proposal_overlay.build_chart_overlay(offer(status), device(), feed(), NOW)["status"], status)

    def test_unpublished_and_terminal_states_are_hidden(self):
        for status in ("draft", "failed", "rejected", "expired", "cancelled", "unknown", "executing"):
            self.assertIsNone(proposal_overlay.build_chart_overlay(offer(status), device(), feed(), NOW))
        for name, value in (("published_at", None), ("published_at", NOW + timedelta(seconds=1)),
                            ("message_id", None), ("message_id", True), ("updated_at", NOW + timedelta(seconds=1))):
            candidate = offer(); candidate[name] = value
            self.assertIsNone(proposal_overlay.build_chart_overlay(candidate, device(), feed(), NOW))

    def test_expiry_is_explicit_even_for_prepared_rows(self):
        for status in ("offered", "requested", "preparing", "prepared"):
            for expiry in (NOW, NOW - timedelta(seconds=1), NOW + timedelta(minutes=5, microseconds=1)):
                candidate = offer(status); candidate["expires_at"] = expiry
                self.assertIsNone(proposal_overlay.build_chart_overlay(candidate, device(), feed(), NOW))

    def test_stale_m1_timing_overrides_longer_preparing_timeout_and_prepared_result_is_checked(self):
        candidate = offer("preparing")
        later = NOW + timedelta(seconds=120)
        current, source = device(), feed()
        current["last_seen_at"] = later
        source["quote"]["time"] = later.isoformat()
        # A preparation may not use stale M1 timing merely because the
        # separate interruption timeout has not elapsed yet.
        self.assertIsNone(proposal_overlay.build_chart_overlay(candidate, current, source, later))
        self.assertIsNone(proposal_overlay.build_chart_overlay(candidate, current, source, later + timedelta(microseconds=1)))
        for result in (None, {"status": "failed"}, {"status": "prepared", "order_ticket": 123}):
            candidate = offer("prepared"); candidate["result"] = result
            self.assertIsNone(proposal_overlay.build_chart_overlay(candidate, device(), feed(), NOW))

    def test_owner_source_device_and_actual_trigger_bar_are_bound(self):
        mutations = (
            ("offer", "user_id", 12), ("offer", "chat_id", -11), ("offer", "device_id", OFFER),
            ("device", "owner_user_id", 12), ("device", "owner_user_id", True), ("device", "active", False),
            ("device", "account_mode", "real"), ("device", "volume", 0.02),
            ("feed", "device_id", str(OFFER)), ("feed", "symbol", "XAUUSD.other"),
            ("payload", "source_identity", "mt5:XAUUSD"), ("payload", "workflow", "automatic"),
            ("payload", "state", "no_signal"), ("payload", "account_mode", "real"), ("payload", "volume", True),
            ("payload", "bar_time", (NOW - timedelta(minutes=14)).isoformat()),
        )
        for where, name, value in mutations:
            row, current, source = offer(), device(), feed()
            target = {"offer": row, "device": current, "feed": source, "payload": row["payload"]}[where]
            target[name] = value
            with self.subTest(where=where, name=name):
                self.assertIsNone(proposal_overlay.build_chart_overlay(row, current, source, NOW))
        source = feed(); source["timeframes"]["M1"].pop()
        self.assertIsNone(proposal_overlay.build_chart_overlay(offer(), device(), source, NOW))

    def test_current_broker_metadata_must_match_immutable_payload(self):
        for name, value in (("tick_size", 0.05), ("point", 0.01), ("digits", 2), ("stops_level", 21)):
            source = feed(); source["execution"][name] = value
            self.assertIsNone(proposal_overlay.build_chart_overlay(offer(), device(), source, NOW))

    def test_invalid_or_off_grid_frozen_numbers_never_reach_chart(self):
        mutations = (("entry", True), ("entry", float("nan")), ("stop", float("inf")),
                     ("target", "2020"), ("entry", 2000.001), ("stop", 1990.001), ("target", 2020.001),
                     ("stop", 2000), ("target", 1999), ("original_stop_distance", 9.9),
                     ("max_drift_r", 0.2), ("price_digits", True))
        for name, value in mutations:
            row = offer(); row["payload"][name] = value
            with self.subTest(name=name, value=value):
                self.assertIsNone(proposal_overlay.build_chart_overlay(row, device(), feed(), NOW))

    def test_reference_zone_cannot_touch_or_cross_protective_prices(self):
        payload = offer()["payload"]
        payload["target"] = payload["entry_zone_high"]
        with self.assertRaises(ValueError):
            proposal_overlay.reference_zone(payload)
        payload = offer()["payload"]
        payload.update(stop=1999.99, original_stop_distance=0.01)
        with self.assertRaises(ValueError):
            proposal_overlay.reference_zone(payload)

    def test_allowed_price_drift_keeps_frozen_levels_and_excess_drift_clears(self):
        original = offer()
        frozen = deepcopy(original)
        source = feed(); source["quote"].update(bid=2000.011, ask=2000.013)
        dto = proposal_overlay.build_chart_overlay(original, device(), source, NOW)
        self.assertEqual((dto["entry"], dto["stop"], dto["target"]),
                         tuple(original["payload"][key] for key in ("entry", "stop", "target")))
        self.assertEqual(original, frozen)
        source["quote"].update(bid=2010, ask=2010.1)
        self.assertIsNone(proposal_overlay.build_chart_overlay(original, device(), source, NOW))

    def test_quote_skew_and_device_freshness_have_independent_bounds(self):
        for delta, allowed in ((-5, True), (-6, False), (10, True), (11, False)):
            source = feed(); source["quote"]["time"] = (NOW - timedelta(seconds=delta)).isoformat()
            self.assertEqual(proposal_overlay.build_chart_overlay(offer(), device(), source, NOW) is not None, allowed)
        for delta, allowed in ((0, True), (180, True), (181, False), (-1, False)):
            current = device(); current["last_seen_at"] = NOW - timedelta(seconds=delta)
            self.assertEqual(proposal_overlay.build_chart_overlay(offer(), current, feed(), NOW) is not None, allowed)

    def test_legacy_or_unpinned_payload_never_displays_despite_fresh_chart_prices(self):
        original = offer()
        for field, value in (("strategy_id", "ema9-21-atr14-v1"), ("display_timeframe", "M15"),
                             ("qualification_id", "b" * 64), ("provisional", True)):
            changed = deepcopy(original)
            changed["payload"][field] = value
            with self.subTest(field=field):
                self.assertIsNone(proposal_overlay.build_chart_overlay(changed, device(), feed(), NOW))
        with patch.dict(os.environ, {"MT5_EVIDENCE_SHA256": ""}):
            self.assertIsNone(proposal_overlay.build_chart_overlay(original, device(), feed(), NOW))
