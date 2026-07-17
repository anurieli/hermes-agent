from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _blocked_task(tmp_path, monkeypatch, *, choices=None):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Choose the rollout mode",
            assignee="penny",
        )
        assert kb.block_task(
            conn,
            task_id,
            reason="Which rollout mode should I use?",
            kind="needs_input",
            choices=choices,
        )
        return task_id
    finally:
        conn.close()


def _query(data: str, *, message_id: int = 42, user_id: str = "777"):
    message = SimpleNamespace(
        chat_id=12345,
        message_id=message_id,
        message_thread_id=None,
        chat=SimpleNamespace(type="private"),
        text="decision card",
    )
    return SimpleNamespace(
        data=data,
        message=message,
        from_user=SimpleNamespace(id=user_id, first_name="Ariel"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def test_block_event_preserves_sanitized_structured_choices(tmp_path, monkeypatch):
    task_id = _blocked_task(
        tmp_path,
        monkeypatch,
        choices=["Canary", "Full rollout", "  ", "Canary"],
    )
    conn = kb.connect()
    try:
        events = kb.list_events(conn, task_id)
    finally:
        conn.close()

    blocked = next(event for event in events if event.kind == "blocked")
    assert blocked.payload["choices"] == ["Canary", "Full rollout"]


def test_answer_blocked_task_is_atomic_and_idempotent(tmp_path, monkeypatch):
    task_id = _blocked_task(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        assert kb.answer_blocked_task(
            conn,
            task_id,
            author="telegram:777",
            answer="Use the canary rollout",
        ) == "ready"
        assert kb.answer_blocked_task(
            conn,
            task_id,
            author="telegram:777",
            answer="duplicate",
        ) is None
        comments = kb.list_comments(conn, task_id)
        assert [comment.body for comment in comments] == ["Use the canary rollout"]
        assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_card_renders_human_context_choices_and_generic_actions(tmp_path, monkeypatch):
    task_id = _blocked_task(tmp_path, monkeypatch, choices=["Canary", "Full rollout"])
    adapter = _adapter()
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=42)

    result = await adapter.send_kanban_decision_card(
        chat_id="12345",
        task_id=task_id,
        board=None,
        assignee="penny",
        title="Choose the rollout mode",
        reason="Which rollout mode should I use?",
        choices=["Canary", "Full rollout"],
        project_label="Hermes Agent · hermes-agent",
        user_id="777",
    )

    assert result.success is True
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert "Penny" in kwargs["text"]
    assert "Hermes Agent" in kwargs["text"]
    assert "Which rollout mode should I use?" in kwargs["text"]
    assert "resumes automatically" in kwargs["text"]
    assert task_id not in kwargs["text"]
    assert kwargs["reply_markup"] is not None
    state = next(iter(adapter._kanban_decision_state.values()))
    assert state["choices"] == ["Canary", "Full rollout"]


@pytest.mark.asyncio
async def test_choice_callback_records_answer_unblocks_and_expires_card(tmp_path, monkeypatch):
    task_id = _blocked_task(tmp_path, monkeypatch, choices=["Canary", "Full rollout"])
    adapter = _adapter()
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=42)
    await adapter.send_kanban_decision_card(
        chat_id="12345",
        task_id=task_id,
        board=None,
        assignee="penny",
        title="Choose the rollout mode",
        reason="Which rollout mode should I use?",
        choices=["Canary", "Full rollout"],
        user_id="777",
    )
    state_id = next(iter(adapter._kanban_decision_state))
    query = _query(f"kb:{state_id}:c0")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "777"}, clear=False):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "ready"
        assert [comment.body for comment in kb.list_comments(conn, task_id)] == ["Canary"]
    finally:
        conn.close()
    assert state_id not in adapter._kanban_decision_state
    query.edit_message_text.assert_awaited_once()
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), MagicMock()
    )
    assert "already" in query.answer.await_args_list[-1].kwargs["text"].lower()


@pytest.mark.asyncio
async def test_unauthorized_choice_does_not_change_task(tmp_path, monkeypatch):
    task_id = _blocked_task(tmp_path, monkeypatch, choices=["Canary"])
    adapter = _adapter()
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=42)
    await adapter.send_kanban_decision_card(
        chat_id="12345",
        task_id=task_id,
        board=None,
        assignee="penny",
        title="Choose the rollout mode",
        reason="Which rollout mode should I use?",
        choices=["Canary"],
        user_id="777",
    )
    state_id = next(iter(adapter._kanban_decision_state))
    query = _query(f"kb:{state_id}:c0", user_id="999")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "777"}, clear=False):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.list_comments(conn, task_id) == []
    finally:
        conn.close()
    assert "not authorized" in query.answer.call_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_respond_force_reply_correlates_multiple_blocked_cards(tmp_path, monkeypatch):
    first_id = _blocked_task(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        second_id = kb.create_task(conn, title="Choose region", assignee="penny")
        assert kb.block_task(
            conn,
            second_id,
            reason="Which region?",
            kind="needs_input",
        )
    finally:
        conn.close()

    adapter = _adapter()
    adapter._bot.send_message.side_effect = [
        SimpleNamespace(message_id=42),
        SimpleNamespace(message_id=43),
        SimpleNamespace(message_id=100),
    ]
    for task_id, title, reason in (
        (first_id, "Choose rollout", "Which rollout?"),
        (second_id, "Choose region", "Which region?"),
    ):
        await adapter.send_kanban_decision_card(
            chat_id="12345",
            task_id=task_id,
            board=None,
            assignee="penny",
            title=title,
            reason=reason,
            choices=None,
            user_id="777",
        )

    first_state = next(iter(adapter._kanban_decision_state))
    query = _query(f"kb:{first_state}:r", message_id=42)
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "777"}, clear=False):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    prompt_kwargs = adapter._bot.send_message.call_args.kwargs
    assert prompt_kwargs["reply_markup"] is not None
    assert adapter._kanban_decision_prompts[("12345", 100)] == first_state
    prompt = SimpleNamespace(
        message_id=101,
        text="Use canary",
        chat=SimpleNamespace(id=12345, type="private"),
        chat_id=12345,
        from_user=SimpleNamespace(id=777, first_name="Ariel"),
        message_thread_id=None,
        reply_to_message=SimpleNamespace(message_id=100),
    )
    consumed = await adapter._consume_kanban_decision_reply(prompt)
    assert consumed is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, first_id).status == "ready"
        assert kb.get_task(conn, second_id).status == "blocked"
        assert [comment.body for comment in kb.list_comments(conn, first_id)] == ["Use canary"]
        assert kb.list_comments(conn, second_id) == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_later_leaves_task_blocked_and_removes_actions(tmp_path, monkeypatch):
    task_id = _blocked_task(tmp_path, monkeypatch)
    adapter = _adapter()
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=42)
    await adapter.send_kanban_decision_card(
        chat_id="12345",
        task_id=task_id,
        board=None,
        assignee="penny",
        title="Choose rollout",
        reason="Which rollout?",
        choices=None,
        user_id="777",
    )
    state_id = next(iter(adapter._kanban_decision_state))
    query = _query(f"kb:{state_id}:l")

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "777"}, clear=False):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
    finally:
        conn.close()
    assert state_id not in adapter._kanban_decision_state
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_old_card_cannot_answer_after_task_reblocks(tmp_path, monkeypatch):
    task_id = _blocked_task(tmp_path, monkeypatch, choices=["Old choice"])
    conn = kb.connect()
    try:
        old_event_id = [
            event.id for event in kb.list_events(conn, task_id)
            if event.kind == "blocked"
        ][-1]
    finally:
        conn.close()

    adapter = _adapter()
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=42)
    await adapter.send_kanban_decision_card(
        chat_id="12345",
        task_id=task_id,
        board=None,
        blocked_event_id=old_event_id,
        assignee="penny",
        title="Choose rollout",
        reason="Which rollout?",
        choices=["Old choice"],
        user_id="777",
    )
    state_id = next(iter(adapter._kanban_decision_state))

    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="A newer decision is required",
            kind="capability",
            choices=["New choice"],
        )
    finally:
        conn.close()

    query = _query(f"kb:{state_id}:c0")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "777"}, clear=False):
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), MagicMock()
        )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.list_comments(conn, task_id) == []
    finally:
        conn.close()
    assert "already" in query.answer.call_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_tasks_lists_waiting_items_and_reissues_actionable_card(tmp_path, monkeypatch):
    task_id = _blocked_task(tmp_path, monkeypatch, choices=["Canary"])
    conn = kb.connect()
    try:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="12345",
            user_id="777",
        )
    finally:
        conn.close()

    adapter = MagicMock()
    adapter.send_kanban_decision_card = AsyncMock()
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._session_db = None
    runner._authorization_adapter = lambda platform, profile=None: adapter
    monkeypatch.setattr(
        "tools.process_registry.process_registry.list_sessions",
        lambda: [],
    )
    event = MessageEvent(
        text="/tasks",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            user_id="777",
            chat_type="dm",
            profile="penny",
        ),
    )

    result = await runner._handle_agents_command(event)

    assert "Waiting for you" in result
    assert "Choose the rollout mode" in result
    waiting_line = next(
        line for line in result.splitlines()
        if "Choose the rollout mode" in line
    )
    assert task_id not in waiting_line
    adapter.send_kanban_decision_card.assert_awaited_once()
    card = adapter.send_kanban_decision_card.call_args.kwargs
    assert card["task_id"] == task_id
    assert card["choices"] == ["Canary"]
