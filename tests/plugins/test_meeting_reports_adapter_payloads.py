"""Native Slack and Telegram meeting-report delivery payloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.meeting_reports.cards import REVIEW_ACTIONS
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_slack_native_card_has_report_link_unique_actions_and_thread_origin():
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._app = object()
    adapter._ensure_dm_conversation = AsyncMock(return_value="C1")
    adapter._metadata_team_id = lambda metadata: None
    adapter._resolve_thread_ts = lambda reply_to, metadata: metadata.get("thread_ts")
    client = SimpleNamespace(chat_postMessage=AsyncMock(return_value={"ts": "123.4"}))
    adapter._get_client = lambda chat_id: client

    result = asyncio.run(
        adapter.send_meeting_report_card(
            chat_id="C1",
            report_id="mtgrpt-native-1",
            title="Meeting ready: Sync",
            body="Summary",
            buttons=REVIEW_ACTIONS,
            report_url="https://example.test/report",
            metadata={"thread_ts": "100.2"},
        )
    )

    assert result.success is True
    payload = client.chat_postMessage.await_args.kwargs
    assert payload["thread_ts"] == "100.2"
    assert payload["mrkdwn"] is False
    assert payload["blocks"][0]["text"]["type"] == "plain_text"
    action_blocks = [block for block in payload["blocks"] if block["type"] == "actions"]
    assert len(action_blocks) == 2
    elements = [element for block in action_blocks for element in block["elements"]]
    assert elements[0]["url"] == "https://example.test/report"
    action_ids = [element["action_id"] for element in elements]
    assert len(action_ids) == len(set(action_ids))
    by_action_id = {element["action_id"]: element for element in elements}
    assert by_action_id["meeting_report_review_accept"]["style"] == "primary"
    assert by_action_id["meeting_report_review_reject"]["style"] == "danger"


def test_telegram_native_card_has_report_link_callbacks_and_topic_origin():
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._bot = object()
    adapter._reply_to_mode = "off"
    adapter._metadata_thread_id = lambda metadata: metadata.get("thread_id")
    adapter._thread_kwargs_for_send = lambda *args, **kwargs: {"message_thread_id": 77}
    adapter._notification_kwargs = lambda metadata: {}
    adapter._link_preview_kwargs = lambda: {}
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=SimpleNamespace(message_id=42)
    )

    result = asyncio.run(
        adapter.send_meeting_report_card(
            chat_id="-1001",
            report_id="mtgrpt-native-2",
            title="Meeting ready: Sync",
            body="Summary",
            buttons=REVIEW_ACTIONS,
            report_url="https://example.test/report",
            metadata={"thread_id": 77},
        )
    )

    assert result.success is True
    payload = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert payload["message_thread_id"] == 77
    rows = payload["reply_markup"].inline_keyboard
    assert rows[0][0].url == "https://example.test/report"
    callbacks = [row[0].callback_data for row in rows[1:]]
    assert callbacks == [
        f"mtgrpt:{action}:mtgrpt-native-2" for action, _label in REVIEW_ACTIONS
    ]
