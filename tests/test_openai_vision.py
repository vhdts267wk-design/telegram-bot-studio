"""Exercise the real SDK against a local HTTP transport, without API access."""

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
from openai import AsyncOpenAI

from bot import handlers


class OpenAIVisionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_photo_request(self, *, status="completed"):
        requests = []
        observation = "The visible screenshot shows a consolidation area."
        image_bytes = bytearray(b"local-test-image")

        def respond(request):
            requests.append(request)
            return httpx2.Response(
                200,
                json={
                    "id": "resp_local_vision",
                    "object": "response",
                    "created_at": 0.0,
                    "status": status,
                    "model": handlers.GOLD_MODEL,
                    "output": [
                        {
                            "id": "msg_local_vision",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": observation,
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "parallel_tool_calls": False,
                    "tools": [],
                    "tool_choice": "auto",
                    "metadata": {},
                },
            )

        clients = []

        def create_client(**kwargs):
            http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
            clients.append(http_client)
            return AsyncOpenAI(http_client=http_client, **kwargs)

        photo = SimpleNamespace(
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    download_as_bytearray=AsyncMock(return_value=image_bytes)
                )
            )
        )
        message = SimpleNamespace(photo=[photo], reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message, effective_user=SimpleNamespace(id=101)
        )

        with patch.dict(
            handlers.os.environ, {"OPENAI_API_KEY": "local-test-key", "OPENAI_ENABLED": "true"}, clear=True
        ), patch.object(handlers, "AsyncOpenAI", side_effect=create_client):
            await handlers.gold_photo(update, SimpleNamespace(bot_data={}))

        return requests, message, clients, observation, image_bytes

    async def test_photo_request_serializes_and_response_text_reaches_telegram(self):
        requests, message, clients, observation, image_bytes = (
            await self.exercise_photo_request()
        )

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1/responses")
        body = json.loads(request.content)
        self.assertEqual(body["model"], handlers.GOLD_MODEL)
        self.assertEqual(body["instructions"], handlers.GOLD_INSTRUCTIONS)
        self.assertFalse(body["store"])
        image = body["input"][0]["content"][1]
        self.assertEqual(image["type"], "input_image")
        self.assertEqual(image["detail"], "high")
        self.assertEqual(
            image["image_url"],
            "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii"),
        )
        message.reply_text.assert_awaited_once()
        self.assertTrue(message.reply_text.await_args.args[0].startswith(observation))
        self.assertIsNone(message.reply_text.await_args.kwargs["parse_mode"])
        self.assertTrue(clients[0].is_closed)

    async def test_incomplete_and_failed_sdk_responses_do_not_publish_partial_text(self):
        for status in ("incomplete", "failed"):
            with self.subTest(status=status), self.assertLogs(
                handlers.logger, level="WARNING"
            ) as captured:
                requests, message, clients, observation, _ = (
                    await self.exercise_photo_request(status=status)
                )
                self.assertEqual(len(requests), 1)
                message.reply_text.assert_awaited_once()
                reply = message.reply_text.await_args.args[0]
                self.assertIn("could not be completed", reply)
                self.assertNotIn(observation, reply)
                self.assertNotIn(observation, "\n".join(captured.output))
                self.assertTrue(clients[0].is_closed)


if __name__ == "__main__":
    unittest.main()
