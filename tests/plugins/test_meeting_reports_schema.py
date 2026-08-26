"""Schema round-trip + validation for plugins/meeting_reports/models.py."""

from __future__ import annotations

import pytest

from plugins.meeting_reports.models import (
    ActionItem,
    MeetingReport,
    ProposedDelegation,
    ReviewState,
)


def _sample_report(**overrides) -> MeetingReport:
    kwargs = dict(
        report_id="mtgrpt-abc123",
        title="Weekly Sync",
        source={"kind": "teams", "meeting_id": "m-1"},
        participants=["Ada", "Grace"],
        summary="Shipped the migration doc.",
        decisions=["Ship on Friday"],
        action_items=[
            ActionItem(text="Write release notes", owner="Ada", due="Friday")
        ],
        proposed_delegations=[
            ProposedDelegation(
                goal="Draft release notes", target_agent="penny", rationale="owns docs"
            )
        ],
        confidence="high",
    )
    kwargs.update(overrides)
    return MeetingReport(**kwargs)


def test_round_trip_to_dict_from_dict():
    report = _sample_report()
    payload = report.to_dict()
    restored = MeetingReport.from_dict(payload)

    assert restored.report_id == report.report_id
    assert restored.title == report.title
    assert restored.summary == report.summary
    assert restored.decisions == report.decisions
    assert [item.to_dict() for item in restored.action_items] == [
        item.to_dict() for item in report.action_items
    ]
    assert [item.to_dict() for item in restored.proposed_delegations] == [
        item.to_dict() for item in report.proposed_delegations
    ]
    assert restored.review.status == "pending"


def test_canonical_json_includes_required_fields():
    payload = _sample_report().to_dict()
    for key in (
        "summary",
        "decisions",
        "action_items",
        "proposed_delegations",
        "ttl_seconds",
        "expires_at",
        "review",
    ):
        assert key in payload, key


def test_expires_at_derived_from_created_at_plus_ttl():
    report = _sample_report(ttl_seconds=3600)
    delta = report.expires_at - report.created_at
    assert delta.total_seconds() == 3600


def test_report_id_required():
    with pytest.raises(ValueError):
        _sample_report(report_id="")


@pytest.mark.parametrize("report_id", ["../escape", "has:colon", "x" * 37])
def test_report_id_must_be_callback_and_filename_safe(report_id):
    with pytest.raises(ValueError):
        _sample_report(report_id=report_id)


def test_title_required():
    with pytest.raises(ValueError):
        _sample_report(title=" ")


def test_report_url_round_trip_and_scheme_validation():
    report = _sample_report(report_url="https://example.test/reports/1")
    assert MeetingReport.from_dict(report.to_dict()).report_url == report.report_url
    with pytest.raises(ValueError):
        _sample_report(report_url="file:///tmp/report.html")


@pytest.mark.parametrize(
    "url",
    ["https://example.test/a b", "https://example.test/a\nheader"],
)
def test_report_url_rejects_whitespace(url):
    with pytest.raises(ValueError, match="whitespace"):
        _sample_report(report_url=url)


def test_unknown_schema_version_rejected():
    payload = _sample_report().to_dict()
    payload["schema_version"] = 99
    with pytest.raises(ValueError):
        MeetingReport.from_dict(payload)


def test_ttl_must_be_positive():
    with pytest.raises(ValueError):
        _sample_report(ttl_seconds=0)
    with pytest.raises(ValueError):
        _sample_report(ttl_seconds=-1)


def test_invalid_review_status_rejected():
    with pytest.raises(ValueError):
        ReviewState(status="maybe")


def test_owned_action_items_carry_owner_and_due():
    report = _sample_report()
    item = report.action_items[0]
    assert item.owner == "Ada"
    assert item.due == "Friday"


def test_proposed_delegations_are_not_dispatch_records():
    """The schema stores intent only - no dispatched/status/task_id fields."""
    payload = ProposedDelegation(goal="x").to_dict()
    assert "dispatched" not in payload
    assert "task_id" not in payload
    assert "status" not in payload


def test_proposed_delegation_toolsets_round_trip_without_dispatching():
    item = ProposedDelegation(goal="x", toolsets=["web", "terminal"])
    assert ProposedDelegation.from_dict(item.to_dict()).toolsets == ["web", "terminal"]
