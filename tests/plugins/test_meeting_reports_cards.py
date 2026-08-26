"""Completion card and Telegram/Slack adapter payload contracts."""

from __future__ import annotations

from plugins.meeting_reports.cards import (
    CALLBACK_PREFIX,
    SLACK_ACTION_PREFIX,
    SLACK_OPEN_ACTION_ID,
    build_completion_card,
    parse_slack_action_value,
    parse_telegram_callback,
    to_slack_blocks,
    to_telegram_reply_markup,
)
from plugins.meeting_reports.models import ActionItem, MeetingReport, ProposedDelegation


def _report(**overrides) -> MeetingReport:
    kwargs = dict(
        report_id="mtgrpt-card-1",
        title="Weekly Sync",
        summary="Shipped the migration doc.\nMore detail on a second line.",
        decisions=["Ship Friday"],
        action_items=[ActionItem(text="Write release notes", owner="Ada")],
        proposed_delegations=[ProposedDelegation(goal="Draft release notes")],
    )
    kwargs.update(overrides)
    return MeetingReport(**kwargs)


def test_completion_card_is_compact_not_a_dump():
    card = build_completion_card(_report())
    assert "Weekly Sync" in card.title
    assert "Shipped the migration doc." in card.body
    assert "report_id" not in card.body
    assert len(card.body) < 400


def test_completion_card_counts_are_accurate():
    card = build_completion_card(_report())
    assert "Decisions: 1" in card.body
    assert "Action items: 1" in card.body
    assert "Proposed delegations: 1" in card.body


def test_pending_report_gets_all_review_and_dismiss_actions():
    card = build_completion_card(_report())
    assert [action for action, _label in card.buttons] == [
        "accept",
        "accept_with_notes",
        "reject",
        "reject_with_notes",
        "dismiss",
    ]


def test_terminal_report_gets_no_review_buttons():
    report = _report()
    report.review.status = "accepted"
    assert build_completion_card(report).buttons == ()


def test_telegram_payload_has_report_link_and_bounded_callbacks():
    card = build_completion_card(_report(report_url="https://example.test/reports/1"))
    rows = to_telegram_reply_markup(card)["inline_keyboard"]
    assert rows[0][0] == {"text": "Open report", "url": card.report_url}
    for row in rows[1:]:
        button = row[0]
        assert button["callback_data"].startswith(f"{CALLBACK_PREFIX}:")
        assert len(button["callback_data"].encode("utf-8")) <= 64


def test_telegram_markup_none_when_terminal_and_no_report_link():
    report = _report()
    report.review.status = "rejected"
    assert to_telegram_reply_markup(build_completion_card(report)) is None


def test_slack_payload_has_unique_action_ids_and_chunks_at_five():
    card = build_completion_card(_report(report_url="https://example.test/reports/1"))
    blocks = to_slack_blocks(card)
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["type"] == "plain_text"
    action_blocks = blocks[1:]
    assert [len(block["elements"]) for block in action_blocks] == [5, 1]
    elements = [element for block in action_blocks for element in block["elements"]]
    assert elements[0]["action_id"] == SLACK_OPEN_ACTION_ID
    review_elements = elements[1:]
    assert len({element["action_id"] for element in review_elements}) == 5
    assert all(
        element["action_id"].startswith(SLACK_ACTION_PREFIX)
        for element in review_elements
    )
    assert [element["value"] for element in review_elements] == [
        f"accept:{card.report_id}",
        f"accept_with_notes:{card.report_id}",
        f"reject:{card.report_id}",
        f"reject_with_notes:{card.report_id}",
        f"dismiss:{card.report_id}",
    ]


def test_slack_terminal_report_keeps_only_open_report_button():
    report = _report(report_url="https://example.test/reports/1")
    report.review.status = "accepted_with_notes"
    blocks = to_slack_blocks(build_completion_card(report))
    assert len(blocks) == 2
    assert blocks[1]["elements"][0]["action_id"] == SLACK_OPEN_ACTION_ID


def test_parse_telegram_callback_round_trip():
    card = build_completion_card(_report())
    data = to_telegram_reply_markup(card)["inline_keyboard"][0][0]["callback_data"]
    assert parse_telegram_callback(data) == ("accept", card.report_id)


def test_parse_callbacks_reject_malformed_values():
    assert parse_telegram_callback("kb:abcd:c0") is None
    assert parse_telegram_callback("not-even-colonized") is None
    assert parse_slack_action_value("") is None
    assert parse_slack_action_value("no-colon-here") is None


def test_parse_slack_action_value_round_trip():
    card = build_completion_card(_report())
    value = to_slack_blocks(card)[1]["elements"][0]["value"]
    assert parse_slack_action_value(value) == ("accept", card.report_id)
