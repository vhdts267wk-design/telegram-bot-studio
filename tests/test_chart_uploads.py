import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
from telegram import Document, Update
from telegram.ext import Application
from telegram.request import HTTPXRequest

from bot import handlers


class ChartDocumentRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent_texts = []
        self.sent_parameters = []
        self.request_paths = []

        def telegram_response(request):
            self.request_paths.append(request.url.path)
            if request.url.path.endswith("/getMe"):
                result = {
                    "id": 999,
                    "is_bot": True,
                    "first_name": "Local upload test bot",
                    "username": "local_upload_test_bot",
                }
            elif request.url.path.endswith("/sendMessage"):
                parameters = parse_qs(request.content.decode())
                self.sent_parameters.append(parameters)
                text = parameters["text"][0]
                self.sent_texts.append(text)
                result = {
                    "message_id": len(self.sent_texts),
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

        self.application = (
            Application.builder()
            .token("123:local-upload-test-token")
            .request(local_request())
            .get_updates_request(local_request())
            .build()
        )
        handlers.register_handlers(self.application)
        self.download_patch = patch.object(Document, "get_file", new_callable=AsyncMock)
        self.get_file = self.download_patch.start()
        self.addCleanup(self.download_patch.stop)
        self.provider_patch = patch.object(handlers, "AsyncOpenAI")
        self.provider = self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        await self.application.initialize()
        await self.application.start()

    async def asyncTearDown(self):
        await self.application.stop()
        await self.application.shutdown()

    def document_update(self, mime_type, *, edited=False):
        message_key = "edited_message" if edited else "message"
        return Update.de_json(
            {
                "update_id": 1,
                message_key: {
                    "message_id": 1,
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                    "from": {"id": 101, "is_bot": False, "first_name": "Local user"},
                    "document": {
                        "file_id": "local-chart-file",
                        "file_unique_id": "local-chart-unique-file",
                        "file_name": "local-chart-upload",
                        "mime_type": mime_type,
                    },
                },
            },
            self.application.bot,
        )

    def assert_no_chart_processing(self):
        self.get_file.assert_not_awaited()
        self.provider.assert_not_called()
        self.assertFalse(any(path.endswith("/getFile") for path in self.request_paths))

    async def test_png_and_jpeg_documents_receive_plain_photo_upload_guidance(self):
        for mime_type in ("image/png", "image/jpeg"):
            with self.subTest(mime_type=mime_type):
                self.sent_texts.clear()
                self.sent_parameters.clear()
                await self.application.process_update(self.document_update(mime_type))
                self.assertEqual(len(self.sent_texts), 1)
                self.assertIn("Photo", self.sent_texts[0])
                self.assertIn("XAUUSD", self.sent_texts[0])
                self.assertNotIn("parse_mode", self.sent_parameters[0])
                self.assert_no_chart_processing()

    async def test_edited_image_documents_are_ignored(self):
        for mime_type in ("image/png", "image/jpeg"):
            with self.subTest(mime_type=mime_type):
                await self.application.process_update(
                    self.document_update(mime_type, edited=True)
                )
        self.assertEqual(self.sent_texts, [])
        self.assert_no_chart_processing()

    async def test_non_image_documents_are_ignored(self):
        for mime_type in ("application/pdf", "text/plain", "application/octet-stream"):
            with self.subTest(mime_type=mime_type):
                await self.application.process_update(self.document_update(mime_type))
        self.assertEqual(self.sent_texts, [])
        self.assert_no_chart_processing()


if __name__ == "__main__":
    unittest.main()
