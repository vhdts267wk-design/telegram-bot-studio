from datetime import datetime, timedelta, timezone
import math
from unittest.mock import AsyncMock, Mock, patch
import unittest

import httpx

from bot import reference_market as market


class ReferenceClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        self.requests = []
        self.data = {"symbol": "XAU", "price": 2502.25, "updatedAt": "2026-10-04T11:59:00Z"}

    def transport(self, data=None, status=200):
        def respond(request):
            self.requests.append(request)
            return httpx.Response(status, json=self.data if data is None else data)
        return httpx.MockTransport(respond)

    async def fetch(self, data=None, status=200):
        async with httpx.AsyncClient(transport=self.transport(data, status)) as client:
            return await market.ReferenceClient(client).fetch_quote(self.now)

    async def test_quote_uses_fixed_keyless_endpoint_and_original_utc_time(self):
        quote = await self.fetch()
        self.assertEqual(quote.price, 2502.25)
        self.assertEqual(quote.as_of, self.now - timedelta(minutes=1))
        self.assertEqual(quote.as_of.tzinfo, timezone.utc)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(str(self.requests[0].url), "https://api.gold-api.com/price/XAU")
        self.assertEqual(self.requests[0].method, "GET")
        self.assertNotIn("authorization", self.requests[0].headers)
        self.assertNotIn("x-api-key", self.requests[0].headers)
        self.assertEqual(self.requests[0].extensions["timeout"]["read"], 12.0)
        self.assertFalse(hasattr(quote, "bid"))
        self.assertFalse(hasattr(quote, "ask"))

    async def test_repeated_stale_reference_quote_never_becomes_fresh(self):
        self.data["updatedAt"] = "2026-10-02T12:00:00Z"
        async with httpx.AsyncClient(transport=self.transport()) as client:
            reference = market.ReferenceClient(client)
            first = await reference.fetch_quote(self.now)
            later = await reference.fetch_quote(self.now + timedelta(hours=1))
        self.assertEqual(first, later)
        self.assertEqual(first.as_of, self.now - timedelta(days=2))
        self.assertEqual(len(self.requests), 2)

    async def test_invalid_symbol_currency_price_or_timestamp_is_unavailable(self):
        for change in (
            {"symbol": "XAG"}, {"symbol": "GC"}, {"currency": "EUR"},
            {"price": None}, {"price": "2502"}, {"price": True}, {"price": 0},
            {"price": -1}, {"updatedAt": None}, {"updatedAt": "2026-10-04T11:59:00"},
            {"updatedAt": "2026-10-04T14:59:00+03:00"},
            {"updatedAt": "2026-10-04T12:00:31Z"}, {"updatedAt": "private-error-marker"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(market.ReferenceUnavailable) as error:
                    await self.fetch({**self.data, **change})
                self.assertEqual(str(error.exception), "Reference gold quotes are unavailable.")
                self.assertIsNone(error.exception.__cause__)
        for data in ([], {}, {"symbol": "XAU", "updatedAt": self.data["updatedAt"]}):
            with self.subTest(data=data):
                with self.assertRaises(market.ReferenceUnavailable):
                    await self.fetch(data)

    async def test_nonfinite_prices_and_invalid_json_are_rejected(self):
        for raw_body in (
            '{"symbol":"XAU","price":NaN,"updatedAt":"2026-10-04T11:59:00Z"}',
            '{"symbol":"XAU","price":Infinity,"updatedAt":"2026-10-04T11:59:00Z"}',
            "private upstream response that is not JSON",
        ):
            with self.subTest(raw_body=raw_body):
                transport = httpx.MockTransport(lambda req: httpx.Response(200, text=raw_body))
                async with httpx.AsyncClient(transport=transport) as client:
                    with self.assertRaises(market.ReferenceUnavailable) as error:
                        await market.ReferenceClient(client).fetch_quote(self.now)
                self.assertNotIn(raw_body, str(error.exception))

    async def test_http_error_and_redirect_are_not_retried_or_followed(self):
        for status in (301, 401, 429, 500):
            with self.subTest(status=status):
                self.requests.clear()
                def respond(req):
                    self.requests.append(req)
                    return httpx.Response(status, text="private provider response", headers={
                        "Location": "https://elsewhere.invalid/private"
                    })
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
                    with self.assertRaises(market.ReferenceUnavailable) as error:
                        await market.ReferenceClient(client).fetch_quote(self.now)
                self.assertEqual(len(self.requests), 1)
                self.assertNotIn("private", str(error.exception))
                self.assertIsNone(error.exception.__cause__)

    async def test_connection_timeout_is_private_with_one_attempt(self):
        requests = []
        def respond(req):
            requests.append(req)
            raise httpx.ConnectTimeout("private-key https://private.invalid/error")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(market.ReferenceUnavailable) as error:
                await market.ReferenceClient(client).fetch_quote(self.now)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(error.exception), "Reference gold quotes are unavailable.")

    async def test_oversized_response_is_rejected_without_exposing_body(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text="private" * 10000))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaises(market.ReferenceUnavailable):
                await market.ReferenceClient(client).fetch_quote(self.now)

    async def test_owned_client_has_no_retries_and_closes_but_injected_client_stays_open(self):
        client = Mock(aclose=AsyncMock())
        with patch.object(market.httpx, "AsyncHTTPTransport") as transport, \
                patch.object(market.httpx, "AsyncClient", return_value=client) as constructor:
            async with market.ReferenceClient():
                pass
        transport.assert_called_once_with(retries=0)
        self.assertEqual(constructor.call_args.kwargs["timeout"], 12.0)
        self.assertFalse(constructor.call_args.kwargs["follow_redirects"])
        client.aclose.assert_awaited_once()
        injected = Mock(aclose=AsyncMock())
        async with market.ReferenceClient(injected):
            pass
        injected.aclose.assert_not_awaited()


class SampledBarTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
        self.now = self.start + timedelta(minutes=30)

    def samples(self, minutes=range(15), *, start=None):
        start = self.start if start is None else start
        return [
            {"time": (start + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
             "price": 2500.0 + minute}
            for minute in minutes
        ]

    def test_closed_bar_is_explicitly_sampled_with_coverage_and_actual_observations(self):
        samples = self.samples()
        samples[3]["price"] = 2600.0
        samples[8]["price"] = 2400.0
        result = market.aggregate_samples(samples, self.now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {
            "time": "2026-10-04T12:00:00Z", "open": 2500.0, "high": 2600.0,
            "low": 2400.0, "close": 2514.0, "sampled": True, "sample_count": 15,
            "coverage_ok": True, "first_sample": "2026-10-04T12:00:00Z",
            "last_sample": "2026-10-04T12:14:00Z", "max_gap_seconds": 60.0,
        })
        self.assertNotIn("tick_volume", result[0])
        self.assertNotIn("volume", result[0])

    def test_forming_slot_is_omitted_and_missing_slots_are_not_filled(self):
        samples = self.samples() + self.samples(start=self.start + timedelta(minutes=45))
        now = self.start + timedelta(minutes=50)
        # Only observations already seen by the current clock are passed in.
        samples = [sample for sample in samples if sample["time"] <= now.isoformat().replace("+00:00", "Z")]
        result = market.aggregate_samples(samples, now)
        self.assertEqual([bar["time"] for bar in result], ["2026-10-04T12:00:00Z"])
        result = market.aggregate_samples(self.samples() + self.samples(start=self.start + timedelta(hours=1)),
                                          self.start + timedelta(hours=2))
        self.assertEqual([bar["time"] for bar in result], [
            "2026-10-04T12:00:00Z", "2026-10-04T13:00:00Z"
        ])

    def test_sparse_samples_large_gaps_or_uncovered_edges_cannot_enable_indicators(self):
        for minutes in (
            range(9),  # Not enough observations.
            [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14],  # Five-minute internal gap.
            range(3, 15),  # Misses the opening edge by three minutes.
            range(12),  # Last observation is four minutes before the closing edge.
        ):
            with self.subTest(minutes=list(minutes)):
                bars = market.aggregate_samples(self.samples(minutes), self.now)
                self.assertEqual(len(bars), 1)
                self.assertFalse(bars[0]["coverage_ok"])
                self.assertEqual(bars[0]["sample_count"], len(minutes))

    def test_exact_coverage_boundaries_are_accepted(self):
        minutes = [2, 3, 4, 5, 6, 7, 8, 9, 10, 13]
        bar = market.aggregate_samples(self.samples(minutes), self.now)[0]
        self.assertTrue(bar["coverage_ok"])
        self.assertEqual(bar["max_gap_seconds"], 180.0)
        self.assertEqual(bar["sample_count"], 10)

    def test_duplicate_out_of_order_or_future_samples_are_rejected(self):
        cases = (
            self.samples([0, 1, 1]), self.samples([1, 0]),
            self.samples([31]), [{"time": "2026-10-04T12:00:00", "price": 2500.0}],
            [{"time": "2026-10-04T15:00:00+03:00", "price": 2500.0}],
        )
        for samples in cases:
            with self.subTest(samples=samples):
                with self.assertRaises(ValueError):
                    market.aggregate_samples(samples, self.now)

    def test_missing_nonfinite_or_nonpositive_prices_are_rejected_without_zero_fill(self):
        for price in (None, 0, -1, math.nan, math.inf, True, "2500"):
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    market.aggregate_samples([{"time": "2026-10-04T12:00:00Z", "price": price}], self.now)
        with self.assertRaises(ValueError):
            market.aggregate_samples([{"time": "2026-10-04T12:00:00Z"}], self.now)

    def test_empty_samples_and_aware_local_clock_are_handled_without_inventing_bars(self):
        self.assertEqual(market.aggregate_samples([], self.now), [])
        local_now = self.now.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(market.aggregate_samples(self.samples(), local_now),
                         market.aggregate_samples(self.samples(), self.now))
        with self.assertRaises(ValueError):
            market.aggregate_samples(self.samples(), self.now.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
