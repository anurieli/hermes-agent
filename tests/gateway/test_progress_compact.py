"""Tests for the Telegram compact live-activity progress card."""

from gateway.progress_compact import render_compact_card


def test_compact_card_has_live_summary_and_expandable_log():
    card = render_compact_card(
        "Hide tool traces",
        "Searching files",
        3,
        ["Running tests", "Reading source", "Searching files"],
    )
    assert card.startswith("📋 Hide tool traces")
    assert "_3 actions_" in card
    assert "**> Running tests" in card
    assert card.endswith("Searching files||")


def test_compact_card_keeps_newest_log_lines_with_omission_count():
    card = render_compact_card(
        "A" * 100,
        "Newest",
        4,
        ["old one", "old two", "newest action"],
        max_log_chars=15,
    )
    assert "A" * 79 + "…" in card
    assert "earlier actions omitted" in card
    assert "newest action" in card
    assert "old one" not in card
