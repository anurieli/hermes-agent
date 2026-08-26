"""Idempotent, dispatch-free review transitions (plugins/meeting_reports/review.py)."""

from __future__ import annotations

import threading

import pytest

from plugins.meeting_reports.models import MeetingReport, ProposedDelegation
from plugins.meeting_reports.pipeline import review_report
from plugins.meeting_reports.review import (
    ReviewNotesRequiredError,
    UnknownReviewActionError,
    apply_review_action,
)
from plugins.meeting_reports.store import MeetingReportStore


def _report(**overrides) -> MeetingReport:
    kwargs = dict(
        report_id="mtgrpt-review-1",
        title="Review test",
        summary="s",
        proposed_delegations=[
            ProposedDelegation(goal="Do the thing", target_agent="penny")
        ],
    )
    kwargs.update(overrides)
    return MeetingReport(**kwargs)


def test_accept_flips_status_and_records_actor():
    outcome = apply_review_action(_report(), "accept", actor="ariel")
    assert outcome.changed is True
    assert outcome.previous_status == "pending"
    assert outcome.report.review.status == "accepted"
    assert outcome.report.review.actor == "ariel"


def test_reject_flips_status():
    outcome = apply_review_action(_report(), "reject", actor="ariel")
    assert outcome.report.review.status == "rejected"


def test_accept_with_notes_requires_notes():
    with pytest.raises(ReviewNotesRequiredError):
        apply_review_action(_report(), "accept_with_notes", notes="")


def test_accept_with_notes_stores_notes():
    outcome = apply_review_action(
        _report(), "accept_with_notes", notes="looks good, ship it"
    )
    assert outcome.report.review.status == "accepted_with_notes"
    assert outcome.report.review.notes == "looks good, ship it"


def test_unknown_action_raises():
    with pytest.raises(UnknownReviewActionError):
        apply_review_action(_report(), "approve_forever")


def test_second_call_after_terminal_status_is_noop():
    first = apply_review_action(_report(), "accept", actor="ariel")
    second = apply_review_action(first.report, "accept", actor="ariel")

    assert second.changed is False
    assert second.report.review.status == "accepted"
    assert second.applied_action is None


def test_terminal_review_is_noop_before_note_validation():
    first = apply_review_action(_report(), "accept", actor="ariel")
    duplicate = apply_review_action(first.report, "reject_with_notes", actor="ariel")
    assert duplicate.changed is False
    assert duplicate.report.review.status == "accepted"


def test_terminal_status_ignores_a_different_action_too():
    """Idempotency means "already decided", not "matches the same action"."""
    first = apply_review_action(_report(), "accept", actor="ariel")
    second = apply_review_action(first.report, "reject", actor="someone_else")

    assert second.changed is False
    assert second.report.review.status == "accepted"


def test_review_action_never_touches_proposed_delegations():
    outcome = apply_review_action(_report(), "accept", actor="ariel")
    assert [d.to_dict() for d in outcome.report.proposed_delegations] == [
        d.to_dict() for d in _report().proposed_delegations
    ]


def test_review_module_never_imports_delegate_tool():
    import plugins.meeting_reports.review as review_module

    assert "delegate_tool" not in review_module.__dict__
    assert "delegate_task" not in dir(review_module)


# --- pipeline.review_report (disk-backed, idempotent wrapper) --------------


def test_pipeline_review_report_persists_and_is_idempotent(tmp_path):
    store = MeetingReportStore(tmp_path / "meeting_reports")
    store.save(_report(report_id="mtgrpt-review-2"))

    first = review_report("mtgrpt-review-2", "accept", actor="ariel", store=store)
    second = review_report("mtgrpt-review-2", "accept", actor="ariel", store=store)

    assert first.changed is True
    assert second.changed is False
    reloaded = store.load("mtgrpt-review-2")
    assert reloaded.review.status == "accepted"


def test_pipeline_review_report_unknown_id_returns_none(tmp_path):
    store = MeetingReportStore(tmp_path / "meeting_reports")
    assert review_report("does-not-exist", "accept", store=store) is None


def test_concurrent_conflicting_reviews_only_apply_once(tmp_path):
    store = MeetingReportStore(tmp_path / "meeting_reports")
    store.save(_report(report_id="mtgrpt-review-race"))
    barrier = threading.Barrier(2)
    outcomes = []

    def run(action):
        barrier.wait()
        outcomes.append(
            review_report("mtgrpt-review-race", action, actor=action, store=store)
        )

    threads = [
        threading.Thread(target=run, args=("accept",)),
        threading.Thread(target=run, args=("reject",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert sum(outcome.changed for outcome in outcomes) == 1
    assert store.load("mtgrpt-review-race").review.status in {"accepted", "rejected"}
