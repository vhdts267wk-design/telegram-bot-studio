import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs

import httpx
import httpx2
from openai import APIConnectionError, InternalServerError, RateLimitError
from telegram import Update
from telegram.error import NetworkError, TelegramError
from telegram.request import HTTPXRequest

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
        self.update = SimpleNamespace(
            effective_message=self.message, effective_user=SimpleNamespace(id=101)
        )
        self.context = SimpleNamespace(bot_data={})

        self.client = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(
                    return_value=SimpleNamespace(
                        status="completed", output_text="Visible chart observations."
                    )
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
        self.client.responses.create.return_value = SimpleNamespace(
            status="completed", output_text="   "
        )

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

    def provider_status_error(self, code, *, status=429):
        request = httpx2.Request(
            "POST",
            "https://private-user:private-password@local.invalid/PRIVATE_REQUEST_URL",
            headers={"Authorization": "Bearer PRIVATE_REQUEST_TOKEN"},
        )
        response = httpx2.Response(
            status,
            request=request,
            headers={
                "x-request-id": "PRIVATE_REQUEST_ID",
                "x-private": "PRIVATE_RESPONSE_HEADER",
            },
        )
        error_class = RateLimitError if status == 429 else InternalServerError
        return error_class(
            "PRIVATE_ERROR_MESSAGE test-only-api-key",
            response=response,
            body={
                "message": "PRIVATE_RESPONSE_BODY test-only-api-key",
                "code": code,
                "type": "PRIVATE_ERROR_TYPE",
                "param": "PRIVATE_ERROR_PARAM",
            },
        )

    def assert_provider_content_is_private(self, output):
        for private in (
            "PRIVATE_",
            "private-user",
            "private-password",
            "local.invalid",
            self.api_key,
        ):
            self.assertNotIn(private, output)

    async def test_documented_quota_codes_are_safe_and_do_not_suggest_waiting(self):
        for code in (
            "insufficient_quota",
            "credit_balance_exhausted",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
        ):
            with self.subTest(code=code):
                self.context.bot_data.clear()
                self.message.reply_text.reset_mock()
                self.client.responses.create.side_effect = self.provider_status_error(code)
                with self.assertLogs(handlers.logger, level="WARNING") as captured:
                    await handlers.gold_photo(self.update, self.context)
                self.assert_nonempty_fallback()
                log = "\n".join(captured.output)
                self.assertIn("RateLimitError; status=429; code=" + code, log)
                reply = self.replies()[0]
                self.assertIn("credits or usage limits", reply)
                self.assertNotIn("try again later", reply)
                self.assert_provider_content_is_private(log + reply)

    async def test_documented_throttle_codes_recommend_waiting_safely(self):
        for code in ("rate_limit_exceeded", "slow_down"):
            with self.subTest(code=code):
                self.context.bot_data.clear()
                self.message.reply_text.reset_mock()
                self.client.responses.create.side_effect = self.provider_status_error(code)
                with self.assertLogs(handlers.logger, level="WARNING") as captured:
                    await handlers.gold_photo(self.update, self.context)
                self.assert_nonempty_fallback()
                log = "\n".join(captured.output)
                self.assertIn("RateLimitError; status=429; code=" + code, log)
                reply = self.replies()[0]
                self.assertIn("temporarily rate limited", reply)
                self.assert_provider_content_is_private(log + reply)

    async def test_documented_overload_code_retains_generic_feedback(self):
        self.client.responses.create.side_effect = self.provider_status_error(
            "server_is_overloaded", status=503
        )
        with self.assertLogs(handlers.logger, level="WARNING") as captured:
            await handlers.gold_photo(self.update, self.context)
        log = "\n".join(captured.output)
        self.assertIn("InternalServerError; status=503; code=server_is_overloaded", log)
        self.assertIn("temporarily unavailable", self.replies()[0])
        self.assert_provider_content_is_private(log + self.replies()[0])

    async def test_unexpected_provider_codes_are_not_logged_or_echoed(self):
        for code in (
            "rate_limit_exceeded\nPRIVATE_MALICIOUS_CODE test-only-api-key",
            "PRIVATE_UNRECOGNIZED_CODE",
            {"PRIVATE_BODY_CODE": self.api_key},
            None,
        ):
            with self.subTest(code_type=type(code).__name__):
                self.context.bot_data.clear()
                self.message.reply_text.reset_mock()
                self.client.responses.create.side_effect = self.provider_status_error(code)
                with self.assertLogs(handlers.logger, level="WARNING") as captured:
                    await handlers.gold_photo(self.update, self.context)
                log = "\n".join(captured.output)
                self.assertIn("RateLimitError; status=429; code=unavailable", log)
                self.assertIn("temporarily unavailable", self.replies()[0])
                self.assert_provider_content_is_private(log + self.replies()[0])

    async def test_invalid_status_metadata_is_not_logged_or_used_for_feedback(self):
        for status in ("PRIVATE_STATUS", 9999, True, None):
            with self.subTest(status_type=type(status).__name__):
                self.context.bot_data.clear()
                self.message.reply_text.reset_mock()
                error = self.provider_status_error("insufficient_quota")
                error.status_code = status
                self.client.responses.create.side_effect = error
                with self.assertLogs(handlers.logger, level="WARNING") as captured:
                    await handlers.gold_photo(self.update, self.context)
                log = "\n".join(captured.output)
                self.assertIn("status=unavailable; code=insufficient_quota", log)
                self.assertIn("temporarily unavailable", self.replies()[0])
                self.assert_provider_content_is_private(log + self.replies()[0])

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
        self.client.responses.create.return_value = SimpleNamespace(
            status="completed", output_text=analysis
        )

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

    async def test_error_reply_failure_is_safely_handled(self):
        self.context.error = RuntimeError("private original detail")
        self.message.reply_text.side_effect = TelegramError("private reply token")
        update = MagicMock(spec=handlers.Update)
        update.effective_message = self.message

        with self.assertLogs(handlers.logger, level="WARNING") as captured:
            await handlers.error_handler(update, self.context)

        self.message.reply_text.assert_awaited_once()
        output = "\n".join(captured.output)
        self.assertIn("TelegramError", output)
        self.assertNotIn("private original detail", output)
        self.assertNotIn("private reply token", output)

    async def test_chart_admission_limits_concurrency_before_work_starts(self):
        started = asyncio.Event()
        release = asyncio.Event()
        running = []

        async def slow_analysis(message, api_key):
            running.append(message)
            if len(running) == 2:
                started.set()
            await release.wait()

        def another_update(user_id):
            message = SimpleNamespace(
                photo=[SimpleNamespace(get_file=AsyncMock())], reply_text=AsyncMock()
            )
            return SimpleNamespace(
                effective_message=message, effective_user=SimpleNamespace(id=user_id)
            )

        second = another_update(102)
        duplicate = another_update(101)
        excess = another_update(103)
        with patch.object(handlers, "_analyze_gold_photo", side_effect=slow_analysis):
            tasks = [
                asyncio.create_task(handlers.gold_photo(self.update, self.context)),
                asyncio.create_task(handlers.gold_photo(second, self.context)),
            ]
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                await handlers.gold_photo(duplicate, self.context)
                await handlers.gold_photo(excess, self.context)
                self.assertEqual(len(running), 2)
                duplicate.effective_message.reply_text.assert_awaited_once()
                excess.effective_message.reply_text.assert_awaited_once()
                duplicate.effective_message.photo[0].get_file.assert_not_awaited()
                excess.effective_message.photo[0].get_file.assert_not_awaited()
            finally:
                release.set()
                await asyncio.gather(*tasks)

        self.assertEqual(
            self.context.bot_data[handlers.GOLD_ADMISSION_KEY].active_users, set()
        )

    async def test_cancellation_releases_chart_slot(self):
        started = asyncio.Event()

        async def slow_analysis(message, api_key):
            started.set()
            await asyncio.Event().wait()

        with patch.object(handlers, "_analyze_gold_photo", side_effect=slow_analysis):
            task = asyncio.create_task(handlers.gold_photo(self.update, self.context))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(
            self.context.bot_data[handlers.GOLD_ADMISSION_KEY].active_users, set()
        )

    async def test_cooldown_expires_and_prunes_previous_users(self):
        with patch.object(handlers, "monotonic", return_value=100.0):
            await handlers.gold_photo(self.update, self.context)
            await handlers.gold_photo(self.update, self.context)
        self.client.responses.create.assert_awaited_once()
        admission = self.context.bot_data[handlers.GOLD_ADMISSION_KEY]
        self.assertEqual(admission.request_times, {101: 100.0})

        # An expired timestamp from a different user must also be removed.
        admission.request_times[202] = 100.0
        with patch.object(handlers, "monotonic", return_value=131.0):
            await handlers.gold_photo(self.update, self.context)
        self.assertEqual(self.client.responses.create.await_count, 2)
        self.assertEqual(admission.request_times, {101: 131.0})

    async def test_chart_admission_is_isolated_between_applications(self):
        await handlers.gold_photo(self.update, self.context)
        await handlers.gold_photo(self.update, SimpleNamespace(bot_data={}))
        self.assertEqual(self.client.responses.create.await_count, 2)

    async def test_provider_failure_releases_chart_slot(self):
        self.client.responses.create.side_effect = APIConnectionError(
            message="private test detail", request=MagicMock()
        )
        with self.assertLogs(handlers.logger, level="WARNING"):
            await handlers.gold_photo(self.update, self.context)
        self.assertEqual(
            self.context.bot_data[handlers.GOLD_ADMISSION_KEY].active_users, set()
        )


class GoldPhotoRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_chart_does_not_block_ping_and_edited_photos_are_ignored(self):
        sent_texts = []
        started = asyncio.Event()
        release = asyncio.Event()

        def telegram_response(request):
            if request.url.path.endswith("/getMe"):
                result = {
                    "id": 999,
                    "is_bot": True,
                    "first_name": "Local test bot",
                    "username": "local_test_bot",
                }
            elif request.url.path.endswith("/sendMessage"):
                params = parse_qs(request.content.decode())
                text = params["text"][0]
                sent_texts.append(text)
                result = {
                    "message_id": len(sent_texts),
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                    "text": text,
                }
            else:
                raise AssertionError("Unexpected local Telegram request")
            return httpx.Response(200, json={"ok": True, "result": result})

        def local_request():
            return HTTPXRequest(
                httpx_kwargs={"transport": httpx.MockTransport(telegram_response)}
            )

        application = (
            handlers.Application.builder()
            .token("123:local-test-token")
            .request(local_request())
            .get_updates_request(local_request())
            .build()
        )
        handlers.register_handlers(application)

        def photo_update(*, edited=False):
            key = "edited_message" if edited else "message"
            return Update.de_json(
                {
                    "update_id": 1 if edited else 2,
                    key: {
                        "message_id": 1,
                        "date": 1,
                        "chat": {"id": 101, "type": "private"},
                        "from": {"id": 101, "is_bot": False, "first_name": "User"},
                        "photo": [
                            {
                                "file_id": "local-photo",
                                "file_unique_id": "local-unique-photo",
                                "width": 100,
                                "height": 100,
                            }
                        ],
                    },
                },
                application.bot,
            )

        ping_update = Update.de_json(
            {
                "update_id": 3,
                "message": {
                    "message_id": 2,
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                    "from": {"id": 101, "is_bot": False, "first_name": "User"},
                    "text": "/ping",
                    "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
                },
            },
            application.bot,
        )

        async def slow_analysis(message, api_key):
            started.set()
            await release.wait()

        with patch.dict(
            handlers.os.environ, {"OPENAI_API_KEY": "local-test-key"}, clear=True
        ), patch.object(
            handlers, "_analyze_gold_photo", side_effect=slow_analysis
        ) as analyze:
            await application.initialize()
            await application.start()
            try:
                await application.process_update(photo_update(edited=True))
                await asyncio.sleep(0)
                analyze.assert_not_awaited()
                await asyncio.wait_for(
                    application.process_update(photo_update()), timeout=1
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                await asyncio.wait_for(
                    application.process_update(ping_update), timeout=1
                )
                self.assertEqual(sent_texts, ["pong"])
                self.assertFalse(release.is_set())
                analyze.assert_awaited_once()
            finally:
                release.set()
                await application.stop()
                await application.shutdown()

        self.assertEqual(
            application.bot_data[handlers.GOLD_ADMISSION_KEY].active_users, set()
        )


if __name__ == "__main__":
    unittest.main()
