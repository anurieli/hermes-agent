"""Portable meeting-processing report plugin for Hermes Agent."""

from __future__ import annotations

from typing import Any

from .cards import (
    CALLBACK_PREFIX,
    SLACK_ACTION_PATTERN,
    SLACK_OPEN_ACTION_ID,
    SLACK_VIEW_CALLBACK_ID,
)
from .cli import meeting_report_command as handle_cli
from .cli import register_cli as setup_cli
from .handlers import (
    consume_telegram_note_reply,
    handle_slack_action,
    handle_slack_open,
    handle_slack_view_submission,
    handle_telegram_callback,
)
from .models import ActionItem, MeetingReport, ProposedDelegation, ReviewState
from .pipeline import (
    cleanup_expired_reports,
    deliver_completion,
    generate_report,
    review_report,
    route_pipeline_event,
)
from .silent_events import PipelineEvent, PipelineEventLog, run_silent_fanout
from .store import MeetingReportStore


def register(ctx: Any) -> None:
    """Register CLI and native Telegram/Slack interactive handlers."""
    ctx.register_cli_command(
        name="meeting-report",
        help="Inspect, review, render, and clean up portable meeting reports",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description=(
            "Shows canonical JSON and 24-hour HTML meeting reports, applies "
            "review decisions, and cleans "
            "up expired artifacts."
        ),
    )
    ctx.register_slack_action_handler(SLACK_ACTION_PATTERN, handle_slack_action)
    ctx.register_slack_action_handler(SLACK_OPEN_ACTION_ID, handle_slack_open)
    ctx.register_slack_view_handler(
        SLACK_VIEW_CALLBACK_ID, handle_slack_view_submission
    )
    ctx.register_telegram_callback_handler(
        f"{CALLBACK_PREFIX}:", handle_telegram_callback
    )
    ctx.register_telegram_text_interceptor(consume_telegram_note_reply)


__all__ = [
    "ActionItem",
    "MeetingReport",
    "MeetingReportStore",
    "PipelineEvent",
    "PipelineEventLog",
    "ProposedDelegation",
    "ReviewState",
    "cleanup_expired_reports",
    "deliver_completion",
    "generate_report",
    "register",
    "review_report",
    "run_silent_fanout",
    "route_pipeline_event",
]
