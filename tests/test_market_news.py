"""News requests use the real SDK with synthetic local transports only."""

import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI

from bot import market_news


NOW = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)


def output_text(text, url="https://www.reuters.com/world/test?a=1&b=2"):
    start = text.index("[المصدر]")
    return {
        "type": "output_text",
        "text": text,
        "annotations": [{
            "type": "url_citation",
            "start_index": start,
            "end_index": start + len("[المصدر]"),
            "url": url,
            "title": "Synthetic <source> & report",
        }],
    }


def response_body():
    return {
        "id": "resp_local_news",
        "object": "response",
        "created_at": NOW.timestamp(),
        "status": "completed",
        "model": market_news.NEWS_MODEL,
        "output": [
            {
                "id": "ws_local_news",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "synthetic news", "sources": []},
            },
            {
                "id": "msg_news_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    output_text("🟡 نُشر 2026-10-04: خبر اقتصادي <اختبار> & بيان. [المصدر]"),
                    output_text("نُشر 2026-10-04: خبر سياسي تجريبي. [المصدر]", "https://www.un.org/news/test"),
                ],
            },
            {
                "id": "msg_news_2",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    output_text("نُشر 2026-10-03: بيان تجريبي ثالث. [المصدر]", "https://www.federalreserve.gov/test"),
                ],
            },
        ],
        "parallel_tool_calls": False,
        "tools": [],
        "tool_choice": "required",
        "metadata": {},
    }


class ParsedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.urls = []
        self.link_open = False

    def handle_starttag(self, tag, attrs):
        if tag != "a" or self.link_open:
            raise AssertionError("Unexpected or nested HTML tag")
        self.link_open = True
        self.urls.append(dict(attrs)["href"])

    def handle_endtag(self, tag):
        if tag != "a" or not self.link_open:
            raise AssertionError("Unbalanced HTML tag")
        self.link_open = False

    def handle_data(self, text):
        self.text.append(text)


class MarketNewsTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, body=None, *, status_code=200, now=NOW, transport_error=None):
        requests, clients, options = [], [], []
        body = response_body() if body is None else body

        def respond(request):
            requests.append(request)
            if transport_error:
                raise transport_error
            return httpx2.Response(
                status_code,
                json=body,
                headers={"x-private-test": "private-header-do-not-log"},
            )

        def client_factory(**kwargs):
            options.append(kwargs)
            http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
            clients.append(http_client)
            return AsyncOpenAI(http_client=http_client, **kwargs)

        with patch.object(market_news, "AsyncOpenAI", side_effect=client_factory):
            try:
                result = await market_news.generate_briefing("synthetic-key-do-not-log", now)
            finally:
                self.assertTrue(all(client.is_closed for client in clients))
        return result, requests, options

    async def test_actual_sdk_request_is_bounded_and_all_text_blocks_keep_inline_links(self):
        result, requests, options = await self.exercise()
        self.assertEqual(result.fetched_at, NOW)
        self.assertEqual(result.fetched_at.tzinfo, timezone.utc)
        self.assertIsInstance(result.html_chunks, tuple)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1/responses")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "gpt-4.1-mini")
        self.assertEqual(payload["tools"], [{"type": "web_search", "external_web_access": True}])
        self.assertEqual(payload["tool_choice"], "required")
        self.assertEqual(payload["max_tool_calls"], 1)
        self.assertEqual(payload["max_output_tokens"], 800)
        self.assertEqual(payload["include"], ["web_search_call.action.sources"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["instructions"], market_news.NEWS_INSTRUCTIONS)
        self.assertIn("2026-10-03T12:30:00+00:00", payload["input"])
        self.assertIn("2026-10-04T12:30:00+00:00", payload["input"])
        self.assertNotIn("synthetic-key", json.dumps(payload))
        self.assertEqual(options[0]["timeout"], 45.0)
        self.assertEqual(options[0]["max_retries"], 0)
        parser = ParsedHTML()
        parser.feed("".join(result.html_chunks))
        self.assertFalse(parser.link_open)
        self.assertEqual(len(parser.urls), 3)
        self.assertIn("https://www.un.org/news/test", parser.urls)
        self.assertIn("https://www.federalreserve.gov/test", parser.urls)
        raw = "".join(result.html_chunks)
        self.assertIn("&lt;اختبار&gt; &amp;", raw)
        self.assertIn('href="https://www.reuters.com/world/test?a=1&amp;b=2"', raw)
        self.assertIn("تم التحقق 2026-10-04 12:30 UTC", "".join(parser.text))
        self.assertIn(market_news._NOTICE, "".join(parser.text))

    async def test_timezone_is_normalized_and_naive_time_is_rejected_before_request(self):
        local_time = NOW.astimezone(timezone(timedelta(hours=3)))
        result, _, _ = await self.exercise(now=local_time)
        self.assertEqual(result.fetched_at, NOW)
        with patch.object(market_news, "AsyncOpenAI") as client:
            with self.assertRaisesRegex(ValueError, "timezone"):
                await market_news.generate_briefing("synthetic-key", NOW.replace(tzinfo=None))
            with self.assertRaisesRegex(ValueError, "API key"):
                await market_news.generate_briefing(" ", NOW)
            client.assert_not_called()

    async def test_rejects_incomplete_or_unsearched_responses_and_empty_or_uncited_output(self):
        cases = []
        for status in ("incomplete", "failed", "in_progress"):
            body = response_body()
            body["status"] = status
            cases.append(body)
        body = response_body()
        body["output"].pop(0)
        cases.append(body)
        for status in ("failed", "incomplete", "searching"):
            body = response_body()
            body["output"][0]["status"] = status
            cases.append(body)
        body = response_body()
        body["output"][0]["action"] = {"type": "open_page", "url": "https://www.un.org/test"}
        cases.append(body)
        body = response_body()
        body["output"] = body["output"][:1]
        cases.append(body)
        body = response_body()
        for message in body["output"][1:]:
            for content in message["content"]:
                content["annotations"] = []
        cases.append(body)
        body = response_body()
        body["output"][1]["status"] = "in_progress"
        cases.append(body)
        for index, body in enumerate(cases):
            with self.subTest(case=index), self.assertLogs(market_news.logger, level="WARNING") as logs:
                with self.assertRaises(market_news.BriefingUnavailable):
                    await self.exercise(body)
                self.assertEqual(logs.output, ["WARNING:bot.market_news:News briefing unavailable: BriefingUnavailable"])
                self.assertNotIn("خبر", "".join(logs.output))

    async def test_rejects_bad_annotation_ranges_and_overlapping_citations(self):
        # The SDK coerces JSON bool/int fields; enforce the parsed integer bounds.
        for start, end in ((-1, 2), (4, 4), (6, 5), (0, 9999), (0, False)):
            body = response_body()
            annotation = body["output"][1]["content"][0]["annotations"][0]
            annotation.update(start_index=start, end_index=end)
            with self.subTest(start=start, end=end), self.assertLogs(market_news.logger, level="WARNING"):
                with self.assertRaises(market_news.BriefingUnavailable):
                    await self.exercise(body)
        body = response_body()
        annotations = body["output"][1]["content"][0]["annotations"]
        annotations.append(copy.deepcopy(annotations[0]))
        with self.assertLogs(market_news.logger, level="WARNING"):
            with self.assertRaises(market_news.BriefingUnavailable):
                await self.exercise(body)

    async def test_rejects_unsafe_citation_urls_without_logging_them(self):
        urls = (
            "javascript:alert('private-url-secret')",
            "http://www.reuters.com/private-url-secret",
            "https://user:private-url-secret@www.reuters.com/news",
            "https://localhost/private-url-secret",
            "https://127.0.0.1/private-url-secret",
            "https://[::1]/private-url-secret",
            "https://news.internal/private-url-secret",
            "https://www.reuters.com:8443/private-url-secret",
            'https://www.reuters.com/\" onmouseover=\"private-url-secret',
            "https://www.reuters.com/\\private-url-secret",
            "https://www.reuters.com/news?api_key=private-url-secret",
            "https://www.reuters.com/news?ACCESS_TOKEN=private-url-secret",
            "https://www.reuters.com/\nprivate-url-secret",
            "https://-invalid.com/private-url-secret",
        )
        for url in urls:
            body = response_body()
            body["output"][1]["content"][0]["annotations"][0]["url"] = url
            with self.subTest(url=url), self.assertLogs(market_news.logger, level="WARNING") as logs:
                with self.assertRaises(market_news.BriefingUnavailable) as caught:
                    await self.exercise(body)
                self.assertNotIn("private-url-secret", "".join(logs.output))
                self.assertNotIn("private-url-secret", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    async def test_provider_error_is_one_request_and_class_only_diagnostic(self):
        private = "provider-message-and-secret-do-not-log"
        body = {"error": {"message": private, "type": private, "code": private, "param": private}}
        with self.assertLogs(market_news.logger, level="WARNING") as logs:
            with self.assertRaises(market_news.BriefingUnavailable) as caught:
                await self.exercise(body, status_code=429)
        self.assertEqual(logs.output, ["WARNING:bot.market_news:News briefing unavailable: RateLimitError"])
        self.assertNotIn(private, str(caught.exception))
        self.assertNotIn("synthetic-key", "".join(logs.output))
        self.assertNotIn("private-header", "".join(logs.output))
        # A second request would make the mock fail instead of a RateLimitError.
        count = 0

        def one_request(request):
            nonlocal count
            count += 1
            if count != 1:
                raise AssertionError("Provider failure was retried")
            return httpx2.Response(429, json=body)

        transport = httpx2.AsyncClient(transport=httpx2.MockTransport(one_request))
        with patch.object(market_news, "AsyncOpenAI", side_effect=lambda **kwargs: AsyncOpenAI(http_client=transport, **kwargs)):
            with self.assertLogs(market_news.logger, level="WARNING"):
                with self.assertRaises(market_news.BriefingUnavailable):
                    await market_news.generate_briefing("synthetic-key", NOW)
        self.assertEqual(count, 1)
        self.assertTrue(transport.is_closed)

    async def test_transport_failures_are_sanitized(self):
        with self.assertLogs(market_news.logger, level="WARNING") as logs:
            with self.assertRaises(market_news.BriefingUnavailable) as caught:
                await self.exercise(transport_error=httpx2.ConnectError("private-transport-secret"))
        self.assertIn("APIConnectionError", "".join(logs.output))
        self.assertNotIn("private-transport-secret", "".join(logs.output))
        self.assertNotIn("private-transport-secret", str(caught.exception))

    async def test_invalid_unicode_provider_output_is_rejected_without_logging_text(self):
        body = response_body()
        body["output"][1]["content"][0]["text"] += " private-unicode-secret\ud800"
        with self.assertLogs(market_news.logger, level="WARNING") as logs:
            with self.assertRaises(market_news.BriefingUnavailable) as caught:
                await self.exercise(body)
        self.assertIn("UnicodeEncodeError", "".join(logs.output))
        self.assertNotIn("private-unicode-secret", "".join(logs.output))
        self.assertNotIn("private-unicode-secret", str(caught.exception))

    async def test_cancellation_is_not_converted_to_availability_error(self):
        with patch.object(market_news, "AsyncOpenAI", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await market_news.generate_briefing("synthetic-key", NOW)


class MarketNewsRenderingTests(unittest.TestCase):
    def test_long_emoji_and_escaped_text_links_remain_balanced_below_telegram_limits(self):
        prefix = "فقرة عربية قصيرة.\n\n"
        linked = ("🟡<&> فقرة عربية.\n\n" * 500) + "نهاية الرابط"
        suffix = "\n\nنهاية الموجز"
        url = "https://www.reuters.com/news?a=1&b=2"
        chunks = market_news._html_chunks([(prefix, None), (linked, url), (suffix, None)])
        self.assertGreater(len(chunks), 2)
        combined = []
        for chunk in chunks:
            parser = ParsedHTML()
            parser.feed(chunk)
            self.assertFalse(parser.link_open)
            text = "".join(parser.text)
            combined.append(text)
            self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 4096)
            self.assertLessEqual(len(text), 2000)
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4000)
            self.assertTrue(all(value == url for value in parser.urls))
        self.assertEqual("".join(combined), prefix + linked + suffix)
        self.assertTrue(any(chunk.endswith("\n\n</a>") for chunk in chunks[:-1]))

    def test_plain_output_html_cannot_create_tags_and_long_urls_fit(self):
        text = '<a href="javascript:alert(1)">🟡</a>' * 250
        url = "https://www.reuters.com/" + "a" * 1800
        chunks = market_news._html_chunks([(text, url)])
        combined = []
        for chunk in chunks:
            parser = ParsedHTML()
            parser.feed(chunk)
            self.assertFalse(parser.link_open)
            self.assertEqual(parser.urls, [url])
            self.assertLessEqual(market_news._utf16_units(chunk), 4096)
            combined.extend(parser.text)
        self.assertEqual("".join(combined), text)
        self.assertIn("&lt;a", "".join(chunks))

if __name__ == "__main__":
    unittest.main()
