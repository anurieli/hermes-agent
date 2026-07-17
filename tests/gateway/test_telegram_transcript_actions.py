from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as telegram_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter


class _CopyTextButton:
    def __init__(self, text):
        self.text = text


class _InlineKeyboardButton:
    def __init__(self, text, callback_data=None, copy_text=None):
        self.text = text
        self.callback_data = callback_data
        self.copy_text = copy_text


class _InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


@pytest.fixture(autouse=True)
def _telegram_keyboard_types(monkeypatch):
    monkeypatch.setattr(telegram_adapter, "CopyTextButton", _CopyTextButton)
    monkeypatch.setattr(telegram_adapter, "InlineKeyboardButton", _InlineKeyboardButton)
    monkeypatch.setattr(telegram_adapter, "InlineKeyboardMarkup", _InlineKeyboardMarkup)


def _adapter():
    adapter = object.__new__(TelegramAdapter)
    adapter._transcript_action_state = OrderedDict()
    return adapter


def test_transcript_keyboard_uses_native_copy_and_separate_done_callback():
    adapter = _adapter()
    state_id = adapter._register_transcript_actions(
        chat_id="123",
        transcript="send this exactly",
        original_message_id="40",
    )

    keyboard = adapter._transcript_actions_keyboard("send this exactly", state_id)
    copy_button = keyboard.inline_keyboard[0][0]
    done_button = keyboard.inline_keyboard[-1][0]

    assert copy_button.copy_text.text == "send this exactly"
    assert copy_button.callback_data is None
    assert done_button.copy_text is None
    assert done_button.callback_data == f"tx:{state_id}:done"


def test_long_transcript_copy_buttons_are_truthful_256_character_chunks():
    adapter = _adapter()
    transcript = "a" * 600
    state_id = adapter._register_transcript_actions(
        chat_id="123",
        transcript=transcript,
        original_message_id="40",
    )

    keyboard = adapter._transcript_actions_keyboard(transcript, state_id)
    copy_buttons = [
        button
        for row in keyboard.inline_keyboard[:-1]
        for button in row
    ]

    assert "".join(button.copy_text.text for button in copy_buttons) == transcript
    assert all(len(button.copy_text.text) <= 256 for button in copy_buttons)
    assert [button.text for button in copy_buttons] == [
        "📋 Copy 1/3",
        "📋 Copy 2/3",
        "📋 Copy 3/3",
    ]


@pytest.mark.asyncio
async def test_transcript_metadata_attaches_actions_and_records_sent_message():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=50)),
        send_chat_action=AsyncMock(),
    )

    result = await adapter.send(
        "123",
        '🎙️ "hello"',
        metadata={
            "telegram_transcript_text": "hello",
            "telegram_transcript_original_message_id": "40",
        },
    )

    assert result.success is True
    markup = adapter._bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].copy_text.text == "hello"
    state = next(iter(adapter._transcript_action_state.values()))
    assert state["message_ids"] == ["50"]


@pytest.mark.asyncio
async def test_done_callback_deletes_transcript_and_original_recording():
    adapter = _adapter()
    adapter._bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
    adapter._is_callback_user_authorized = lambda *_args, **_kwargs: True
    state_id = adapter._register_transcript_actions(
        chat_id="123",
        transcript="hello",
        original_message_id="40",
    )
    adapter._transcript_action_state[state_id]["message_ids"] = ["50"]
    query = SimpleNamespace(
        data=f"tx:{state_id}:done",
        message=SimpleNamespace(
            chat_id=123,
            message_id=50,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
        ),
        from_user=SimpleNamespace(id=7, first_name="Ariel"),
        answer=AsyncMock(),
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    assert adapter._bot.delete_message.await_args_list == [
        call(chat_id=123, message_id=50),
        call(chat_id=123, message_id=40),
    ]
    query.answer.assert_awaited_once_with(text="✓ Got it")
    assert state_id not in adapter._transcript_action_state


@pytest.mark.asyncio
async def test_done_callback_cannot_delete_from_a_forwarded_or_unrelated_message():
    adapter = _adapter()
    adapter._bot = SimpleNamespace(delete_message=AsyncMock(return_value=True))
    adapter._is_callback_user_authorized = lambda *_args, **_kwargs: True
    state_id = adapter._register_transcript_actions(
        chat_id="123",
        transcript="hello",
        original_message_id="40",
    )
    adapter._transcript_action_state[state_id]["message_ids"] = ["50"]
    query = SimpleNamespace(
        data=f"tx:{state_id}:done",
        message=SimpleNamespace(
            chat_id=999,
            message_id=50,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
        ),
        from_user=SimpleNamespace(id=7, first_name="Ariel"),
        answer=AsyncMock(),
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    adapter._bot.delete_message.assert_not_awaited()
    query.answer.assert_awaited_once_with(text="This transcript action expired.")