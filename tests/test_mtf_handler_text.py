"""MT5 help and media requests cannot bypass empirical qualification."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import handlers, market_monitor


class MtfHandlerTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(handlers.os.environ, {
            "MARKET_SOURCE": "mt5", "OPENAI_ENABLED": "false",
        }, clear=True))
        self.enterContext(patch.object(handlers.commands, "menu_commands", return_value=[]))
        self.enterContext(patch.object(handlers.commands, "reply_menu_buttons", return_value=[]))
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.update = SimpleNamespace(
            effective_message=self.message,
            effective_user=SimpleNamespace(id=101, first_name="Synthetic User", username=None),
            effective_chat=SimpleNamespace(id=101, type="private"),
        )
        self.service = SimpleNamespace(source="mt5", manual_tickets_enabled=True)
        self.context = SimpleNamespace(bot_data={market_monitor.SERVICE_KEY: self.service})

    async def assert_current_guidance(self, command):
        for manual in (True, False):
            with self.subTest(manual=manual):
                self.service.manual_tickets_enabled = manual
                self.message.reply_text.reset_mock()
                await command(self.update, self.context)
                self.message.reply_text.assert_awaited_once()
                text = self.message.reply_text.await_args.args[0]
                for phrase in ("M15", "M5", "M1", "200", "مستقلة", "Wilson", "ذي الطرفين", "95%", "70%", "التكاليف", "10 ثوانٍ", "60 دقيقة", "5 ثوانٍ", "كل دقيقة"):
                    self.assertIn(phrase, text)
                self.assertIn("غياب الأدلة", text)
                self.assertNotIn("Accept", text)
                self.assertNotIn("15 دقيقة", text)
                self.assertNotIn("22 شمعة M15", text)
                self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
                if manual:
                    self.assertIn("جهّز على اللابتوب", text)
                    self.assertIn("Buy أو Sell بنفسك", text)
                    self.assertIn("0.01", text)
                else:
                    self.assertIn("تجهيز نافذة MT5 غير مفعّل", text)
                    self.assertIn("لا ترسل أوامر تداول تلقائية", text)

    async def test_start_explains_current_strategy_and_manual_or_disabled_mode(self):
        await self.assert_current_guidance(handlers.start)
        markup = self.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertTrue(markup.is_persistent)

    async def test_help_explains_current_strategy_and_archived_reviews(self):
        await self.assert_current_guidance(handlers.help_command)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("مراجعات ورقية قديمة", text)
        self.assertIn("الصورة لا تثبت فرصة مؤهلة", text)
        self.assertNotIn("Send a clear XAUUSD screenshot", text)

    async def test_about_explains_empirical_evidence_without_promising_signals(self):
        await self.assert_current_guidance(handlers.about)

    def test_command_descriptions_do_not_advertise_old_cadence_or_automatic_accept(self):
        commands = dict(handlers.BOT_COMMANDS)
        for frame in ("M15", "M5", "M1"):
            self.assertIn(frame, commands["market"])
        self.assertIn("خارج العينة", commands["signals"])
        self.assertIn("ليست تأهيل", commands["reviews"])
        self.assertNotIn("15 دقيقة", "\n".join(commands.values()) + handlers.HELP_TEXT)
        self.assertNotIn("Accept", "\n".join(commands.values()) + handlers.HELP_TEXT)

    async def test_configured_mt5_without_service_still_has_fail_closed_guidance(self):
        self.context.bot_data.clear()
        await handlers.help_command(self.update, self.context)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("M15", text)
        self.assertIn("غياب الأدلة", text)
        self.assertIn("تجهيز نافذة MT5 غير مفعّل", text)
        self.assertNotIn("Accept", text)

    async def test_reference_help_keeps_reference_education_separate(self):
        self.service.source = "reference"
        self.service.manual_tickets_enabled = False
        await handlers.about(self.update, self.context)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("المصدر المرجعي", text)
        self.assertIn("ليس سعر تنفيذ", text)
        self.assertIn("شرح الصورة التعليمي منفصل", text)


class MtfMediaAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(handlers.os.environ, {
            "MARKET_SOURCE": "mt5", "OPENAI_ENABLED": "true", "OPENAI_API_KEY": "synthetic-unused-key",
        }, clear=True))
        self.photo = SimpleNamespace(get_file=AsyncMock())
        self.message = SimpleNamespace(photo=[self.photo], reply_text=AsyncMock())
        self.update = SimpleNamespace(
            effective_message=self.message,
            effective_user=SimpleNamespace(id=101),
            effective_chat=SimpleNamespace(id=101, type="private"),
        )
        self.context = SimpleNamespace(bot_data={
            market_monitor.SERVICE_KEY: SimpleNamespace(source="mt5", manual_tickets_enabled=True),
        })
        self.client = self.enterContext(patch.object(handlers, "AsyncOpenAI"))
        self.vision = self.enterContext(patch.object(handlers, "_analyze_gold_photo", new_callable=AsyncMock))

    async def test_mt5_photo_uses_guarded_market_route_even_when_vision_is_enabled(self):
        for enabled in ("true", "false"):
            with self.subTest(enabled=enabled):
                handlers.os.environ["OPENAI_ENABLED"] = enabled
                self.message.reply_text.reset_mock()
                with patch.object(market_monitor, "market_command", new_callable=AsyncMock) as analysis:
                    await handlers.gold_photo(self.update, self.context)
                    analysis.assert_awaited_once_with(self.update, self.context)
                self.assertIn("M15 وM5 وM1", self.message.reply_text.await_args.args[0])
                self.photo.get_file.assert_not_awaited()
                self.client.assert_not_called()
                self.vision.assert_not_awaited()

    async def test_mt5_photo_without_available_service_cannot_fall_back_to_paid_vision(self):
        self.context.bot_data.clear()
        with patch.object(market_monitor, "market_command", new_callable=AsyncMock) as analysis:
            await handlers.gold_photo(self.update, self.context)
            analysis.assert_awaited_once_with(self.update, self.context)
        self.photo.get_file.assert_not_awaited()
        self.client.assert_not_called()
        self.vision.assert_not_awaited()

    async def test_mt5_image_document_uses_the_same_guarded_route(self):
        self.message.document = SimpleNamespace(file_name="synthetic-chart.png")
        with patch.object(market_monitor, "market_command", new_callable=AsyncMock) as analysis:
            await handlers.gold_document(self.update, self.context)
            analysis.assert_awaited_once_with(self.update, self.context)
        self.client.assert_not_called()
        self.vision.assert_not_awaited()

    async def test_actual_mt5_route_without_feed_or_evidence_shows_no_actionable_levels(self):
        service = market_monitor.MarketService(object(), 991)
        self.context.bot_data[market_monitor.SERVICE_KEY] = service
        with patch.object(market_monitor.market_store, "get_cache", new_callable=AsyncMock, return_value=None), patch.object(
            market_monitor.market_store, "save_cache", new_callable=AsyncMock,
        ) as save:
            await handlers.gold_photo(self.update, self.context)
        replies = "\n".join(call.args[0] for call in self.message.reply_text.await_args_list)
        self.assertIn("لا توجد فرصة", replies)
        for action in ("شراء BUY", "بيع SELL", "SL:", "TP1:", "جهّز على اللابتوب"):
            self.assertNotIn(action, replies)
        save.assert_not_awaited()
        self.photo.get_file.assert_not_awaited()
        self.client.assert_not_called()
        self.vision.assert_not_awaited()

    async def test_explicit_reference_service_preserves_opted_in_educational_vision(self):
        self.context.bot_data[market_monitor.SERVICE_KEY].source = "reference"
        with patch.object(market_monitor, "market_command", new_callable=AsyncMock) as analysis:
            await handlers.gold_photo(self.update, self.context)
            analysis.assert_not_awaited()
        self.vision.assert_awaited_once_with(self.message, "synthetic-unused-key")
        self.client.assert_not_called()

    async def test_reference_free_photo_uses_reference_report_without_mt5_claim(self):
        self.context.bot_data[market_monitor.SERVICE_KEY].source = "reference"
        handlers.os.environ["OPENAI_ENABLED"] = "false"
        with patch.object(market_monitor, "market_command", new_callable=AsyncMock) as analysis:
            await handlers.gold_photo(self.update, self.context)
            analysis.assert_awaited_once_with(self.update, self.context)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("الأسعار المرجعية", text)
        self.assertNotIn("بيانات MT5", text)
        self.vision.assert_not_awaited()

    async def test_no_service_free_photo_fallback_does_not_promise_old_reviews_or_cadence(self):
        self.context.bot_data.clear()
        handlers.os.environ.update(MARKET_SOURCE="reference", OPENAI_ENABLED="false")
        await handlers.gold_photo(self.update, self.context)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("اقتراح مؤهل", text)
        self.assertIn("الورقية السابقة", text)
        self.assertNotIn("15 دقيقة", text)
        self.vision.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
