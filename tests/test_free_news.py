"""Official-feed news tests use synthetic local HTTP transports only."""

import asyncio
import html
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from unittest.mock import patch

import httpx

from bot import free_news, market_news


NOW = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
REAL_ASYNC_CLIENT = httpx.AsyncClient


def rss(items=()):
    return (
        "<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel>"
        + "".join(
            "<item><title>" + html.escape(title) + "</title><link>"
            + html.escape(url) + "</link><pubDate>" + html.escape(date)
            + "</pubDate><description>Do not copy this summary.</description></item>"
            for title, url, date in items
        ) + "</channel></rss>"
    ).encode("utf-8")


def item(title="Synthetic monetary policy decision", *, url=None, published=None):
    return (
        title,
        url or "https://www.federalreserve.gov/newsevents/pressreleases/test.htm",
        format_datetime(NOW - timedelta(hours=1)) if published is None else published,
    )


class ParsedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.urls = [], []
        self.open = False

    def handle_starttag(self, tag, attrs):
        if tag != "a" or self.open:
            raise AssertionError("Unexpected HTML")
        self.open = True
        self.urls.append(dict(attrs)["href"])

    def handle_endtag(self, tag):
        if tag != "a" or not self.open:
            raise AssertionError("Unbalanced HTML")
        self.open = False

    def handle_data(self, value):
        self.text.append(value)


class FreeNewsTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, responses=None, *, handler=None, now=NOW):
        responses = {} if responses is None else responses
        requests, clients, options = [], [], []

        def respond(request):
            requests.append(request)
            if handler is not None:
                return handler(request)
            response = responses.get(str(request.url), rss())
            if isinstance(response, Exception):
                raise response
            if isinstance(response, httpx.Response):
                return response
            return httpx.Response(200, content=response, headers={"content-type": "text/xml"})

        def client_factory(**kwargs):
            options.append(kwargs)
            client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(client)
            return client

        with patch.object(free_news.httpx, "AsyncClient", side_effect=client_factory), patch.object(
            market_news, "AsyncOpenAI"
        ) as ai_client:
            try:
                result = await free_news.generate_briefing(now)
            finally:
                self.assertTrue(all(client.is_closed for client in clients))
                ai_client.assert_not_called()
        return result, requests, options

    async def test_official_sources_return_linked_titles_without_summaries_or_paid_calls(self):
        fed, ecb, un = free_news.FEEDS
        result, requests, options = await self.exercise({
            fed.url: rss([item("<b>Synthetic decision</b> & policy")]),
            ecb.url: rss([item("Synthetic ECB policy", url="https://www.ecb.europa.eu/press/test.en.html")]),
            un.url: rss([
                item("Synthetic peace talks", url="https://news.un.org/en/story/test"),
                item("Synthetic sports results", url="https://news.un.org/en/story/sports"),
            ]),
        })
        self.assertEqual(result.fetched_at, NOW)
        self.assertEqual(result.fetched_at.tzinfo, timezone.utc)
        self.assertEqual({str(request.url) for request in requests}, {source.url for source in free_news.FEEDS})
        self.assertTrue(all(request.method == "GET" for request in requests))
        self.assertFalse(options[0]["follow_redirects"])
        self.assertFalse(options[0]["trust_env"])
        self.assertEqual(options[0]["timeout"], 12.0)
        self.assertTrue(all("authorization" not in request.headers for request in requests))
        parsed = ParsedHTML()
        for chunk in result.html_chunks:
            parsed.feed(chunk)
            self.assertFalse(parsed.open)
        self.assertEqual(len(parsed.urls), 3)
        text = "".join(parsed.text)
        self.assertIn("<b>Synthetic decision</b> & policy", text)
        self.assertNotIn("Do not copy this summary", text)
        self.assertNotIn("sports results", text)
        self.assertIn("2026-10-04 11:30 UTC", text)

    async def test_exact_24_hour_window_aware_dates_and_future_dates(self):
        cases = [
            ("within", NOW - timedelta(minutes=1)),
            ("boundary", NOW - timedelta(hours=24)),
            ("stale", NOW - timedelta(hours=24, seconds=1)),
            ("future", NOW + timedelta(seconds=1)),
            ("local-offset", (NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=3)))),
        ]
        items = [item(name, url=f"https://www.federalreserve.gov/{name}", published=format_datetime(date)) for name, date in cases]
        items += [
            item("missing-timezone", published="Sun, 04 Oct 2026 10:30:00"),
            item("date-only", published="2026-10-04"),
            item("malformed-date", published="recently"),
        ]
        result, _, _ = await self.exercise({free_news.FEEDS[0].url: rss(items)})
        parsed = ParsedHTML()
        for chunk in result.html_chunks:
            parsed.feed(chunk)
        self.assertEqual(set(parsed.urls), {
            "https://www.federalreserve.gov/within",
            "https://www.federalreserve.gov/boundary",
            "https://www.federalreserve.gov/local-offset",
        })

    async def test_duplicates_entry_cap_and_balanced_chunks_with_emoji_and_long_urls(self):
        items = [item(
            f"🟡 <title & {index}> " + "🟡" * 260,
            url=f"https://www.federalreserve.gov/{index}?a=" + "x" * 800 + "&b=2",
        ) for index in range(10)]
        items.append(items[0])
        result, _, _ = await self.exercise({free_news.FEEDS[0].url: rss(items)})
        parsed = ParsedHTML()
        for chunk in result.html_chunks:
            self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 4096)
            parsed.feed(chunk)
            self.assertFalse(parsed.open)
        self.assertLessEqual(len(result.html_chunks), 8)
        self.assertLessEqual(len(parsed.urls), free_news.MAX_HEADLINES * 2)
        self.assertEqual(len(set(parsed.urls)), free_news.MAX_HEADLINES)

    async def test_empty_success_is_an_informative_current_bulletin(self):
        responses = {source.url: httpx.Response(503) for source in free_news.FEEDS}
        responses[free_news.FEEDS[0].url] = rss()
        result, _, _ = await self.exercise(responses)
        text = "".join(result.html_chunks)
        self.assertIn("آخر 24 ساعة", text)
        self.assertIn("تعذّر جلب بعض المصادر", text)
        self.assertEqual(result.fetched_at, NOW)

    async def test_all_failed_sources_raise_safe_failure_and_hide_provider_details(self):
        for failure in (httpx.Response(503), httpx.ConnectError("private-provider-detail"), b"<html>not RSS</html>"):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(market_news.BriefingUnavailable) as raised:
                    await self.exercise({source.url: failure for source in free_news.FEEDS})
                self.assertNotIn("private-provider-detail", str(raised.exception))

    async def test_unsafe_citation_urls_are_omitted(self):
        urls = [
            "http://www.federalreserve.gov/test",
            "https://www.federalreserve.gov.evil.test/test",
            "https://federalreserve.gov@evil.test/test",
            "https://www.federalreserve.gov:444/test",
            "https://127.0.0.1/test",
            "https://www.federalreserve.gov/test?api_key=private",
            "https://www.federalreserve.gov/test?token=",
            "https://www.federalreserve.gov/test\\evil",
            "https://www.federalreserve.gov/te\nst",
        ]
        result, _, _ = await self.exercise({free_news.FEEDS[0].url: rss([item(url=url) for url in urls])})
        self.assertNotIn("<a ", "".join(result.html_chunks))

    async def test_redirect_is_checked_before_any_new_host_is_requested(self):
        fed = free_news.FEEDS[0]
        def respond(request):
            if str(request.url) == fed.url:
                return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
            return httpx.Response(200, content=rss())
        result, requests, _ = await self.exercise(handler=respond)
        self.assertEqual(len(requests), 3)
        self.assertFalse(any(request.url.host == "127.0.0.1" for request in requests))
        self.assertIn("تعذّر", "".join(result.html_chunks))

    async def test_safe_relative_redirect_is_followed_and_redirect_loops_are_bounded(self):
        fed = free_news.FEEDS[0]
        def redirect(request):
            if str(request.url) == fed.url:
                return httpx.Response(302, headers={"location": "/feeds/current.xml"})
            return httpx.Response(200, content=rss([item()]))
        result, requests, _ = await self.exercise(handler=redirect)
        self.assertTrue(any(str(request.url) == "https://www.federalreserve.gov/feeds/current.xml" for request in requests))
        self.assertIn("Synthetic monetary", "".join(result.html_chunks))
        with self.assertRaises(market_news.BriefingUnavailable):
            await self.exercise(handler=lambda request: httpx.Response(302, headers={"location": str(request.url)}))

    async def test_response_size_header_and_stream_limit(self):
        for response in (
            httpx.Response(200, content=rss(), headers={"content-length": str(free_news.MAX_RESPONSE_BYTES + 1)}),
            httpx.Response(200, content=b"x" * (free_news.MAX_RESPONSE_BYTES + 1)),
        ):
            with self.subTest(response=len(response.content)), self.assertRaises(market_news.BriefingUnavailable):
                await self.exercise({source.url: response for source in free_news.FEEDS})

    async def test_xml_entities_and_dtd_are_rejected_in_utf8_and_utf16(self):
        xml = '<!DOCTYPE rss [<!ENTITY secret "expanded">]><rss><channel><title>&secret;</title></channel></rss>'
        for payload in (xml.encode("utf-8"), xml.encode("utf-16")):
            with self.subTest(encoding=payload[:2]), self.assertRaises(market_news.BriefingUnavailable):
                await self.exercise({source.url: payload for source in free_news.FEEDS})

    async def test_stalled_feed_has_a_total_deadline_and_does_not_block_other_sources(self):
        class StalledStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.Future()
                yield b"unreachable"

        def respond(request):
            if str(request.url) == free_news.FEEDS[0].url:
                return httpx.Response(200, stream=StalledStream())
            return httpx.Response(200, content=rss())

        with patch.object(free_news, "SOURCE_DEADLINE_SECONDS", 0.01):
            result, _, _ = await asyncio.wait_for(self.exercise(handler=respond), timeout=2.0)
        self.assertIn("تعذّر", "".join(result.html_chunks))

    async def test_atom_requires_publication_and_does_not_treat_update_as_new(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Published item</title><published>2026-10-04T11:00:00Z</published>
          <link href="https://www.federalreserve.gov/published"/></entry>
          <entry><title>Updated old item</title><updated>2026-10-04T11:00:00Z</updated>
          <link href="https://www.federalreserve.gov/updated"/></entry></feed>'''
        result, _, _ = await self.exercise({free_news.FEEDS[0].url: xml})
        self.assertIn("Published item", "".join(result.html_chunks))
        self.assertNotIn("Updated old item", "".join(result.html_chunks))

    async def test_timestamp_requires_timezone(self):
        with self.assertRaises(ValueError):
            await free_news.generate_briefing(NOW.replace(tzinfo=None))
        result, _, _ = await self.exercise(now=NOW.astimezone(timezone(timedelta(hours=3))))
        self.assertEqual(result.fetched_at, NOW)


if __name__ == "__main__":
    unittest.main()
