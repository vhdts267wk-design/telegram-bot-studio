"""No paid-provider calls are possible with the default free configuration."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import handlers, market_monitor as monitor
from bot.market_news import BriefingUnavailable, NewsBriefing


NOW = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)


class FreeModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_key_does_not_enable_news_or_chart_costs(self):
        with patch.dict(monitor.os.environ, {"OPENAI_API_KEY": "synthetic-key"}, clear=True), patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None
        ), patch.object(monitor.market_store, "save_cache", new_callable=AsyncMock) as save, patch.object(
            monitor.market_store, "claim_news_request", new_callable=AsyncMock
        ) as budget, patch.object(monitor.market_news, "generate_briefing", new_callable=AsyncMock) as paid, patch.object(
            monitor.free_news, "generate_briefing", new_callable=AsyncMock, return_value=NewsBriefing(NOW, ("free bulletin",))
        ) as free, patch.object(monitor, "utc_now", return_value=NOW), patch.object(handlers, "AsyncOpenAI") as client:
            service = monitor.MarketService(object(), 9901)
            self.assertEqual(service.news_source, "rss")
            self.assertFalse(service.openai_enabled)
            self.assertEqual(await service.news(), NewsBriefing(NOW, ("free bulletin",)))
            free.assert_awaited_once_with(NOW)
            self.assertEqual(save.await_args.args[3]["source"], "rss")
            message = SimpleNamespace(photo=[SimpleNamespace(get_file=AsyncMock())], reply_text=AsyncMock())
            await handlers.gold_photo(SimpleNamespace(effective_message=message), SimpleNamespace(bot_data={}))
            message.photo[-1].get_file.assert_not_awaited()
            self.assertIn("/reviews", message.reply_text.await_args.args[0])
            paid.assert_not_awaited()
            budget.assert_not_awaited()
            client.assert_not_called()

    async def test_rss_failure_never_falls_back_to_paid_provider(self):
        with patch.dict(monitor.os.environ, {"OPENAI_API_KEY": "synthetic-key", "OPENAI_ENABLED": "true"}, clear=True), patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None
        ), patch.object(monitor.market_store, "claim_news_request", new_callable=AsyncMock) as budget, patch.object(
            monitor.market_news, "generate_briefing", new_callable=AsyncMock
        ) as paid, patch.object(monitor.free_news, "generate_briefing", new_callable=AsyncMock, side_effect=BriefingUnavailable):
            with self.assertRaises(BriefingUnavailable):
                await monitor.MarketService(object(), 9901).news()
            paid.assert_not_awaited()
            budget.assert_not_awaited()

    async def test_old_paid_cache_is_not_served_as_free_news(self):
        cached = {"payload": {"source": "openai", "fetched_at": NOW.isoformat(), "html_chunks": ["old AI text"]}}
        with patch.dict(monitor.os.environ, {}, clear=True), patch.object(monitor, "utc_now", return_value=NOW), patch.object(
            monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=cached
        ), patch.object(monitor.market_store, "save_cache", new_callable=AsyncMock), patch.object(
            monitor.free_news, "generate_briefing", new_callable=AsyncMock, return_value=NewsBriefing(NOW, ("free",))
        ) as free:
            self.assertEqual((await monitor.MarketService(object(), 9901).news()).html_chunks, ("free",))
            free.assert_awaited_once()

