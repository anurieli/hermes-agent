"""Regression tests for AAS-88 Part 1.

Symptom: when Ariel taps several Gate-2 piece-buttons (Approve / Deny /
Give-feedback) in quick succession, each tap's synthesized MessageEvent
started a fresh gateway turn via ``_handle_agent_button`` -> ``handle_message``.
Penny's gateway runs ``busy_input_mode: interrupt`` (the fleet default), so a
later tap interrupted the earlier tap's in-flight commit instead of queueing
behind it, and a commit could be aborted mid-write.

Fix: ``_handle_agent_button`` (plugins/platforms/telegram/adapter.py) now
synthesizes its MessageEvent with ``force_queue=True``. The gateway's busy
handler (``GatewayRunner._handle_active_session_busy_message`` in
gateway/run.py) demotes ``effective_mode`` to ``"queue"`` for any event
carrying that flag, before the interrupt/steer branches run, regardless of
the session's configured ``busy_input_mode``. This mirrors the existing
demotion pattern used for #30170 (active subagents) and #56391 (compression
in flight).

These tests pin down:
  * ``force_queue=True`` + ``busy_input_mode="interrupt"`` -> no interrupt()
    call, event lands in the FIFO queue, ack explains the queueing.
  * ``force_queue=True`` + ``busy_input_mode="steer"`` -> steer() is never
    called either; the event still queues.
  * ``force_queue=False`` (a normal free-text follow-up) is unaffected: the
    configured interrupt mode still interrupts. Free-text barge-in must not
    be degraded by this fix.
  * End-to-end via ``_handle_agent_button``: two rapid taps against a busy
    session both resolve (both land as distinct queued events; neither tap
    is dropped or silently overwrites the other).
"""

from __future__ import annotations

import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so gateway/telegram adapter modules import cleanly (mirrors
# tests/gateway/test_subagent_protection_30170.py and
# tests/gateway/test_telegram_agent_buttons.py).
# ---------------------------------------------------------------------------
_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_ct.CHANNEL = "channel"
_tg.constants.ChatType = _ct
_tg.constants.ParseMode = MagicMock()
_tg.error = MagicMock()
_tg.error.NetworkError = type("NetworkError", (OSError,), {})
_tg.error.TimedOut = type("TimedOut", (OSError,), {})
_tg.error.BadRequest = type("BadRequest", (Exception,), {})
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))
sys.modules.setdefault("telegram.error", _tg.error)

from gateway.platforms.base import (  # noqa: E402
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL  # noqa: E402


def _make_source() -> SessionSource:
    return SessionSource(
        platform=MagicMock(value="telegram"),
        chat_id="c1",
        chat_type="private",
        user_id="user1",
    )


def _make_event(text: str = "approve", *, force_queue: bool = False) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        force_queue=force_queue,
    )


def _make_runner(*, busy_input_mode: str) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    runner._busy_input_mode = busy_input_mode
    runner._agent_has_active_subagents = lambda _agent: False
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    return runner


def _make_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value="telegram")
    return adapter


def _make_busy_agent() -> MagicMock:
    agent = MagicMock()
    agent.get_activity_summary.return_value = {
        "api_call_count": 2,
        "max_iterations": 60,
        "current_tool": "write_file",
    }
    return agent


class TestForceQueueDemotesInterrupt:
    @pytest.mark.asyncio
    async def test_does_not_interrupt_when_force_queue_set(self) -> None:
        runner = _make_runner(busy_input_mode="interrupt")
        adapter = _make_adapter()
        event = _make_event("approved", force_queue=True)
        sk = build_session_key(event.source)
        agent = _make_busy_agent()
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        agent.interrupt.assert_not_called()
        # FIFO path, not the destructive merge helper, each tap gets its
        # own turn (mirrors #30170 / #43066 sub-bug 2 FIFO rationale).
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_ack_explains_the_queueing(self) -> None:
        runner = _make_runner(busy_input_mode="interrupt")
        adapter = _make_adapter()
        event = _make_event("approved", force_queue=True)
        sk = build_session_key(event.source)
        agent = _make_busy_agent()
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 30
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        adapter._send_with_retry.assert_called_once()
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "queued" in content.lower()
        assert "Interrupting" not in content

    @pytest.mark.asyncio
    async def test_does_not_steer_when_force_queue_set(self) -> None:
        """Steer mode must also be overridden: a button tap must run as its
        own distinct turn after the current one, not get spliced mid-run."""
        runner = _make_runner(busy_input_mode="steer")
        adapter = _make_adapter()
        event = _make_event("denied", force_queue=True)
        sk = build_session_key(event.source)
        agent = _make_busy_agent()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        agent.steer.assert_not_called()
        agent.interrupt.assert_not_called()
        assert adapter._pending_messages.get(sk) is event

    @pytest.mark.asyncio
    async def test_free_text_barge_in_unaffected(self) -> None:
        """Sanity control: without force_queue, configured interrupt mode
        is unchanged, this fix must not degrade normal chat barge-in."""
        runner = _make_runner(busy_input_mode="interrupt")
        adapter = _make_adapter()
        event = _make_event("actually wait", force_queue=False)
        sk = build_session_key(event.source)
        agent = _make_busy_agent()
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        agent.interrupt.assert_called_once_with("actually wait")

    @pytest.mark.asyncio
    async def test_queue_mode_unchanged_with_force_queue(self) -> None:
        """Already-configured queue mode combined with force_queue still
        queues (never interrupts), the guard is a no-op on top of an
        already-safe configured mode."""
        runner = _make_runner(busy_input_mode="queue")
        adapter = _make_adapter()
        event = _make_event("approved", force_queue=True)
        sk = build_session_key(event.source)
        agent = _make_busy_agent()
        runner._running_agents[sk] = agent
        runner.adapters[event.source.platform] = adapter

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        agent.interrupt.assert_not_called()
        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "queued" in content.lower()


class TestRapidButtonTapsBothResolve:
    """End-to-end via _handle_agent_button: two taps fired while the
    session is busy must both queue as distinct events, proving neither
    tap's commit is interrupted or dropped."""

    @pytest.mark.asyncio
    async def test_two_rapid_taps_both_land_as_queued_events(self) -> None:
        from plugins.platforms.telegram.adapter import TelegramAdapter
        from gateway.config import PlatformConfig

        config = PlatformConfig(enabled=True, token="test-token", extra={})
        adapter = TelegramAdapter(config)
        adapter._bot = AsyncMock()
        adapter._app = MagicMock()

        captured_events = []

        async def _fake_handle_message(event):
            # Simulate the gateway busy-queue path: while busy, the event
            # is captured (queued) rather than starting a fresh turn.
            captured_events.append(event)

        adapter.handle_message = _fake_handle_message

        sid1 = adapter._register_agent_buttons(
            {"multi": False, "options": [("Approve", "approve recap"), ("Deny", "deny recap")]}
        )
        sid2 = adapter._register_agent_buttons(
            {"multi": False, "options": [("Approve", "approve todos"), ("Deny", "deny todos")]}
        )

        def _make_query(sid, idx):
            query = AsyncMock()
            query.data = f"ab:{sid}:{idx}"
            query.answer = AsyncMock()
            query.edit_message_reply_markup = AsyncMock()
            query.delete_message = AsyncMock()
            query.from_user = MagicMock()
            query.from_user.id = 42
            query.from_user.full_name = "Ariel Nurieli"
            query.message = MagicMock()
            query.message.chat_id = 123
            query.message.chat = MagicMock()
            query.message.chat.type = "private"
            query.message.chat.title = None
            query.message.message_thread_id = None
            return query

        import os

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_agent_button(
                _make_query(sid1, 0),
                f"ab:{sid1}:0",
                chat_id=123,
                chat_type="private",
                thread_id=None,
                user_name="Ariel Nurieli",
            )
            # Second tap fired immediately after, must still resolve, not
            # be dropped or silently merged into the first.
            await adapter._handle_agent_button(
                _make_query(sid2, 0),
                f"ab:{sid2}:0",
                chat_id=123,
                chat_type="private",
                thread_id=None,
                user_name="Ariel Nurieli",
            )

        assert len(captured_events) == 2
        assert captured_events[0].text == "approve recap"
        assert captured_events[1].text == "approve todos"
        assert all(e.force_queue is True for e in captured_events)
