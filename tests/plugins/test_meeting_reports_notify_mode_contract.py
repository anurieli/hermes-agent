"""Product contract: the underlying kanban work for a meeting-processing run
stays silent on the real gateway notifier, while the plugin's own
``meeting:report_ready`` completion card still reaches chat.

This exercises the REAL notifier and REAL kanban tasks (not just the
plugin's local ``PipelineEventLog`` bookkeeping) to prove the two systems
compose correctly: ``notify_mode="silent"`` suppresses kanban lifecycle
noise, and ``route_pipeline_event`` still delivers exactly one card.
"""

from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from plugins.meeting_reports.models import MeetingReport
from plugins.meeting_reports.pipeline import route_pipeline_event
from plugins.meeting_reports.silent_events import PipelineEvent, REPORT_READY_KIND
from plugins.meeting_reports.store import MeetingReportStore


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.card_calls = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def send_meeting_report_card(self, **kwargs):
        self.card_calls.append(kwargs)
        return "message-1"


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def test_silent_meeting_kanban_work_stays_off_chat_while_report_ready_card_sends(
    monkeypatch, tmp_path,
):
    adapter = RecordingAdapter()

    # 1. The source pipeline's underlying processing work is real kanban
    #    tasks (a parent "Process <source> meeting" task with a child worker),
    #    created silent per the plugin's own migration guidance.
    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn, title="Process Granola meeting", assignee="penny-worker",
            notify_mode="silent",
        )
        child_id = kb.create_task(
            conn, title="Summarize transcript", assignee="penny-worker",
            parents=(parent_id,), notify_mode="inherit",
        )
        kb.add_notify_sub(conn, task_id=parent_id, platform="telegram", chat_id="chat-1")
        kb.add_notify_sub(conn, task_id=child_id, platform="telegram", chat_id="chat-1")
        assert kb.complete_task(conn, parent_id, summary="Meeting processed") is True
        assert kb.complete_task(conn, child_id, summary="Transcript summarized") is True
    finally:
        conn.close()

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # No start ack, no ids, no parent/child completion ping reaches chat.
    assert adapter.sent == []

    # 2. The plugin's own report-ready event is the one thing that reaches
    #    the originating chat: exactly one compact completion card.
    store = MeetingReportStore(tmp_path / "meeting_reports")
    store.save(
        MeetingReport(
            report_id="mtgrpt-contract-1",
            title="Weekly Sync",
            summary="Processed via Granola",
            filing_verdict="filed",
            filed_destinations=["Notion: Meeting Notes / Weekly Sync"],
        )
    )
    ready = PipelineEvent(
        kind=REPORT_READY_KIND,
        payload={"report_id": "mtgrpt-contract-1"},
        silent=False,
    )
    result = asyncio.run(
        route_pipeline_event(ready, adapter=adapter, chat_id="chat-1", store=store)
    )

    assert result == "message-1"
    assert len(adapter.card_calls) == 1
    assert adapter.card_calls[0]["report_id"] == "mtgrpt-contract-1"
    # Still no plain-text sends. Only the native card path was used.
    assert adapter.sent == []
