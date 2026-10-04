"""Keyless reference gold quotes and explicitly sampled M15 bars.

These prices are not represented as a broker feed or historical broker OHLC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from numbers import Real
from typing import Any

import httpx


REFERENCE_URL = "https://api.gold-api.com/price/XAU"
REFERENCE_SOURCE = "GoldAPI reference prices"
BAR_SECONDS = 15 * 60
MIN_SAMPLES = 10
MAX_SAMPLE_GAP_SECONDS = 180
EDGE_COVERAGE_SECONDS = 120
MAX_FUTURE_SECONDS = 30


class ReferenceUnavailable(RuntimeError):
    """The reference endpoint or its data could not be used safely."""


@dataclass(frozen=True)
class Quote:
    price: float
    as_of: datetime


def _price(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("Invalid reference price.")
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Invalid reference price.")
    return price


def _utc_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A UTC timestamp is required.")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("A UTC timestamp is required.") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0) or timestamp.timestamp() <= 0:
        raise ValueError("A UTC timestamp is required.")
    return timestamp.astimezone(timezone.utc)


def _clock(now: datetime | None) -> datetime:
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware clock is required.")
    return now.astimezone(timezone.utc)


def _iso(timestamp: datetime) -> str:
    return timestamp.isoformat().replace("+00:00", "Z")


class ReferenceClient:
    """Reuse one async HTTP client; never retry or follow redirects implicitly."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    async def fetch_quote(self, now: datetime | None = None) -> Quote:
        try:
            clock = _clock(now)
            response = await self._client.get(
                REFERENCE_URL, timeout=12.0, follow_redirects=False
            )
            response.raise_for_status()
            if len(response.content) > 64 * 1024:
                raise ValueError("Reference response is too large.")
            data = response.json()
            if not isinstance(data, dict) or data.get("symbol") != "XAU":
                raise ValueError("The reference symbol must be XAU.")
            if data.get("currency", "USD") != "USD":
                raise ValueError("The reference quote must be in USD.")
            price = _price(data.get("price"))
            as_of = _utc_time(data.get("updatedAt"))
            if as_of > clock + timedelta(seconds=MAX_FUTURE_SECONDS):
                raise ValueError("Reference timestamp is in the future.")
            return Quote(price=price, as_of=as_of)
        except Exception:
            # HTTP errors can include URLs, bodies or credentials from upstream.
            raise ReferenceUnavailable("Reference gold quotes are unavailable.") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> ReferenceClient:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()


def aggregate_samples(samples: list[dict], now: datetime | None = None) -> list[dict]:
    """Return closed UTC M15 bars from observed prices, with coverage metadata.

    Invalid, duplicate, out-of-order or future samples are rejected. Missing
    intervals are absent; no prices or observations are invented. Consumers
    must check coverage_ok before calculating trends or indicators.
    """
    clock = _clock(now)
    groups: dict[int, list[tuple[datetime, float]]] = {}
    previous_time = None
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("Invalid reference sample.")
        timestamp = _utc_time(sample.get("time"))
        price = _price(sample.get("price"))
        if timestamp > clock:
            raise ValueError("Future reference sample.")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("Reference samples must be strictly ordered and unique.")
        previous_time = timestamp
        slot = int(timestamp.timestamp()) // BAR_SECONDS * BAR_SECONDS
        groups.setdefault(slot, []).append((timestamp, price))
    bars = []
    for slot, observations in groups.items():
        start = datetime.fromtimestamp(slot, timezone.utc)
        end = start + timedelta(seconds=BAR_SECONDS)
        if end > clock:
            continue
        first, last = observations[0][0], observations[-1][0]
        max_gap = max(
            ((right[0] - left[0]).total_seconds() for left, right in zip(observations, observations[1:])),
            default=0.0,
        )
        prices = [price for _, price in observations]
        coverage_ok = (
            len(observations) >= MIN_SAMPLES
            and max_gap <= MAX_SAMPLE_GAP_SECONDS
            and (first - start).total_seconds() <= EDGE_COVERAGE_SECONDS
            and (end - last).total_seconds() <= EDGE_COVERAGE_SECONDS
        )
        bars.append({
            "time": _iso(start),
            "open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1],
            "sampled": True,
            "sample_count": len(observations),
            "coverage_ok": coverage_ok,
            "first_sample": _iso(first),
            "last_sample": _iso(last),
            "max_gap_seconds": max_gap,
        })
    return bars
