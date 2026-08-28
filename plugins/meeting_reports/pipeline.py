"""Portable meeting-report generation, delivery, review, and event routing."""

from __future__ import annotations

import secrets
from typing import Any, Mapping, Optional, Sequence

from .cards import CompletionCard, build_completion_card
from .models import (
    DEFAULT_TTL_SECONDS,
    ActionItem,
    MeetingReport,
    ProposedDelegation,
    coerce_action_items,
    coerce_delegations,
)
from .review import ReviewOutcome, apply_review_action
from .silent_events import PipelineEvent, PipelineEventLog, REPORT_READY_KIND
from .store import MeetingReportStore, get_default_store


def new_report_id() -> str:
    return f"mtgrpt-{secrets.token_hex(6)}"


def generate_report(
    *,
    title: str,
    summary: str,
    source: Optional[Mapping[str, Any]] = None,
    participants: Optional[Sequence[str]] = None,
    decisions: Optional[Sequence[str]] = None,
    action_items: Optional[Sequence[Any]] = None,
    proposed_delegations: Optional[Sequence[Any]] = None,
    confidence: Optional[str] = None,
    confidence_notes: Optional[str] = None,
    filing_verdict: Optional[str] = None,
    filed_destinations: Optional[Sequence[str]] = None,
    report_url: Optional[str] = None,
    report_id: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    store: Optional[MeetingReportStore] = None,
    events: Optional[PipelineEventLog] = None,
) -> MeetingReport:
    """Persist canonical JSON and HTML, then emit one report-ready event."""
    store = store or get_default_store()
    events = events or PipelineEventLog()
    report = MeetingReport(
        report_id=report_id or new_report_id(),
        title=title,
        summary=summary,
        source=dict(source or {}),
        participants=list(participants or []),
        decisions=list(decisions or []),
        action_items=coerce_action_items(action_items),
        proposed_delegations=coerce_delegations(proposed_delegations),
        confidence=confidence,
        confidence_notes=confidence_notes,
        filing_verdict=filing_verdict,
        filed_destinations=list(filed_destinations or []),
        report_url=report_url,
        ttl_seconds=ttl_seconds,
    )
    saved = store.save(report)
    events.report_ready(saved.report_id)
    return saved


async def deliver_completion(
    adapter: Any, chat_id: str, report: MeetingReport, **kwargs: Any
) -> Any:
    """Deliver exactly the compact completion card through a native adapter."""
    send = getattr(adapter, "send_meeting_report_card", None)
    card = build_completion_card(report)
    if callable(send):
        payload = {
            "chat_id": chat_id,
            "report_id": report.report_id,
            "title": card.title,
            "body": card.body,
            "buttons": card.buttons,
            **kwargs,
        }
        if card.report_url:
            payload["report_url"] = card.report_url
        return await send(**payload)
    plain_send = getattr(adapter, "send", None)
    if not callable(plain_send):
        raise TypeError("adapter implements neither send_meeting_report_card nor send")
    metadata = kwargs.get("metadata")
    text = f"{card.title}\n\n{card.body}"
    if card.report_url:
        text += f"\n\nOpen report: {card.report_url}"
    return await plain_send(chat_id, text, metadata=metadata)


# Compatibility name used by early adopters of the portable kit.
deliver_completion_card = deliver_completion


async def route_pipeline_event(
    event: PipelineEvent,
    *,
    adapter: Any,
    chat_id: str,
    store: Optional[MeetingReportStore] = None,
    **kwargs: Any,
) -> Any:
    """Route only report-ready events; silent orchestration never reaches chat."""
    if event.silent or event.kind != REPORT_READY_KIND:
        return None
    report_id = str(event.payload.get("report_id") or "")
    store = store or get_default_store()
    try:
        report = store.load(report_id)
        available = store.is_available(report_id)
    except ValueError:
        return None
    if report is None or not available:
        return None
    return await deliver_completion(adapter, chat_id, report, **kwargs)


def review_report(
    report_id: str,
    action: str,
    *,
    notes: Optional[str] = None,
    actor: Optional[str] = None,
    store: Optional[MeetingReportStore] = None,
) -> Optional[ReviewOutcome]:
    """Apply and persist an idempotent review transition without dispatching."""
    store = store or get_default_store()
    with store.review_lock():
        report = store.load(report_id)
        if report is None or not store.is_available(report_id):
            return None
        outcome = apply_review_action(report, action, notes=notes, actor=actor)
        if outcome.changed:
            store.save(outcome.report)
        return outcome


def cleanup_expired_reports(*, store: Optional[MeetingReportStore] = None) -> list[str]:
    return (store or get_default_store()).cleanup_expired()


def to_card_data(report: MeetingReport) -> dict[str, Any]:
    card = build_completion_card(report)
    return {
        "report_id": card.report_id,
        "title": card.title,
        "body": card.body,
        "report_url": card.report_url,
        "filing_verdict": report.filing_verdict,
        "filed_destinations": list(report.filed_destinations),
        "buttons": [
            {"action": action, "label": label} for action, label in card.buttons
        ],
    }


__all__ = [
    "ActionItem",
    "CompletionCard",
    "MeetingReport",
    "ProposedDelegation",
    "cleanup_expired_reports",
    "deliver_completion",
    "deliver_completion_card",
    "generate_report",
    "new_report_id",
    "review_report",
    "route_pipeline_event",
    "to_card_data",
]
