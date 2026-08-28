"""Idempotent review-state transitions for a :class:`MeetingReport`.

This module NEVER calls ``delegate_task`` or any other dispatch surface.
Approving a report's proposed delegations only flips ``review.status`` - a
separate, explicit step (owned by whatever system consumes accepted
reports) is required before any of ``proposed_delegations`` actually runs.
That separation is the whole point: a report a human has not looked at yet
can never cause work to start.

Idempotency: once a report reaches a terminal review status (anything but
``pending``), every subsequent :func:`apply_review_action` call is a no-op -
it returns the stored report unchanged and reports ``changed=False``. A
double tap on "Accept" (a slow network retry, a duplicate webhook, a second
person clicking the same card) must never re-fire whatever the caller does
in response to a state *change*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from plugins.meeting_reports.models import MeetingReport, ReviewState

ACTION_TO_STATUS = {
    "accept": "accepted",
    "accept_with_notes": "accepted_with_notes",
    "reject": "rejected",
    "reject_with_notes": "rejected_with_notes",
}
REVIEW_ACTIONS = tuple(ACTION_TO_STATUS)
NOTES_ACTIONS = frozenset({"accept_with_notes", "reject_with_notes"})


class UnknownReviewActionError(ValueError):
    pass


class ReviewNotesRequiredError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewOutcome:
    report: MeetingReport
    changed: bool
    previous_status: str
    applied_action: Optional[str]


def apply_review_action(
    report: MeetingReport,
    action: str,
    *,
    actor: Optional[str] = None,
    notes: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ReviewOutcome:
    """Return a new report with the review transition applied.

    Pure: does not touch disk or dispatch anything. Callers persist the
    returned report themselves (see ``pipeline.review_report`` for the
    disk-backed convenience wrapper).
    """
    if action not in ACTION_TO_STATUS:
        raise UnknownReviewActionError(
            f"Unknown review action {action!r}; expected one of {REVIEW_ACTIONS}"
        )
    previous_status = report.review.status
    if report.review.terminal:
        # Already decided: idempotent no-op regardless of which action
        # (even the same one) arrives next.
        return ReviewOutcome(
            report=report,
            changed=False,
            previous_status=previous_status,
            applied_action=None,
        )

    if action in NOTES_ACTIONS and not (notes or "").strip():
        raise ReviewNotesRequiredError(f"Action {action!r} requires non-empty notes.")

    new_status = ACTION_TO_STATUS[action]
    updated = MeetingReport.from_dict(report.to_dict())
    updated.review = ReviewState(
        status=new_status,
        actor=actor,
        notes=notes,
        reviewed_at=now or datetime.now(timezone.utc),
    )
    return ReviewOutcome(
        report=updated,
        changed=True,
        previous_status=previous_status,
        applied_action=action,
    )


__all__ = [
    "ACTION_TO_STATUS",
    "REVIEW_ACTIONS",
    "NOTES_ACTIONS",
    "UnknownReviewActionError",
    "ReviewNotesRequiredError",
    "ReviewOutcome",
    "apply_review_action",
]
