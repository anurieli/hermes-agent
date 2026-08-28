"""Portable completion-card model and Telegram/Slack payload builders."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .models import MeetingReport

CALLBACK_PREFIX = "mtgrpt"
SLACK_ACTION_PREFIX = "meeting_report_review_"
SLACK_ACTION_PATTERN = re.compile(
    rf"^{SLACK_ACTION_PREFIX}(accept|accept_with_notes|reject|reject_with_notes|dismiss)$"
)
_REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$")
SLACK_OPEN_ACTION_ID = "meeting_report_open"
SLACK_VIEW_CALLBACK_ID = "meeting_report_review_notes"
REVIEW_ACTIONS = (
    ("accept", "Accept"),
    ("accept_with_notes", "Accept with notes"),
    ("reject", "Reject"),
    ("reject_with_notes", "Reject with notes"),
    ("dismiss", "Dismiss"),
)


@dataclass(frozen=True)
class CompletionCard:
    report_id: str
    title: str
    body: str
    report_url: Optional[str]
    buttons: tuple[tuple[str, str], ...]


def _first_summary_line(summary: str, max_chars: int = 180) -> str:
    text = " ".join((summary or "").split())
    if not text:
        return "Meeting processing is complete."
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _filing_line(report: MeetingReport) -> str:
    verdict = report.filing_verdict or "not filed"
    line = f"Filing: {verdict}"
    if report.filed_destinations:
        line += "\nFiled to: " + "; ".join(report.filed_destinations)
    return line


def build_completion_card(report: MeetingReport) -> CompletionCard:
    counts = (
        f"Decisions: {len(report.decisions)}  |  "
        f"Action items: {len(report.action_items)}  |  "
        f"Proposed delegations: {len(report.proposed_delegations)}"
    )
    expiry = report.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f"{_first_summary_line(report.summary)}\n{counts}\n"
        f"{_filing_line(report)}\nAvailable until {expiry}."
    )
    buttons = REVIEW_ACTIONS if not report.review.terminal else ()
    return CompletionCard(
        report_id=report.report_id,
        title=f"Meeting ready: {report.title}",
        body=body,
        report_url=report.report_url,
        buttons=buttons,
    )


def telegram_callback_data(action: str, report_id: str) -> str:
    value = f"{CALLBACK_PREFIX}:{action}:{report_id}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("Telegram callback_data exceeds 64 bytes")
    return value


def parse_telegram_callback(data: str) -> Optional[tuple[str, str]]:
    parts = str(data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action, report_id = parts[1], parts[2]
    if action not in {x[0] for x in REVIEW_ACTIONS} or not _REPORT_ID_PATTERN.fullmatch(
        report_id
    ):
        return None
    return action, report_id


def to_telegram_reply_markup(card: CompletionCard) -> Optional[dict[str, Any]]:
    rows: list[list[dict[str, str]]] = []
    if card.report_url:
        rows.append([{"text": "Open report", "url": card.report_url}])
    rows.extend(
        [
            {
                "text": label,
                "callback_data": telegram_callback_data(action, card.report_id),
            }
        ]
        for action, label in card.buttons
    )
    return {"inline_keyboard": rows} if rows else None


def slack_action_value(action: str, report_id: str) -> str:
    return f"{action}:{report_id}"


def parse_slack_action_value(value: str) -> Optional[tuple[str, str]]:
    action, sep, report_id = str(value or "").partition(":")
    if (
        not sep
        or action not in {x[0] for x in REVIEW_ACTIONS}
        or not _REPORT_ID_PATTERN.fullmatch(report_id)
    ):
        return None
    return action, report_id


def to_slack_blocks(card: CompletionCard) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": f"{card.title}\n{card.body}",
                "emoji": True,
            },
        }
    ]
    elements: list[dict[str, Any]] = []
    if card.report_url:
        elements.append({
            "type": "button",
            "action_id": SLACK_OPEN_ACTION_ID,
            "text": {"type": "plain_text", "text": "Open report"},
            "url": card.report_url,
        })
    for action, label in card.buttons:
        element: dict[str, Any] = {
            "type": "button",
            "action_id": f"{SLACK_ACTION_PREFIX}{action}",
            "text": {"type": "plain_text", "text": label},
            "value": slack_action_value(action, card.report_id),
        }
        if action == "accept":
            element["style"] = "primary"
        elif action == "reject":
            element["style"] = "danger"
        elements.append(element)
    for start in range(0, len(elements), 5):
        blocks.append({"type": "actions", "elements": elements[start : start + 5]})
    return blocks
