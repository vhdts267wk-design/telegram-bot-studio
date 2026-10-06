"""Manual MT5 menu wording with synthetic Telegram updates only."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bot import handlers, market_monitor


class ManualTicketUITests(unittest.IsolatedAsyncioTestCase):
    async def test_start_help_and_about_explain_native_human_click_in_manual_mode(self):
        context = SimpleNamespace(bot_data={market_monitor.SERVICE_KEY: SimpleNamespace(manual_tickets_enabled=True)})
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=101, first_name="Owner"))
        with patch.object(handlers.commands, "menu_commands", return_value=[]), patch.object(
            handlers.commands, "reply_menu_buttons", return_value=[]
        ):
            for command in (handlers.start, handlers.help_command, handlers.about):
                message.reply_text.reset_mock()
                await command(update, context)
                text = message.reply_text.await_args.args[0]
                self.assertIn("جهّز على اللابتوب", text)
                self.assertIn("TP وSL فقط", text)
                self.assertIn("Buy أو Sell بنفسك", text)
                self.assertNotIn("Accept", text)
