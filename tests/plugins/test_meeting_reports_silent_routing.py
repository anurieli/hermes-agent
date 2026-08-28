"""Internal kanban/subagent events stay silent; only report-ready is visible."""

from __future__ import annotations

import asyncio

import pytest

from plugins.meeting_reports.models import MeetingReport
from plugins.meeting_reports.pipeline import route_pipeline_event

from plugins.meeting_reports.silent_events import (
    PipelineEventLog,
    REPORT_READY_KIND,
    PipelineEvent,
    is_kanban_silent,
    run_silent_fanout,
)


def test_stage_events_default_silent():
    log = PipelineEventLog()
    log.stage("resolving_meeting")
    log.stage("fetching_transcript")
    log.stage("summarizing")

    assert all(event.silent for event in log.events)
    assert log.visible_events == []
    assert len(log.silent_events) == 3


def test_report_ready_is_the_only_visible_event():
    log = PipelineEventLog()
    log.stage("resolving_meeting")
    log.stage("fetching_transcript")
    log.stage("summarizing")
    log.report_ready("mtgrpt-xyz")

    assert len(log.visible_events) == 1
    assert log.visible_events[0].kind == REPORT_READY_KIND
    assert log.visible_events[0].payload == {"report_id": "mtgrpt-xyz"}


def test_report_ready_event_is_idempotent_per_pipeline_run():
    log = PipelineEventLog()
    first = log.report_ready("mtgrpt-xyz")
    second = log.report_ready("mtgrpt-xyz")
    assert second is first
    assert len(log.visible_events) == 1
    with pytest.raises(ValueError, match="cannot publish two reports"):
        log.report_ready("mtgrpt-other")


def test_router_drops_internal_events_and_delivers_one_ready_card(tmp_path):
    from plugins.meeting_reports.store import MeetingReportStore

    class Adapter:
        def __init__(self):
            self.calls = []

        async def send_meeting_report_card(self, **kwargs):
            self.calls.append(kwargs)
            return "message-1"

    store = MeetingReportStore(tmp_path / "meeting_reports")
    store.save(MeetingReport(report_id="mtgrpt-route-1", title="Sync", summary="Done"))
    adapter = Adapter()
    internal = PipelineEvent(kind="meeting:summarizing", silent=True)
    visible_but_unknown = PipelineEvent(kind="meeting:custom_alert", silent=False)
    ready = PipelineEvent(
        kind=REPORT_READY_KIND,
        payload={"report_id": "mtgrpt-route-1"},
        silent=False,
    )

    assert (
        asyncio.run(
            route_pipeline_event(internal, adapter=adapter, chat_id="C1", store=store)
        )
        is None
    )
    assert (
        asyncio.run(
            route_pipeline_event(
                visible_but_unknown, adapter=adapter, chat_id="C1", store=store
            )
        )
        is None
    )
    assert (
        asyncio.run(
            route_pipeline_event(ready, adapter=adapter, chat_id="C1", store=store)
        )
        == "message-1"
    )
    assert len(adapter.calls) == 1


def test_processing_jobs_fan_out_with_only_silent_lifecycle_events():
    async def worker(value):
        await asyncio.sleep(0)
        return value

    events = PipelineEventLog()
    result = asyncio.run(
        run_silent_fanout(
            {"summary": worker("done"), "actions": worker(["one"])},
            events=events,
        )
    )
    assert result == {"summary": "done", "actions": ["one"]}
    assert events.visible_events == []
    assert all(event.kind.startswith("meeting:") for event in events.events)


def test_processing_fanout_failure_stays_silent_and_propagates():
    async def fail():
        raise RuntimeError("worker failed")

    events = PipelineEventLog()
    with pytest.raises(RuntimeError, match="worker failed"):
        asyncio.run(run_silent_fanout({"summary": fail()}, events=events))
    assert events.visible_events == []
    assert events.events[-1].kind == "meeting:fanout_failed"


def test_report_ready_kind_cannot_be_silenced():
    """Construction-time guard: even an explicit silent=True is overridden."""
    event = PipelineEvent(kind=REPORT_READY_KIND, silent=True)
    assert event.silent is False


def test_explicit_non_silent_event_is_allowed_for_other_kinds():
    log = PipelineEventLog()
    event = log.record(
        "meeting:custom_alert", {"reason": "manual override"}, silent=False
    )
    assert event.silent is False
    assert log.visible_events == [event]


def test_internal_pipeline_kinds_are_disjoint_from_kanban_terminal_kinds():
    """Every kind we emit is a "meeting:" kind - never one of kanban's own
    TERMINAL_KINDS - so the existing notifier's filtering keeps it silent
    even if a meeting run is itself dispatched as a kanban task."""
    from gateway.kanban_watchers import TERMINAL_KINDS

    assert is_kanban_silent("meeting:resolving_meeting") is True
    assert is_kanban_silent(REPORT_READY_KIND) is True
    for kind in ("meeting:resolving_meeting", "meeting:summarizing", REPORT_READY_KIND):
        assert kind not in TERMINAL_KINDS


def test_kanban_terminal_kinds_are_not_silent():
    from gateway.kanban_watchers import TERMINAL_KINDS

    for kind in TERMINAL_KINDS:
        assert is_kanban_silent(kind) is False


def test_is_kanban_silent_fails_safe_when_gateway_module_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "gateway.kanban_watchers":
            raise ImportError("simulated unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    assert is_kanban_silent("meeting:anything") is True
