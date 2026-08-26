"""Interactive Slack and Telegram review handlers."""

from __future__ import annotations

import asyncio

from plugins.meeting_reports.handlers import (
    consume_telegram_note_reply,
    handle_slack_action,
    handle_slack_view_submission,
    handle_telegram_callback,
)
from plugins.meeting_reports.models import MeetingReport
from plugins.meeting_reports.store import MeetingReportStore


def _store(tmp_path) -> MeetingReportStore:
    store = MeetingReportStore(tmp_path / "meeting_reports")
    store.save(MeetingReport(report_id="mtgrpt-h1", title="Weekly Sync", summary="s"))
    return store


class _Ack:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _SlackClient:
    def __init__(self):
        self.calls = []

    async def views_open(self, **kwargs):
        self.calls.append(kwargs)


class _TelegramMessage:
    _next_id = 100

    def __init__(self, *, chat_id=42, text="", reply_to_message=None):
        self.chat_id = chat_id
        self.text = text
        self.reply_to_message = reply_to_message
        self.message_id = _TelegramMessage._next_id
        _TelegramMessage._next_id += 1
        self.edits = []
        self.replies = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def reply_text(self, text, **kwargs):
        prompt = _TelegramMessage(chat_id=self.chat_id)
        self.replies.append((text, kwargs, prompt))
        return prompt


class _User:
    id = 7
    username = "ariel"


class _Query:
    def __init__(self):
        self.message = _TelegramMessage()
        self.from_user = _User()
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text)


def test_telegram_accept_persists_and_edits_card(tmp_path):
    store = _store(tmp_path)
    query = _Query()
    asyncio.run(
        handle_telegram_callback(None, query, "mtgrpt:accept:mtgrpt-h1", store=store)
    )
    assert store.load("mtgrpt-h1").review.status == "accepted"
    assert query.answers
    assert query.message.edits


def test_telegram_duplicate_review_is_idempotent(tmp_path):
    store = _store(tmp_path)
    first, second = _Query(), _Query()
    asyncio.run(
        handle_telegram_callback(None, first, "mtgrpt:accept:mtgrpt-h1", store=store)
    )
    asyncio.run(
        handle_telegram_callback(None, second, "mtgrpt:reject:mtgrpt-h1", store=store)
    )
    assert store.load("mtgrpt-h1").review.status == "accepted"
    assert "Already reviewed" in second.answers[0]


def test_telegram_dismiss_does_not_change_review(tmp_path):
    store = _store(tmp_path)
    query = _Query()
    asyncio.run(
        handle_telegram_callback(None, query, "mtgrpt:dismiss:mtgrpt-h1", store=store)
    )
    assert store.load("mtgrpt-h1").review.status == "pending"
    assert query.message.edits


def test_telegram_with_notes_uses_force_reply_and_consumes_answer(tmp_path):
    store = _store(tmp_path)
    query = _Query()
    asyncio.run(
        handle_telegram_callback(
            None, query, "mtgrpt:accept_with_notes:mtgrpt-h1", store=store
        )
    )
    assert store.load("mtgrpt-h1").review.status == "pending"
    prompt = query.message.replies[0][2]
    reply = _TelegramMessage(
        chat_id=query.message.chat_id,
        text="Looks good to me",
        reply_to_message=prompt,
    )
    reply.from_user = _User()
    consumed = asyncio.run(consume_telegram_note_reply(None, reply))
    assert consumed is True
    report = store.load("mtgrpt-h1")
    assert report.review.status == "accepted_with_notes"
    assert report.review.notes == "Looks good to me"


def test_telegram_note_reply_is_bound_to_the_user_who_opened_it(tmp_path):
    store = _store(tmp_path)
    query = _Query()
    asyncio.run(
        handle_telegram_callback(
            None, query, "mtgrpt:reject_with_notes:mtgrpt-h1", store=store
        )
    )
    prompt = query.message.replies[0][2]
    reply = _TelegramMessage(
        chat_id=query.message.chat_id,
        text="Someone else's notes",
        reply_to_message=prompt,
    )

    class OtherUser:
        id = 8
        username = "other"

    reply.from_user = OtherUser()
    assert asyncio.run(consume_telegram_note_reply(None, reply)) is True
    assert store.load("mtgrpt-h1").review.status == "pending"
    assert "Only the person" in reply.replies[0][0]


def test_expired_telegram_note_reply_is_consumed_not_sent_to_agent(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "plugins.meeting_reports.handlers._PENDING_NOTE_TTL_SECONDS", -1
    )
    store = _store(tmp_path)
    query = _Query()
    asyncio.run(
        handle_telegram_callback(
            None, query, "mtgrpt:accept_with_notes:mtgrpt-h1", store=store
        )
    )
    prompt = query.message.replies[0][2]
    reply = _TelegramMessage(
        chat_id=query.message.chat_id,
        text="Late notes",
        reply_to_message=prompt,
    )
    reply.from_user = _User()

    assert asyncio.run(consume_telegram_note_reply(None, reply)) is True
    assert store.load("mtgrpt-h1").review.status == "pending"
    assert "expired" in reply.replies[0][0]


def test_slack_plain_review_updates_original(monkeypatch, tmp_path):
    store = _store(tmp_path)
    posted = []

    async def _capture(url, payload):
        posted.append((url, payload))

    monkeypatch.setattr(
        "plugins.meeting_reports.handlers._post_slack_response", _capture
    )
    ack = _Ack()
    asyncio.run(
        handle_slack_action(
            ack,
            {"response_url": "https://hooks.slack.test/1", "user": {"id": "U1"}},
            {"value": "reject:mtgrpt-h1"},
            store=store,
        )
    )
    assert len(ack.calls) == 1
    assert store.load("mtgrpt-h1").review.status == "rejected"
    assert posted[0][1]["replace_original"] is True


def test_slack_with_notes_opens_modal_then_persists_submission(monkeypatch, tmp_path):
    store = _store(tmp_path)
    posted = []

    async def _capture(url, payload):
        posted.append((url, payload))

    monkeypatch.setattr(
        "plugins.meeting_reports.handlers._post_slack_response", _capture
    )
    client = _SlackClient()
    action_ack = _Ack()
    asyncio.run(
        handle_slack_action(
            action_ack,
            {
                "trigger_id": "trigger-1",
                "response_url": "https://hooks.slack.test/1",
                "user": {"id": "U1"},
            },
            {"value": "reject_with_notes:mtgrpt-h1"},
            client=client,
            store=store,
        )
    )
    assert store.load("mtgrpt-h1").review.status == "pending"
    modal = client.calls[0]["view"]
    modal["state"] = {
        "values": {
            "meeting_report_notes_block": {
                "meeting_report_notes_input": {"value": "Needs one correction"}
            }
        }
    }
    view_ack = _Ack()
    asyncio.run(handle_slack_view_submission(view_ack, {}, modal, store=store))
    report = store.load("mtgrpt-h1")
    assert report.review.status == "rejected_with_notes"
    assert report.review.notes == "Needs one correction"
    assert posted[0][1]["replace_original"] is True


def test_slack_empty_notes_returns_modal_validation_error(tmp_path):
    store = _store(tmp_path)
    ack = _Ack()
    view = {
        "private_metadata": (
            '{"report_id":"mtgrpt-h1","action":"accept_with_notes",'
            '"response_url":null,"actor":"U1"}'
        ),
        "state": {"values": {}},
    }
    asyncio.run(handle_slack_view_submission(ack, {}, view, store=store))
    assert ack.calls[0]["response_action"] == "errors"
    assert store.load("mtgrpt-h1").review.status == "pending"


def test_malformed_callbacks_are_acknowledged_without_review(tmp_path):
    store = _store(tmp_path)
    query = _Query()
    asyncio.run(handle_telegram_callback(None, query, "bad", store=store))
    assert query.answers == ["Unknown meeting-report action."]
    ack = _Ack()
    asyncio.run(handle_slack_action(ack, {}, {"value": "bad"}, store=store))
    assert len(ack.calls) == 1
    assert store.load("mtgrpt-h1").review.status == "pending"
