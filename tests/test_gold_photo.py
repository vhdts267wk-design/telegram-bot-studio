import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from openai import APIConnectionError
from telegram.error import NetworkError, TelegramError

from bot import handlers


class GoldPhotoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api_key = "test-only-api-key"
        self.environment = patch.dict(
            handlers.os.environ, {"OPENAI_API_KEY": self.api_key}, clear=True
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        self.image_bytes = bytearray(b"test-image-bytes")
        self.telegram_file = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=self.image_bytes)
        )
        self.small_photo = SimpleNamespace(get_file=AsyncMock())
        self.large_photo = SimpleNamespace(
            get_file=AsyncMock(return_value=self.telegram_file)
        )
        self.message = SimpleNamespace(
            photo=[self.small_photo, self.large_photo], reply_text=AsyncMock()
        )
        self.update = SimpleNamespace(effective_message=self.message)
        self.context = SimpleNamespace()

        self.client = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(
                    return_value=SimpleNamespace(output_text="Visible chart observations.")
                )
            )
        )
        self.client_manager = MagicMock()
        self.client_manager.__aenter__ = AsyncMock(return_value=self.client)
        self.client_manager.__aexit__ = AsyncMock(return_value=False)
        self.client_patch = patch.object(
            handlers, "AsyncOpenAI", return_value=self.client_manager
        )
        self.client_factory = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def replies(self):
        return [call.args[0] for call in self.message.reply_text.await_args_list]

    def assert_nonempty_fallback(self):
        self.assertEqual(self.message.reply_text.await_count, 1)
        self.assertTrue(self.replies()[0].strip())

    async def test_ignores_updates_without_a_message_or_photo(self):
        await handlers.gold_photo(
            SimpleNamespace(effective_message=None), self.context
        )
        self.message.photo = []
        await handlers.gold_photo(self.update, self.context)

        self.message.reply_text.assert_not_awaited()
        self.large_photo.get_file.assert_not_awaited()
        self.client_factory.assert_not_called()

    async def test_missing_or_whitespace_api_key_stops_before_downloading(self):
        for api_key in (None, "   "):
            with self.subTest(api_key=api_key):
                if api_key is None:
                    handlers.os.environ.pop("OPENAI_API_KEY", None)
                else:
                    handlers.os.environ["OPENAI_API_KEY"] = api_key
                self.message.reply_text.reset_mock()

                await handlers.gold_photo(self.update, self.context)

                self.assert_nonempty_fallback()
                self.large_photo.get_file.assert_not_awaited()
                self.client_factory.assert_not_called()

    async def test_downloads_largest_photo_and_awaits_vision_response(self):
        await handlers.gold_photo(self.update, self.context)

        self.small_photo.get_file.assert_not_awaited()
        self.large_photo.get_file.assert_awaited_once_with()
        self.telegram_file.download_as_bytearray.assert_awaited_once_with()
        self.client_factory.assert_called_once_with(
            api_key=self.api_key, timeout=45.0, max_retries=1
        )
        self.client_manager.__aenter__.assert_awaited_once()
        self.client_manager.__aexit__.assert_awaited_once()
        self.client.responses.create.assert_awaited_once()

        request = self.client.responses.create.await_args.kwargs
        self.assertEqual(request["model"], "gpt-4.1-mini")
        self.assertEqual(request["instructions"], handlers.GOLD_INSTRUCTIONS)
        self.assertEqual(request["max_output_tokens"], 1000)
        self.assertIs(request["store"], False)
        self.assertEqual(request["input"][0]["role"], "user")
        content = request["input"][0]["content"]
        self.assertTrue(any(item["type"] == "input_text" for item in content))
        images = [item for item in content if item["type"] == "input_image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["detail"], "high")
        self.assertEqual(
            images[0]["image_url"],
            "data:image/jpeg;base64,"
            + base64.b64encode(self.image_bytes).decode("ascii"),
        )
        reply = "".join(self.replies())
        self.assertTrue(reply.startswith("Visible chart observations."))
        self.assertGreater(len(reply), len("Visible chart observations."))
        for call in self.message.reply_text.await_args_list:
            self.assertIsNone(call.kwargs["parse_mode"])

    async def test_model_override_and_key_are_trimmed(self):
        handlers.os.environ["OPENAI_API_KEY"] = "  test-only-api-key  "
        handlers.os.environ["OPENAI_MODEL"] = "  gpt-4.1  "

        await handlers.gold_photo(self.update, self.context)

        self.assertEqual(
            self.client.responses.create.await_args.kwargs["model"], "gpt-4.1"
        )
        self.assertEqual(self.client_factory.call_args.kwargs["api_key"], self.api_key)

    async def test_blank_model_override_uses_default(self):
        handlers.os.environ["OPENAI_MODEL"] = "   "

        await handlers.gold_photo(self.update, self.context)

        self.assertEqual(
            self.client.responses.create.await_args.kwargs["model"], "gpt-4.1-mini"
        )

    async def test_empty_download_stops_before_openai(self):
        self.telegram_file.download_as_bytearray.return_value = bytearray()

        await handlers.gold_photo(self.update, self.context)

        self.assert_nonempty_fallback()
        self.client_factory.assert_not_called()

    async def test_empty_output_returns_readable_fallback(self):
        self.client.responses.create.return_value = SimpleNamespace(output_text="   ")

        await handlers.gold_photo(self.update, self.context)

        self.assert_nonempty_fallback()

    async def test_provider_failure_redacts_sensitive_exception_details(self):
        sensitive_detail = "RAW_PROVIDER_DETAIL test-only-api-key"
        self.client.responses.create.side_effect = APIConnectionError(
            message=sensitive_detail, request=MagicMock()
        )

        with self.assertLogs(handlers.logger, level="WARNING") as captured:
            await handlers.gold_photo(self.update, self.context)

        self.assert_nonempty_fallback()
        output = "\n".join(captured.output + self.replies())
        self.assertIn("APIConnectionError", output)
        self.assertNotIn(sensitive_detail, output)
        self.assertNotIn(self.api_key, output)
        self.client_manager.__aexit__.assert_awaited_once()

    async def test_telegram_download_failure_redacts_private_file_url(self):
        private_url = "https://api.telegram.org/file/botFAKE_BOT_TOKEN/private.jpg"
        self.telegram_file.download_as_bytearray.side_effect = TelegramError(private_url)

        with self.assertLogs(handlers.logger, level="WARNING") as captured:
            await handlers.gold_photo(self.update, self.context)

        self.assert_nonempty_fallback()
        self.client_factory.assert_not_called()
        output = "\n".join(captured.output + self.replies())
        self.assertIn("TelegramError", output)
        self.assertNotIn(private_url, output)
        self.assertNotIn("FAKE_BOT_TOKEN", output)
        self.assertNotIn(self.api_key, output)

    async def test_telegram_get_file_failure_is_handled(self):
        self.large_photo.get_file.side_effect = TelegramError("private download detail")

        with self.assertLogs(handlers.logger, level="WARNING") as captured:
            await handlers.gold_photo(self.update, self.context)

        self.assert_nonempty_fallback()
        self.telegram_file.download_as_bytearray.assert_not_awaited()
        self.client_factory.assert_not_called()
        self.assertNotIn("private download detail", "\n".join(captured.output))

    async def test_long_unicode_output_is_preserved_in_telegram_sized_chunks(self):
        analysis = "📈 ملاحظات تعليمية على الرسم البياني.\n" * 180
        self.client.responses.create.return_value = SimpleNamespace(output_text=analysis)

        await handlers.gold_photo(self.update, self.context)

        chunks = self.replies()
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 2000 for chunk in chunks))
        combined = "".join(chunks)
        self.assertTrue(combined.startswith(analysis.strip()))
        self.assertGreater(len(combined), len(analysis.strip()))
        for call in self.message.reply_text.await_args_list:
            self.assertIsNone(call.kwargs["parse_mode"])

    async def test_global_network_error_redacts_url_without_replying(self):
        private_url = "https://api.telegram.org/botFAKE_BOT_TOKEN/getUpdates"
        self.context.error = NetworkError(private_url)

        with self.assertLogs(handlers.logger, level="WARNING") as captured:
            await handlers.error_handler(self.update, self.context)

        self.message.reply_text.assert_not_awaited()
        output = "\n".join(captured.output)
        self.assertIn("NetworkError", output)
        self.assertNotIn(private_url, output)
        self.assertNotIn("FAKE_BOT_TOKEN", output)

    async def test_global_unexpected_error_redacts_exception_and_update(self):
        private_error = "PRIVATE_ERROR test-only-api-key"
        private_update = "PRIVATE_UPDATE chart-and-user-data"
        self.context.error = RuntimeError(private_error)
        update = MagicMock(spec=handlers.Update)
        update.effective_message = self.message
        update.__str__.return_value = private_update

        with self.assertLogs(handlers.logger, level="ERROR") as captured:
            await handlers.error_handler(update, self.context)

        self.assert_nonempty_fallback()
        output = "\n".join(captured.output + self.replies())
        self.assertIn("RuntimeError", output)
        self.assertNotIn(private_error, output)
        self.assertNotIn(private_update, output)
        self.assertNotIn(self.api_key, output)


if __name__ == "__main__":
    unittest.main()
