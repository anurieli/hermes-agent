"""Telegram decision cards for blocked Kanban work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.telegram import TelegramAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
        return SimpleNamespace(success=True, message_id="42")


class FailingAdapter:
    async def send(self, chat_id, text, metadata=None):
        return SendResult(success=False, error="telegram disconnected")


class AuthRunner:
    def __init__(self, authorized=True):
        self.authorized = authorized

    def _is_user_authorized(self, source):
        return self.authorized

    async def _handle_message(self, event):
        raise AssertionError("decision replies must not reach the agent")


def _make_adapter(*, authorized=True):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._message_handler = AuthRunner(authorized=authorized)._handle_message
    adapter._bot = AsyncMock()
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=9001)
    )
    return adapter


def _make_query(data, *, user_id="111", chat_id="123", thread_id=None):
    chat = SimpleNamespace(type="private")
    message = SimpleNamespace(
        chat_id=int(chat_id),
        chat=chat,
        message_thread_id=thread_id,
        message_id=700,
    )
    return SimpleNamespace(
        data=data,
        message=message,
        from_user=SimpleNamespace(id=user_id, first_name="Ariel"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _setup_blocked(
    tmp_path,
    monkeypatch,
    *,
    title="Ship the billing fix",
    choices=None,
    chat_id="123",
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title=title,
            assignee="penny",
            workspace_kind="dir",
            workspace_path="/work/hermes-agent",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            notifier_profile="penny",
        )
        assert kb.block_task(
            conn,
            tid,
            reason="Choose the rollout window: now or after the customer call?",
            kind="needs_input",
            choices=choices,
        )
        blocked_event = kb.list_events(conn, tid)[-1]
        return tid, blocked_event.id
    finally:
        conn.close()


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = __import__("asyncio").sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_notifier_runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "penny"
    return runner


def _task_state(task_id):
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
        return task.status, [comment.body for comment in comments]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_blocked_needs_input_delivers_human_readable_choice_card(tmp_path, monkeypatch):
    tid, event_id = _setup_blocked(
        tmp_path,
        monkeypatch,
        choices=["Roll out now", "Wait until after the call"],
    )
    adapter = RecordingAdapter()

    await _run_one_notifier_tick(monkeypatch, _make_notifier_runner(adapter))

    assert len(adapter.sent) == 1
    delivery = adapter.sent[0]
    assert delivery["text"].startswith("🟡 Waiting for you")
    assert not delivery["text"].startswith(tid)
    assert "@penny" in delivery["text"]
    assert "hermes-agent" in delivery["text"]
    assert "Ship the billing fix" in delivery["text"]
    assert "Choose the rollout window" in delivery["text"]
    assert "resumes automatically" in delivery["text"]
    buttons = delivery["metadata"]["telegram_inline_keyboard"]
    labels = [button["text"] for row in buttons for button in row]
    assert labels == [
        "Roll out now",
        "Wait until after the call",
        "Respond",
        "Later",
    ]
    callbacks = [button["callback_data"] for row in buttons for button in row]
    assert f"kd:c:{tid}:{event_id}:0" in callbacks
    assert f"kd:r:{tid}:{event_id}" in callbacks


def test_eight_choices_keep_respond_and_later_controls(tmp_path, monkeypatch):
    choices = [f"Choice {index}" for index in range(8)]
    tid, event_id = _setup_blocked(tmp_path, monkeypatch, choices=choices)
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        event = kb.list_events(conn, tid)[-1]
    finally:
        conn.close()

    monkeypatch.setattr(
        "gateway.platforms.telegram.InlineKeyboardButton",
        lambda text, callback_data: SimpleNamespace(
            text=text, callback_data=callback_data,
        ),
    )
    monkeypatch.setattr(
        "gateway.platforms.telegram.InlineKeyboardMarkup",
        lambda rows: SimpleNamespace(inline_keyboard=rows),
    )
    _, metadata = GatewayRunner._kanban_decision_card(task, event)
    keyboard = TelegramAdapter._inline_keyboard_from_metadata(metadata)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert labels == [*choices, "Respond", "Later"]
    assert metadata["telegram_kanban_decision"] == {
        "task_id": tid,
        "blocked_event_id": event_id,
    }


@pytest.mark.asyncio
async def test_failed_send_result_rewinds_notifier_cursor(tmp_path, monkeypatch):
    tid, _ = _setup_blocked(tmp_path, monkeypatch)
    runner = _make_notifier_runner(FailingAdapter())

    await _run_one_notifier_tick(monkeypatch, runner)

    conn = kb.connect()
    try:
        subscription = kb.list_notify_subs(conn, tid)[0]
    finally:
        conn.close()
    assert subscription["last_event_id"] == 0
    assert next(iter(runner._kanban_sub_fail_counts.values())) == 1


def test_answer_blocked_task_is_atomic_and_idempotent(tmp_path, monkeypatch):
    tid, event_id = _setup_blocked(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        status = kb.answer_blocked_task(
            conn,
            tid,
            blocked_event_id=event_id,
            author="telegram:111",
            answer="Roll out now",
        )
        duplicate = kb.answer_blocked_task(
            conn,
            tid,
            blocked_event_id=event_id,
            author="telegram:111",
            answer="Wait",
        )
    finally:
        conn.close()

    assert status == "ready"
    assert duplicate is None
    assert _task_state(tid) == ("ready", ["Roll out now"])


@pytest.mark.asyncio
async def test_choice_callback_records_answer_resumes_and_disables_card(tmp_path, monkeypatch):
    tid, event_id = _setup_blocked(
        tmp_path,
        monkeypatch,
        choices=["Roll out now", "Wait until after the call"],
    )
    adapter = _make_adapter()
    query = _make_query(f"kd:c:{tid}:{event_id}:1")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert _task_state(tid) == ("ready", ["Wait until after the call"])
    assert "Answer recorded" in query.edit_message_text.call_args.kwargs["text"]
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None

    duplicate_query = _make_query(f"kd:c:{tid}:{event_id}:1")
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=duplicate_query), SimpleNamespace()
    )
    assert _task_state(tid) == ("ready", ["Wait until after the call"])
    assert "already been resolved" in duplicate_query.answer.call_args.kwargs["text"].lower()
    duplicate_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_choice_callback_disables_all_reissued_card_copies(tmp_path, monkeypatch):
    tid, event_id = _setup_blocked(tmp_path, monkeypatch, choices=["Ship now"])
    adapter = _make_adapter()
    metadata = {
        "telegram_kanban_decision": {
            "task_id": tid,
            "blocked_event_id": event_id,
        }
    }
    adapter._remember_kanban_decision_message(
        metadata, chat_id="123", message_id=700,
    )
    adapter._remember_kanban_decision_message(
        metadata, chat_id="123", message_id=701,
    )
    query = _make_query(f"kd:c:{tid}:{event_id}:0")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    adapter._bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=123,
        message_id=701,
        reply_markup=None,
    )
    assert adapter._kanban_decision_messages == {}


@pytest.mark.asyncio
async def test_choice_callback_rejects_unauthorized_user(tmp_path, monkeypatch):
    tid, event_id = _setup_blocked(
        tmp_path,
        monkeypatch,
        choices=["Now"],
    )
    adapter = _make_adapter(authorized=False)
    query = _make_query(f"kd:c:{tid}:{event_id}:0", user_id="999")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert _task_state(tid) == ("blocked", [])
    assert "not authorized" in query.answer.call_args.kwargs["text"].lower()
    query.edit_message_text.assert_not_called()


def test_callback_auth_fails_closed_when_runner_hook_errors(monkeypatch):
    adapter = _make_adapter()

    class BrokenAuthRunner:
        def _is_user_authorized(self, source):
            raise RuntimeError("auth backend unavailable")

        async def _handle_message(self, event):
            raise AssertionError

    adapter._message_handler = BrokenAuthRunner()._handle_message
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

    assert adapter._is_callback_user_authorized("111", chat_id="123") is False


@pytest.mark.asyncio
async def test_respond_force_reply_correlates_free_text_to_exact_task(tmp_path, monkeypatch):
    first, _ = _setup_blocked(tmp_path, monkeypatch, title="First blocked task")
    conn = kb.connect()
    try:
        second = kb.create_task(conn, title="Second blocked task", assignee="penny")
        kb.add_notify_sub(
            conn,
            task_id=second,
            platform="telegram",
            chat_id="123",
            notifier_profile="penny",
        )
        assert kb.block_task(
            conn,
            second,
            reason="Which customer should receive the draft?",
            kind="needs_input",
        )
        second_event = kb.list_events(conn, second)[-1].id
    finally:
        conn.close()

    adapter = _make_adapter()
    query = _make_query(f"kd:r:{second}:{second_event}")
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    prompt_kwargs = adapter._bot.send_message.call_args.kwargs
    assert getattr(prompt_kwargs["reply_markup"], "force_reply", False)
    assert f"[kanban:{second}:{second_event}]" in prompt_kwargs["text"]
    assert "Waiting for your reply" in query.edit_message_text.call_args.kwargs["text"]
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None

    reply_target = SimpleNamespace(
        text=prompt_kwargs["text"],
        caption=None,
        message_id=9001,
        from_user=SimpleNamespace(is_bot=True),
    )
    message = SimpleNamespace(
        text="Send it to Acme first",
        reply_to_message=reply_target,
        chat=SimpleNamespace(id=123, type="private"),
        from_user=SimpleNamespace(id=111, first_name="Ariel"),
        message_thread_id=None,
    )
    handled = await adapter._try_handle_kanban_decision_reply(message)

    assert handled is True
    assert _task_state(first) == ("blocked", [])
    assert _task_state(second) == ("ready", ["Send it to Acme first"])


@pytest.mark.asyncio
async def test_free_text_reply_rejects_unauthorized_user(tmp_path, monkeypatch):
    tid, event_id = _setup_blocked(tmp_path, monkeypatch)
    adapter = _make_adapter(authorized=True)
    query = _make_query(f"kd:r:{tid}:{event_id}")
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )
    prompt = adapter._bot.send_message.call_args.kwargs
    adapter._message_handler = AuthRunner(authorized=False)._handle_message
    message = SimpleNamespace(
        text="Ship it",
        reply_to_message=SimpleNamespace(
            text=prompt["text"],
            caption=None,
            message_id=9001,
            from_user=SimpleNamespace(is_bot=True),
        ),
        chat=SimpleNamespace(id=123, type="private"),
        from_user=SimpleNamespace(id=999, first_name="Mallory"),
        message_thread_id=None,
    )

    assert await adapter._try_handle_kanban_decision_reply(message) is True
    assert _task_state(tid) == ("blocked", [])
    assert "not authorized" in adapter._bot.send_message.await_args_list[-1].kwargs["text"].lower()


@pytest.mark.asyncio
async def test_free_text_marker_must_be_bot_prompt_bound_to_message(tmp_path, monkeypatch):
    tid, event_id = _setup_blocked(tmp_path, monkeypatch)
    adapter = _make_adapter()
    forged = SimpleNamespace(
        text="Ship it",
        reply_to_message=SimpleNamespace(
            text=f"[kanban:{tid}:{event_id}]",
            caption=None,
            message_id=1234,
            from_user=SimpleNamespace(is_bot=False),
        ),
        chat=SimpleNamespace(id=123, type="private"),
        from_user=SimpleNamespace(id=111, first_name="Ariel"),
        message_thread_id=None,
    )

    assert await adapter._try_handle_kanban_decision_reply(forged) is False
    assert _task_state(tid) == ("blocked", [])


@pytest.mark.asyncio
async def test_old_card_cannot_answer_after_task_reblocks(tmp_path, monkeypatch):
    tid, old_event_id = _setup_blocked(tmp_path, monkeypatch, choices=["Old choice"])
    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, tid)
        assert kb.block_task(
            conn,
            tid,
            reason="A newer decision is required",
            kind="needs_input",
            choices=["New choice"],
        )
    finally:
        conn.close()

    adapter = _make_adapter()
    query = _make_query(f"kd:c:{tid}:{old_event_id}:0")
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert _task_state(tid) == ("blocked", [])
    assert "no longer current" in query.answer.call_args.kwargs["text"].lower()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_tasks_lists_waiting_items_and_reissues_actionable_card(tmp_path, monkeypatch):
    tid, _ = _setup_blocked(tmp_path, monkeypatch, title="Approve launch copy")
    adapter = RecordingAdapter()
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    monkeypatch.setattr(
        "tools.process_registry.process_registry.list_sessions",
        lambda: [],
    )
    event = MessageEvent(
        text="/tasks",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            user_id="111",
            chat_type="dm",
        ),
    )

    result = await runner._handle_agents_command(event)

    assert "Waiting for you" in result
    assert "Approve launch copy" in result
    assert tid not in result
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["telegram_inline_keyboard"]
