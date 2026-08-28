"""TTL cleanup for plugins/meeting_reports/store.py.

Runs against the repo's autouse per-test HERMES_HOME sandbox (see
tests/conftest.py::_hermetic_environment) - never the real ~/.hermes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plugins.meeting_reports.models import MeetingReport
from plugins.meeting_reports.store import MeetingReportStore, default_reports_dir


def _make_store(tmp_path) -> MeetingReportStore:
    return MeetingReportStore(tmp_path / "meeting_reports")


def _report(
    report_id="mtgrpt-ttl-1", ttl_seconds=86400, created_at=None
) -> MeetingReport:
    return MeetingReport(
        report_id=report_id,
        title="TTL test",
        summary="s",
        ttl_seconds=ttl_seconds,
        created_at=created_at or datetime.now(timezone.utc),
    )


def test_default_reports_dir_is_profile_scoped(monkeypatch, tmp_path):
    fake_home = tmp_path / "custom_home"
    fake_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    assert default_reports_dir() == fake_home / "meeting_reports"


def test_save_writes_json_and_html(tmp_path):
    store = _make_store(tmp_path)
    report = store.save(_report())

    assert store._json_path(report.report_id).exists()
    assert store._html_path(report.report_id).exists()
    assert report.report_html_path == str(store._html_path(report.report_id))


def test_invalid_source_metadata_does_not_leave_partial_artifacts(tmp_path):
    store = _make_store(tmp_path)
    report = _report(report_id="mtgrpt-invalid-source")
    report.source = {"not_json": object()}

    with pytest.raises(TypeError):
        store.save(report)

    assert not store._json_path(report.report_id).exists()
    assert not store._html_path(report.report_id).exists()


def test_load_round_trips_the_saved_report(tmp_path):
    store = _make_store(tmp_path)
    saved = store.save(_report(report_id="mtgrpt-ttl-2"))
    loaded = store.load("mtgrpt-ttl-2")

    assert loaded is not None
    assert loaded.report_id == saved.report_id
    assert loaded.summary == saved.summary


def test_load_and_list_ignore_schema_corruption(tmp_path):
    store = _make_store(tmp_path)
    store.root.mkdir(parents=True)
    bad_path = store.root / "mtgrpt-bad.json"
    bad_path.write_text(
        json.dumps({"report_id": "mtgrpt-bad", "title": "Bad", "action_items": [1]}),
        encoding="utf-8",
    )

    assert store.load("mtgrpt-bad") is None
    assert store.list_reports() == []


def test_is_available_true_before_expiry(tmp_path):
    store = _make_store(tmp_path)
    store.save(_report(report_id="mtgrpt-ttl-3", ttl_seconds=86400))
    assert store.is_available("mtgrpt-ttl-3") is True


def test_is_available_false_after_expiry_window(tmp_path):
    store = _make_store(tmp_path)
    old_created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    store.save(
        _report(report_id="mtgrpt-ttl-4", ttl_seconds=86400, created_at=old_created_at)
    )

    assert store.is_available("mtgrpt-ttl-4") is False


def test_cleanup_expired_deletes_json_and_html_and_leaves_fresh_reports(tmp_path):
    store = _make_store(tmp_path)
    old_created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    store.save(
        _report(
            report_id="mtgrpt-expired", ttl_seconds=86400, created_at=old_created_at
        )
    )
    store.save(_report(report_id="mtgrpt-fresh", ttl_seconds=86400))

    deleted = store.cleanup_expired()

    assert deleted == ["mtgrpt-expired"]
    assert not store._json_path("mtgrpt-expired").exists()
    assert not store._html_path("mtgrpt-expired").exists()
    assert store.load("mtgrpt-expired") is None
    # Fresh report survives untouched.
    assert store.load("mtgrpt-fresh") is not None
    assert store._html_path("mtgrpt-fresh").exists()


def test_cleanup_expired_is_idempotent(tmp_path):
    store = _make_store(tmp_path)
    old_created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    store.save(
        _report(
            report_id="mtgrpt-expired-2", ttl_seconds=86400, created_at=old_created_at
        )
    )

    first = store.cleanup_expired()
    second = store.cleanup_expired()

    assert first == ["mtgrpt-expired-2"]
    assert second == []


def test_rendered_artifacts_unavailable_after_cleanup(tmp_path):
    store = _make_store(tmp_path)
    old_created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    report = store.save(
        _report(report_id="mtgrpt-gone", ttl_seconds=86400, created_at=old_created_at)
    )
    html_path = report.report_html_path

    store.cleanup_expired()

    import os

    assert not os.path.exists(html_path)
    assert store.is_available("mtgrpt-gone") is False
