"""Authorization boundary for plugin-provided Telegram callbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from plugins.platforms.telegram.adapter import TelegramAdapter


def _update(data: str):
    query = SimpleNamespace(
        data=data,
        message=SimpleNamespace(
            chat_id=42,
            chat=SimpleNamespace(type="group"),
            message_thread_id=7,
        ),
        from_user=SimpleNamespace(id=99, first_name="Ariel"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query), query


def test_plugin_callback_rejects_unauthorized_user():
    callback = AsyncMock()
    manager = MagicMock()
    manager.get_telegram_callback_handlers.return_value = [
        ("mtgrpt:", callback, "meeting_reports")
    ]
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._is_callback_user_authorized = MagicMock(return_value=False)
    update, query = _update("mtgrpt:accept:mtgrpt-1")

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        asyncio.run(adapter._handle_callback_query(update, None))

    query.answer.assert_awaited_once_with(
        text="You are not authorized for this action."
    )
    callback.assert_not_awaited()


def test_plugin_callback_preserves_authorized_dispatch():
    callback = AsyncMock()
    manager = MagicMock()
    manager.get_telegram_callback_handlers.return_value = [
        ("mtgrpt:", callback, "meeting_reports")
    ]
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    update, query = _update("mtgrpt:accept:mtgrpt-1")

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        asyncio.run(adapter._handle_callback_query(update, None))

    callback.assert_awaited_once_with(adapter, query, "mtgrpt:accept:mtgrpt-1")


def test_plugin_callback_cannot_shadow_core_update_prompt():
    callback = AsyncMock()
    manager = MagicMock()
    manager.get_telegram_callback_handlers.return_value = [
        ("update_prompt:", callback, "bad_plugin")
    ]
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    adapter.format_message = lambda text: text
    update, query = _update("update_prompt:y")

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        asyncio.run(adapter._handle_callback_query(update, None))

    callback.assert_not_awaited()
    query.answer.assert_awaited_once_with(text="Sent 'y' to the update process.")
