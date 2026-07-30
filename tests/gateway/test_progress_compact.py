"""Tests for the Telegram compact live-activity progress card."""

from gateway.progress_compact import render_compact_card


def test_render_compact_card_shows_label_latest_action_count_and_expandable_log():
    card = render_compact_card(
        task_label="Fix the flaky login test",
        latest_action='💻 terminal: "pytest tests/test_login.py"',
        action_count=3,
        log_lines=[
            '🔎 read_file: "test_login.py"',
            '💻 terminal: "pytest -k login"',
            '💻 terminal: "pytest tests/test_login.py"',
        ],
    )
    assert "Fix the flaky login test" in card
    assert '💻 terminal: "pytest tests/test_login.py"' in card
    assert "3 action" in card
    assert card.startswith("📋")
    assert "**>" in card
    assert card.endswith("||")


def test_render_compact_card_truncates_task_and_old_log_lines():
    card = render_compact_card(
        task_label="A" * 100,
        latest_action="Reading the newest file",
        action_count=4,
        log_lines=["old one", "old two", "newest action"],
        max_log_chars=15,
    )
    assert "A" * 79 + "…" in card
    assert "newest action" in card
    assert "earlier actions omitted" in card
    assert "old one" not in card
