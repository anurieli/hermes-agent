"""Auto-attached "🗑 Dismiss" button for cron/status Telegram deliveries.

Covers the transport-layer dismiss button: a ``dismissible``-flagged send
attaches a single delete-this-message button, tapping it deletes the message
with no agent turn, unauthorized taps are rejected, a failed delete falls back
to clearing the keyboard, and deliveries that already carry their own inline
keyboard (agent-authored buttons, transcript actions) never get a redundant
dismiss button glued on. See ``cron/scheduler.py``'s ``cron.dismissible_deliveries``
config for the caller side that sets the flag on cron/status deliveries.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as telegram_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter, _DISMISS_CALLBACK_DATA


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


def _sent_adapter():
    """Adapter wired up for exercising send()."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        send_chat_action=AsyncMock(),
    )
    return adapter


def _callback_adapter(*, authorized=True):
    """Adapter for exercising _handle_callback_query in isolation.

    Fully constructed (not object.__new__) so ``self.platform``/``self.name``
    are set — delete_message()'s failure-path logging reads ``self.name``.
    """
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = SimpleNamespace(
        delete_message=AsyncMock(return_value=True),
    )
    adapter._is_callback_user_authorized = lambda *_args, **_kwargs: authorized
    return adapter


def _query(data, *, user_id="111", chat_id=123, message_id=700, thread_id=None):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(
            chat_id=chat_id,
            chat=SimpleNamespace(type="private"),
            message_thread_id=thread_id,
            message_id=message_id,
        ),
        from_user=SimpleNamespace(id=user_id, first_name="Ariel"),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


# --- Keyboard attachment ---------------------------------------------------


@pytest.mark.asyncio
async def test_dismissible_metadata_attaches_dismiss_keyboard():
    adapter = _sent_adapter()

    result = await adapter.send(
        "123", "Model usage, last 24h: …", metadata={"dismissible": True}
    )

    assert result.success
    keyboard = adapter._bot.send_message.await_args.kwargs["reply_markup"]
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == 1
    assert buttons[0].text == "🗑 Dismiss"
    assert buttons[0].callback_data == _DISMISS_CALLBACK_DATA


@pytest.mark.asyncio
async def test_plain_send_without_flag_has_no_keyboard():
    adapter = _sent_adapter()

    await adapter.send("123", "Just a normal reply")

    assert adapter._bot.send_message.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_dismissible_never_double_stacks_with_transcript_actions():
    """A delivery that already carries its own keyboard keeps only that one."""
    adapter = _sent_adapter()

    result = await adapter.send(
        "123",
        '🎙️ "hello"',
        metadata={
            "dismissible": True,
            "telegram_transcript_text": "hello",
            "telegram_transcript_original_message_id": "40",
        },
    )

    assert result.success
    keyboard = adapter._bot.send_message.await_args.kwargs["reply_markup"]
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "🗑 Dismiss" not in labels
    assert any("Done" in label for label in labels)


def test_metadata_is_dismissible_reads_the_flag():
    assert TelegramAdapter._metadata_is_dismissible({"dismissible": True}) is True
    assert TelegramAdapter._metadata_is_dismissible({"dismissible": False}) is False
    assert TelegramAdapter._metadata_is_dismissible(None) is False
    assert TelegramAdapter._metadata_is_dismissible({}) is False


# --- Dismiss callback -------------------------------------------------------


@pytest.mark.asyncio
async def test_tap_dismiss_deletes_message_without_agent_turn():
    adapter = _callback_adapter()
    query = _query(_DISMISS_CALLBACK_DATA)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    query.answer.assert_awaited_once_with()
    adapter._bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=700)
    # Delete succeeded — no need to fall back to clearing the keyboard.
    query.edit_message_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_tap_dismiss_rejects_unauthorized_user():
    adapter = _callback_adapter(authorized=False)
    query = _query(_DISMISS_CALLBACK_DATA, user_id="999")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert "not authorized" in query.answer.call_args.kwargs["text"].lower()
    adapter._bot.delete_message.assert_not_called()
    query.edit_message_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_delete_failure_falls_back_to_clearing_keyboard():
    adapter = _callback_adapter()
    # Simulate Telegram's 48h delete window (or an already-gone message).
    adapter._bot.delete_message = AsyncMock(side_effect=Exception("message can't be deleted"))
    query = _query(_DISMISS_CALLBACK_DATA)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    query.answer.assert_awaited_once_with()
    adapter._bot.delete_message.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_dismiss_callback_survives_keyboard_clear_failure():
    """Even the fallback failing must not raise out of the handler."""
    adapter = _callback_adapter()
    adapter._bot.delete_message = AsyncMock(side_effect=Exception("too old"))
    query = _query(_DISMISS_CALLBACK_DATA)
    query.edit_message_reply_markup = AsyncMock(side_effect=Exception("message gone"))

    # Should not raise.
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    query.answer.assert_awaited_once_with()
