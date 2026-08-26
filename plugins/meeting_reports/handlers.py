"""Review action handlers for Telegram and Slack completion cards."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from .cards import (
    SLACK_VIEW_CALLBACK_ID,
    build_completion_card,
    parse_slack_action_value,
    parse_telegram_callback,
    to_slack_blocks,
    to_telegram_reply_markup,
)
from .pipeline import review_report
from .review import ReviewNotesRequiredError
from .store import MeetingReportStore, get_default_store

_NOTE_ACTIONS = frozenset({"accept_with_notes", "reject_with_notes"})
_NOTE_BLOCK_ID = "meeting_report_notes_block"
_NOTE_INPUT_ID = "meeting_report_notes_input"
_PENDING_NOTE_TTL_SECONDS = 10 * 60
_MAX_PENDING_NOTE_PROMPTS = 256


@dataclass
class _PendingTelegramNotes:
    report_id: str
    action: str
    actor: Optional[str]
    user_id: str
    store: MeetingReportStore
    source_message: Any
    expires_at: float


_PENDING_TELEGRAM_NOTES: "OrderedDict[tuple[str, int], _PendingTelegramNotes]" = (
    OrderedDict()
)


def _actor_from_slack(body: dict[str, Any]) -> Optional[str]:
    user = body.get("user") or {}
    return user.get("username") or user.get("name") or user.get("id")


def _actor_from_telegram(user: Any) -> Optional[str]:
    if user is None:
        return None
    return getattr(user, "username", None) or str(getattr(user, "id", "") or "") or None


def _outcome_text(outcome: Any) -> str:
    if outcome is None:
        return "This meeting report is unavailable or expired."
    if outcome.changed:
        return f"Review recorded: {outcome.report.review.status.replace('_', ' ')}."
    return f"Already reviewed: {outcome.report.review.status.replace('_', ' ')}."


async def _post_slack_response(
    response_url: Optional[str], payload: dict[str, Any]
) -> None:
    if not response_url:
        return
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(response_url, json=payload) as response:
                if response.status >= 400:
                    await response.read()
    except Exception:
        # The decision is already persisted. Slack delivery failure must not
        # undo it or trigger duplicate workflow effects.
        return


def _slack_message_payload(outcome: Any) -> dict[str, Any]:
    if outcome is None:
        return {
            "replace_original": True,
            "text": "This meeting report is unavailable or expired.",
        }
    card = build_completion_card(outcome.report)
    return {
        "replace_original": True,
        "text": _outcome_text(outcome),
        "blocks": to_slack_blocks(card),
    }


def _slack_notes_modal(
    *, report_id: str, action: str, response_url: Optional[str], actor: Optional[str]
) -> dict[str, Any]:
    metadata = json.dumps(
        {
            "report_id": report_id,
            "action": action,
            "response_url": response_url,
            "actor": actor,
        },
        separators=(",", ":"),
    )
    decision = "accept" if action.startswith("accept") else "reject"
    return {
        "type": "modal",
        "callback_id": SLACK_VIEW_CALLBACK_ID,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Review meeting"},
        "submit": {"type": "plain_text", "text": "Save review"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": _NOTE_BLOCK_ID,
                "label": {"type": "plain_text", "text": f"Notes to {decision} with"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _NOTE_INPUT_ID,
                    "multiline": True,
                },
            }
        ],
    }


async def handle_slack_action(
    ack: Any,
    body: dict[str, Any],
    action: dict[str, Any],
    *,
    client: Any = None,
    store: Optional[MeetingReportStore] = None,
) -> None:
    """Apply a Slack action or open a note-collection modal."""
    parsed = parse_slack_action_value(action.get("value", ""))
    await ack()
    if parsed is None:
        return
    verb, report_id = parsed
    response_url = body.get("response_url")
    actor = _actor_from_slack(body)
    store = store or get_default_store()

    if verb == "dismiss":
        await _post_slack_response(
            response_url,
            {"delete_original": True, "text": "Meeting report dismissed."},
        )
        return

    if verb in _NOTE_ACTIONS:
        existing = store.load(report_id)
        if existing is None or not store.is_available(report_id):
            await _post_slack_response(response_url, _slack_message_payload(None))
            return
        if existing.review.terminal:
            outcome = review_report(report_id, "accept", actor=actor, store=store)
            await _post_slack_response(response_url, _slack_message_payload(outcome))
            return
        trigger_id = body.get("trigger_id")
        if client is None or not trigger_id:
            await _post_slack_response(
                response_url,
                {
                    "replace_original": False,
                    "response_type": "ephemeral",
                    "text": "Slack could not open the notes form. Please try again.",
                },
            )
            return
        await client.views_open(
            trigger_id=trigger_id,
            view=_slack_notes_modal(
                report_id=report_id,
                action=verb,
                response_url=response_url,
                actor=actor,
            ),
        )
        return

    outcome = review_report(report_id, verb, actor=actor, store=store)
    await _post_slack_response(response_url, _slack_message_payload(outcome))


async def handle_slack_view_submission(
    ack: Any,
    body: dict[str, Any],
    view: dict[str, Any],
    *,
    client: Any = None,
    store: Optional[MeetingReportStore] = None,
) -> None:
    """Persist notes submitted through the meeting-review modal."""
    del client
    try:
        metadata = json.loads(view.get("private_metadata") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    state = view.get("state", {}).get("values", {})
    notes = str(
        state.get(_NOTE_BLOCK_ID, {}).get(_NOTE_INPUT_ID, {}).get("value") or ""
    ).strip()
    report_id = str(metadata.get("report_id") or "")
    action = str(metadata.get("action") or "")
    if not notes:
        await ack(
            response_action="errors", errors={_NOTE_BLOCK_ID: "Notes are required."}
        )
        return
    if not report_id or action not in _NOTE_ACTIONS:
        await ack(
            response_action="errors",
            errors={_NOTE_BLOCK_ID: "This review request is invalid."},
        )
        return
    await ack()
    try:
        outcome = review_report(
            report_id,
            action,
            notes=notes,
            actor=metadata.get("actor") or _actor_from_slack(body),
            store=store,
        )
    except ReviewNotesRequiredError:
        return
    await _post_slack_response(
        metadata.get("response_url"), _slack_message_payload(outcome)
    )


async def handle_slack_open(
    ack: Any, body: dict[str, Any], action: dict[str, Any]
) -> None:
    """Acknowledge Slack's interaction payload for the URL button."""
    del body, action
    await ack()


def _prune_pending_notes() -> None:
    # Keep expired prompts as bounded tombstones so a late ForceReply is
    # consumed here instead of leaking into the agent conversation.
    while len(_PENDING_TELEGRAM_NOTES) > _MAX_PENDING_NOTE_PROMPTS:
        _PENDING_TELEGRAM_NOTES.popitem(last=False)


def _telegram_markup(data: Optional[dict[str, Any]]) -> Any:
    if data is None:
        return None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup([
            [InlineKeyboardButton(**button) for button in row]
            for row in data["inline_keyboard"]
        ])
    except Exception:
        return data


async def _edit_telegram_card(message: Any, outcome: Any) -> None:
    if message is None or outcome is None:
        return
    card = build_completion_card(outcome.report)
    try:
        await message.edit_text(
            f"{card.title}\n\n{card.body}\n\n{_outcome_text(outcome)}",
            reply_markup=_telegram_markup(to_telegram_reply_markup(card)),
        )
    except Exception:
        return


async def handle_telegram_callback(
    adapter: Any,
    query: Any,
    data: str,
    *,
    store: Optional[MeetingReportStore] = None,
) -> None:
    """Apply a Telegram card action or request a ForceReply note."""
    parsed = parse_telegram_callback(data)
    if parsed is None:
        await query.answer(text="Unknown meeting-report action.")
        return
    action, report_id = parsed
    store = store or get_default_store()

    if action == "dismiss":
        await query.answer(text="Dismissed.")
        try:
            await query.message.edit_text(
                "Meeting report dismissed. Review remains pending."
            )
        except Exception:
            pass
        return

    actor = _actor_from_telegram(getattr(query, "from_user", None))
    if action in _NOTE_ACTIONS:
        existing = store.load(report_id)
        if existing is None or not store.is_available(report_id):
            await query.answer(text="This meeting report is unavailable or expired.")
            return
        if existing.review.terminal:
            outcome = review_report(report_id, "accept", actor=actor, store=store)
            await query.answer(text=_outcome_text(outcome)[:200])
            await _edit_telegram_card(getattr(query, "message", None), outcome)
            return
        _prune_pending_notes()
        try:
            from telegram import ForceReply

            reply_markup = ForceReply(
                selective=True,
                input_field_placeholder="Type your review notes…",
            )
        except Exception:
            reply_markup = None
        prompt = await query.message.reply_text(
            "Reply to this message with your review notes.",
            reply_markup=reply_markup,
        )
        chat_id = str(getattr(query.message, "chat_id", ""))
        prompt_id = int(getattr(prompt, "message_id"))
        _PENDING_TELEGRAM_NOTES[(chat_id, prompt_id)] = _PendingTelegramNotes(
            report_id=report_id,
            action=action,
            actor=actor,
            user_id=str(getattr(query.from_user, "id", "")),
            store=store,
            source_message=query.message,
            expires_at=time.monotonic() + _PENDING_NOTE_TTL_SECONDS,
        )
        _prune_pending_notes()
        await query.answer(text="Reply with your notes.")
        return

    outcome = review_report(report_id, action, actor=actor, store=store)
    await query.answer(text=_outcome_text(outcome)[:200])
    await _edit_telegram_card(getattr(query, "message", None), outcome)


async def consume_telegram_note_reply(adapter: Any, message: Any) -> bool:
    """Consume a reply to a plugin-owned review-notes ForceReply prompt."""
    del adapter
    reply = getattr(message, "reply_to_message", None)
    prompt_id = getattr(reply, "message_id", None)
    chat_id = str(getattr(message, "chat_id", ""))
    if prompt_id is None:
        return False
    _prune_pending_notes()
    pending = _PENDING_TELEGRAM_NOTES.pop((chat_id, int(prompt_id)), None)
    if pending is None:
        return False
    if pending.expires_at <= time.monotonic():
        await message.reply_text(
            "This review-notes prompt expired. Tap the card and try again."
        )
        return True
    reply_user_id = str(getattr(getattr(message, "from_user", None), "id", ""))
    if pending.user_id and reply_user_id != pending.user_id:
        _PENDING_TELEGRAM_NOTES[(chat_id, int(prompt_id))] = pending
        await message.reply_text(
            "Only the person who opened this review can add its notes."
        )
        return True
    notes = str(getattr(message, "text", "") or "").strip()
    if not notes:
        await message.reply_text(
            "Notes cannot be empty. Tap the review button and try again."
        )
        return True
    outcome = review_report(
        pending.report_id,
        pending.action,
        notes=notes,
        actor=pending.actor
        or _actor_from_telegram(getattr(message, "from_user", None)),
        store=pending.store,
    )
    await message.reply_text(_outcome_text(outcome))
    await _edit_telegram_card(pending.source_message, outcome)
    return True


__all__ = [
    "consume_telegram_note_reply",
    "handle_slack_action",
    "handle_slack_open",
    "handle_slack_view_submission",
    "handle_telegram_callback",
]
