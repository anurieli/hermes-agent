"""Generic persisted kanban ``notify_mode``: default vs silent.

Covers the real dispatch path end to end: a task's ``notify_mode`` is
persisted at creation (``create_task`` / ``decompose_triage_task``), then
the real ``_kanban_notifier_watcher`` tick is driven against a real SQLite
board so these tests prove actual delivery/suppression, not source text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import _is_notify_silent
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.decision_cards = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def send_kanban_decision_card(self, **kwargs):
        self.decision_cards.append(kwargs)


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


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Baseline: default (unchanged) behavior
# ---------------------------------------------------------------------------


def test_default_task_notifies_completion_with_summary(monkeypatch):
    """A task with no notify_mode (or notify_mode='default') behaves exactly
    as before: the completion text ping is delivered and carries the
    worker's summary."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ordinary task", assignee="worker")
        task = kb.get_task(conn, tid)
        assert task.notify_mode == "default"
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="Shipped the thing")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Shipped the thing" in adapter.sent[0]["text"]
    assert _unseen_terminal_events(tid) == []


# ---------------------------------------------------------------------------
# Silent tasks fail closed: no completion, no blocked card, no crash ping,
# no artifact, on the real notifier path.
# ---------------------------------------------------------------------------


def test_silent_task_suppresses_completed_event_and_artifacts(monkeypatch, tmp_path):
    artifact = tmp_path / "report.txt"
    artifact.write_text("deliverable")

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="silent processing", assignee="worker", notify_mode="silent",
        )
        task = kb.get_task(conn, tid)
        assert task.notify_mode == "silent"
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(
            conn, tid, summary="done",
            metadata={"artifacts": [str(artifact)]},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    artifact_calls = []

    async def _spy_artifacts(**kwargs):
        artifact_calls.append(kwargs)

    monkeypatch.setattr(runner, "_deliver_kanban_artifacts", _spy_artifacts)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert artifact_calls == []
    # Cursor must have advanced (claim_unseen_events_for_sub already moved
    # it before the silence check runs) so the event is never replayed.
    assert _unseen_terminal_events(tid) == []


def test_silent_task_suppresses_blocked_decision_card(monkeypatch):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="silent needs input", assignee="worker", notify_mode="silent",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="need a decision", kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.decision_cards == []
    assert _unseen_terminal_events(tid) == []


def test_silent_task_suppresses_crashed_event(monkeypatch):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="silent worker", assignee="worker", notify_mode="silent",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 98765)
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [tid]
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert _unseen_terminal_events(tid) == []


# ---------------------------------------------------------------------------
# Parent/child inheritance and explicit overrides
# ---------------------------------------------------------------------------


def test_child_inherits_silent_parent(monkeypatch):
    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn, title="silent parent", assignee="worker", notify_mode="silent",
        )
        child_id = kb.create_task(
            conn, title="inheriting child", assignee="worker",
            parents=(parent_id,), notify_mode="inherit",
        )
        child = kb.get_task(conn, child_id)
        assert child.notify_mode == "silent"
        kb.add_notify_sub(conn, task_id=child_id, platform="telegram", chat_id="chat-1")
        assert kb.complete_task(conn, parent_id, summary="parent done") is True
        assert kb.complete_task(conn, child_id, summary="child done") is True
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []


def test_child_can_override_inherit_to_default(monkeypatch):
    """A child of a silent parent can still opt back into normal delivery."""
    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn, title="silent parent", assignee="worker", notify_mode="silent",
        )
        child_id = kb.create_task(
            conn, title="opted-in child", assignee="worker",
            parents=(parent_id,), notify_mode="default",
        )
        child = kb.get_task(conn, child_id)
        assert child.notify_mode == "default"
        kb.add_notify_sub(conn, task_id=child_id, platform="telegram", chat_id="chat-1")
        assert kb.complete_task(conn, parent_id, summary="parent done") is True
        assert kb.complete_task(conn, child_id, summary="child done, opted in") is True
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "child done, opted in" in adapter.sent[0]["text"]


def test_decompose_children_inherit_root_notify_mode():
    """decompose_triage_task children default to the root's notify_mode;
    a per-child override wins."""
    conn = kb.connect()
    try:
        root_id = kb.create_task(
            conn, title="silent root", assignee="orchestrator",
            notify_mode="silent", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[
                {"title": "inherits silence"},
                {"title": "explicit default", "notify_mode": "default"},
            ],
            author="test",
        )
        inherited = kb.get_task(conn, child_ids[0])
        overridden = kb.get_task(conn, child_ids[1])
        assert inherited.notify_mode == "silent"
        assert overridden.notify_mode == "default"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_notify_mode_validation_rejects_invalid_value():
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="notify_mode"):
            kb.create_task(
                conn, title="bad mode", assignee="worker", notify_mode="loud",
            )
    finally:
        conn.close()


def test_notify_mode_inherit_requires_a_parent():
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="inherit"):
            kb.create_task(
                conn, title="no parent", assignee="worker", notify_mode="inherit",
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fail-closed on a corrupt/unrecognized stored value
# ---------------------------------------------------------------------------


def test_is_notify_silent_defaults_unchanged_for_legacy_none():
    """A task object with no readable notify_mode value (legacy row / None)
    keeps exactly the old behaviour: not silent."""

    class _LegacyTask:
        notify_mode = None

    assert _is_notify_silent(_LegacyTask()) is False
    assert _is_notify_silent(None) is False


def test_is_notify_silent_fails_closed_on_corrupt_value():
    class _CorruptTask:
        notify_mode = "not-a-real-mode"

    assert _is_notify_silent(_CorruptTask()) is True


def test_is_notify_silent_fails_closed_when_attribute_missing():
    class _NoAttrTask:
        __slots__ = ()

    assert _is_notify_silent(_NoAttrTask()) is True


def test_notifier_fails_closed_on_corrupt_stored_notify_mode(monkeypatch):
    """A task whose persisted notify_mode column holds a value outside the
    validated set (simulating DB corruption or a partial migration) must
    not leak a delivery through the real notifier tick."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="corrupt mode", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # Bypass validation to simulate a corrupt stored value directly.
        conn.execute(
            "UPDATE tasks SET notify_mode = 'garbled' WHERE id = ?", (tid,),
        )
        conn.commit()
        kb.complete_task(conn, tid, summary="should not be delivered")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
