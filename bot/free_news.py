"""Read bounded official RSS headlines without an API key or paid fallback.

Feeds are published by their source institutions:
https://www.federalreserve.gov/feeds/feeds.htm
https://www.ecb.europa.eu/rss/press.html
https://news.un.org/feed/subscribe/en/news/all/rss.xml
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from bot import market_news


logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 512 * 1024
MAX_FEED_ITEMS = 200
MAX_HEADLINES = 6
MAX_REDIRECTS = 3
SOURCE_DEADLINE_SECONDS = 20.0
_CREDENTIAL_KEYS = frozenset({
    "token", "access_token", "api_key", "apikey", "key", "secret",
    "password", "authorization",
})
_POLITICAL_TITLE = re.compile(
    r"\b(?:war|wars|conflict|conflicts|ceasefire|peace|security|sanctions|"
    r"nuclear|missile|missiles|strike|strikes|military|hostilities|diplomacy|"
    r"diplomatic|elections?|refugees?|displaced|gaza|palestinian|palestine|"
    r"israel|ukraine|russia|sudan|iran|general assembly|oil|energy|tariffs?|"
    r"trade|economy|economic|inflation)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FeedSource:
    name: str
    url: str
    hosts: frozenset[str]
    political_only: bool = False


FEEDS = (
    FeedSource(
        "Federal Reserve",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        frozenset({"www.federalreserve.gov", "federalreserve.gov"}),
    ),
    FeedSource(
        "ECB",
        "https://www.ecb.europa.eu/rss/press.html",
        frozenset({"www.ecb.europa.eu", "ecb.europa.eu"}),
    ),
    FeedSource(
        "UN News",
        "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
        frozenset({"news.un.org"}),
        political_only=True,
    ),
)


@dataclass(frozen=True, slots=True)
class Headline:
    source: str
    title: str
    url: str
    published_at: datetime


def _safe_url(value: object, source: FeedSource) -> str:
    """Only HTTPS links on the particular institution's known public hosts."""
    if type(value) is not str or not value or len(value) > 1024:
        raise ValueError("Invalid feed URL")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid feed URL")
    if any(char in value for char in '<>"\\'):
        raise ValueError("Invalid feed URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in source.hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or any(
            key.lower() in _CREDENTIAL_KEYS
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        raise ValueError("Invalid feed URL")
    return value


async def _download(client: httpx.AsyncClient, source: FeedSource) -> bytes:
    """Check redirects before following and bound the decoded response body."""
    url = _safe_url(source.url, source)
    for redirect_count in range(MAX_REDIRECTS + 1):
        async with client.stream("GET", url) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise ValueError("Invalid feed redirect")
                url = _safe_url(urljoin(url, location), source)
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length is not None and int(length) > MAX_RESPONSE_BYTES:
                raise ValueError("Feed response is too large")
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("Feed response is too large")
                payload.extend(chunk)
            return bytes(payload)
    raise ValueError("Invalid feed redirect")


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_tag(child.tag) == name:
            return "".join(child.itertext()).strip()
    return None


def _publication(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse(payload: bytes, source: FeedSource, now: datetime) -> list[Headline]:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Feed response is too large")
    # Decoding first also detects declarations in UTF-16 feeds. Never permit
    # DTD/entity expansion, external entities, or a webpage disguised as RSS.
    encoding = "utf-16" if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = payload.decode(encoding)
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Feed declarations are not allowed")
    root = ET.fromstring(text)
    root_tag = _local_tag(root.tag)
    if root_tag == "rss":
        channels = [child for child in root if _local_tag(child.tag) == "channel"]
        if len(channels) != 1:
            raise ValueError("Invalid RSS channel")
        items = [child for child in channels[0] if _local_tag(child.tag) == "item"]
    elif root_tag == "RDF":
        items = [child for child in root if _local_tag(child.tag) == "item"]
    elif root_tag == "feed":
        items = [child for child in root if _local_tag(child.tag) == "entry"]
    else:
        raise ValueError("Invalid RSS document")
    since = now - timedelta(hours=24)
    headlines = []
    for item in items[:MAX_FEED_ITEMS]:
        title = " ".join((_child_text(item, "title") or "").split())
        if not title or (source.political_only and not _POLITICAL_TITLE.search(title)):
            continue
        title = "".join(char for char in title if ord(char) >= 32 and ord(char) != 127)
        title = title[:240] + ("…" if len(title) > 240 else "")
        date = _child_text(item, "published" if root_tag == "feed" else "pubDate")
        if root_tag == "RDF":
            date = _child_text(item, "date")
        published = _publication(date)
        if published is None or not since <= published <= now:
            continue
        link = _child_text(item, "link")
        if root_tag == "feed":
            link = next((
                child.attrib.get("href") for child in item
                if _local_tag(child.tag) == "link"
                and child.attrib.get("rel", "alternate") == "alternate"
            ), None)
        try:
            url = _safe_url(link, source)
        except (ValueError, TypeError):
            continue
        headlines.append(Headline(source.name, title, url, published))
    return headlines


async def _read_source(client: httpx.AsyncClient, source: FeedSource, now: datetime):
    try:
        async with asyncio.timeout(SOURCE_DEADLINE_SECONDS):
            payload = await _download(client, source)
            return _parse(payload, source, now)
    except (httpx.HTTPError, ValueError, UnicodeError, ET.ParseError, TimeoutError) as error:
        logger.warning("Public news feed unavailable: %s (%s)", source.name, type(error).__name__)
        return None


async def generate_briefing(now: datetime) -> market_news.NewsBriefing:
    """Return source headlines in the last 24 hours; never call an AI service."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("The briefing timestamp must include a timezone.")
    fetched_at = now.astimezone(timezone.utc)
    async with httpx.AsyncClient(
        timeout=12.0, follow_redirects=False, trust_env=False,
        headers={"User-Agent": "TelegramBotStudio/1.0 (official RSS reader)"},
    ) as client:
        results = await asyncio.gather(*(
            _read_source(client, source, fetched_at) for source in FEEDS
        ))
    if all(result is None for result in results):
        raise market_news.BriefingUnavailable("Public news feeds are currently unavailable.")
    collected = sorted(
        (headline for result in results if result is not None for headline in result),
        key=lambda headline: headline.published_at, reverse=True,
    )
    unique = {}
    for headline in collected:
        unique.setdefault(headline.url, headline)
    headlines = list(unique.values())[:MAX_HEADLINES]
    segments = [(
        "عناوين مجانية من المصادر الرسمية — "
        f"جلب {fetched_at:%Y-%m-%d %H:%M} UTC\n"
        "سياسة نقدية وشؤون دولية؛ العناوين بلغة المصدر، بلا تحليل لاتجاه الذهب.\n\n",
        None,
    )]
    for headline in headlines:
        segments.extend([
            (f"{headline.source} — {headline.published_at:%Y-%m-%d %H:%M} UTC\n", None),
            (headline.title, headline.url),
            ("\n\n", None),
        ])
    if not headlines:
        segments.append((
            "لم أجد في الخلاصات التي أمكن جلبها عناوين مؤهلة بتاريخ نشر "
            "داخل آخر 24 ساعة. هذه تغطية محدودة وليست دليلاً على غياب الأخبار.\n\n",
            None,
        ))
    if any(result is None for result in results):
        segments.append(("تعذّر جلب بعض المصادر؛ القائمة تعرض المصادر المتاحة فقط.\n\n", None))
    segments.append(("أوقات النشر كما وردت في RSS؛ وقت الجلب منفصل. للتثقيف فقط.", None))
    return market_news.NewsBriefing(
        fetched_at=fetched_at, html_chunks=market_news._html_chunks(segments),
    )
