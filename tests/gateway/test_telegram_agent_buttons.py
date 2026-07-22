"""Tests for agent-authored Telegram inline buttons (":::buttons" blocks)."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported
# (mirrors tests/gateway/test_telegram_approval_buttons.py)
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    """Wire up the minimal mocks required to import TelegramAdapter."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    # Provide real exception classes so ``except (NetworkError, ...)`` in
    # connect() doesn't blow up under xdist when this mock leaks.
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import Platform, PlatformConfig


def _make_adapter(extra=None):
    """Create a TelegramAdapter with mocked internals."""
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_query(chat_id=123, user_id=42, data=""):
    query = AsyncMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.delete_message = AsyncMock()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = "Ariel"
    query.from_user.full_name = "Ariel Nurieli"
    query.message = MagicMock()
    query.message.chat_id = chat_id
    query.message.chat = MagicMock()
    query.message.chat.type = "private"
    query.message.chat.title = None
    query.message.chat.full_name = "Ariel"
    query.message.message_thread_id = None
    return query


def _dispatch(adapter, query):
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    return adapter._handle_callback_query(update, context)


# ===========================================================================
# Parsing — _extract_agent_buttons
# ===========================================================================

def test_extract_single_select_block():
    adapter = _make_adapter()
    content = "Which one?\n\n:::buttons\nOption A => payload-a\nOption B => payload-b\n:::"
    clean, spec = adapter._extract_agent_buttons(content)
    assert clean == "Which one?"
    assert spec is not None
    assert spec["multi"] is False
    assert spec["options"] == [("Option A", "payload-a"), ("Option B", "payload-b")]


def test_extract_multi_select_and_default_payload():
    adapter = _make_adapter()
    content = "Pick some:\n:::buttons multi\nCFM call (27 min) => 1\nJust a label\n:::"
    clean, spec = adapter._extract_agent_buttons(content)
    assert clean == "Pick some:"
    assert spec["multi"] is True
    assert spec["options"][0] == ("CFM call (27 min)", "1")
    # No "=>" — payload defaults to the label
    assert spec["options"][1] == ("Just a label", "Just a label")


def test_extract_no_block_returns_unchanged():
    adapter = _make_adapter()
    content = "Just a normal message with ::: nothing special"
    clean, spec = adapter._extract_agent_buttons(content)
    assert clean == content
    assert spec is None


def test_extract_empty_block_strips_but_no_keyboard():
    adapter = _make_adapter()
    content = "Question?\n:::buttons\n\n:::"
    clean, spec = adapter._extract_agent_buttons(content)
    assert clean == "Question?"
    assert spec is None


def test_extract_caps_options_and_truncates_labels():
    adapter = _make_adapter()
    lines = "\n".join(f"{'X' * 100} option {i} => {i}" for i in range(20))
    content = f"Q?\n:::buttons\n{lines}\n:::"
    _, spec = adapter._extract_agent_buttons(content)
    assert len(spec["options"]) == adapter._AGENT_BUTTONS_MAX_OPTIONS
    assert all(len(label) <= 60 for label, _ in spec["options"])


# ===========================================================================
# send() — keyboard attached, state registered
# ===========================================================================

@pytest.mark.asyncio
async def test_send_attaches_keyboard_and_registers_state():
    adapter = _make_adapter()
    mock_msg = MagicMock()
    mock_msg.message_id = 777
    adapter._bot.send_message = AsyncMock(return_value=mock_msg)

    content = "Which granolas?\n\n:::buttons multi\nCFM (27 min) => 1\nDani (4 min) => 2\n:::"
    result = await adapter.send("123", content)

    assert result.success is True
    adapter._bot.send_message.assert_called_once()
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["reply_markup"] is not None
    assert ":::" not in kwargs["text"]

    assert len(adapter._agent_buttons) == 1
    state = next(iter(adapter._agent_buttons.values()))
    assert state["message_id"] == 777
    assert state["chat_id"] == "123"
    assert state["multi"] is True


@pytest.mark.asyncio
async def test_send_without_block_has_no_keyboard():
    adapter = _make_adapter()
    mock_msg = MagicMock()
    mock_msg.message_id = 778
    adapter._bot.send_message = AsyncMock(return_value=mock_msg)

    result = await adapter.send("123", "plain message")

    assert result.success is True
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["reply_markup"] is None
    assert adapter._agent_buttons == {}


@pytest.mark.asyncio
async def test_send_buttons_only_message_gets_placeholder_text():
    adapter = _make_adapter()
    mock_msg = MagicMock()
    mock_msg.message_id = 779
    adapter._bot.send_message = AsyncMock(return_value=mock_msg)

    content = ":::buttons\nYes => yes\nNo => no\n:::"
    result = await adapter.send("123", content)

    assert result.success is True
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert kwargs["reply_markup"] is not None
    assert kwargs["text"].strip()  # not empty — placeholder text used


# ===========================================================================
# Callbacks — taps route payloads into the agent session
# ===========================================================================

@pytest.mark.asyncio
async def test_single_select_tap_injects_payload():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons(
        {"multi": False, "options": [("A", "process 1"), ("B", "process 2")]}
    )

    query = _make_query(data=f"ab:{sid}:1")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await _dispatch(adapter, query)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "process 2"
    assert event.source.user_id == "42"
    # AAS-88: resolution events must carry force_queue so the gateway
    # queues (rather than interrupts/steers) a busy session.
    assert event.force_queue is True
    # State consumed; message bubble deleted (Part 2)
    assert sid not in adapter._agent_buttons
    query.delete_message.assert_awaited_once()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolution_falls_back_to_clearing_keyboard_when_delete_fails():
    """AAS-88 Part 2: Telegram's 48h delete window (or any other
    delete_message failure) must not raise, resolution falls back to the
    old edit_message_reply_markup(reply_markup=None) behavior instead."""
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons(
        {"multi": False, "options": [("Approve", "approve recap"), ("Deny", "deny recap")]}
    )

    query = _make_query(data=f"ab:{sid}:0")
    query.delete_message = AsyncMock(side_effect=Exception("message too old to delete"))
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await _dispatch(adapter, query)  # must not raise

    query.delete_message.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_select_toggle_then_done():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons(
        {"multi": True, "options": [("A", "1"), ("B", "2"), ("C", "3")]}
    )

    toggle_snapshots = []
    original_keyboard = adapter._agent_buttons_keyboard

    def capture_keyboard(state_id):
        toggle_snapshots.append(set(adapter._agent_buttons[state_id]["selected"]))
        return original_keyboard(state_id)

    first_toggle = _make_query(data=f"ab:{sid}:0")
    second_toggle = _make_query(data=f"ab:{sid}:2")
    with (
        patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False),
        patch.object(adapter, "_agent_buttons_keyboard", side_effect=capture_keyboard),
    ):
        await _dispatch(adapter, first_toggle)
        await _dispatch(adapter, second_toggle)
        # Toggles keep state alive and don't inject anything
        adapter.handle_message.assert_not_awaited()
        assert adapter._agent_buttons[sid]["selected"] == {0, 2}
        assert toggle_snapshots == [{0}, {0, 2}]
        first_toggle.edit_message_reply_markup.assert_awaited_once()
        second_toggle.edit_message_reply_markup.assert_awaited_once()

        await _dispatch(adapter, _make_query(data=f"ab:{sid}:done"))

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "1 3"
    assert sid not in adapter._agent_buttons


@pytest.mark.asyncio
async def test_multi_select_toggle_off():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons(
        {"multi": True, "options": [("A", "1"), ("B", "2")]}
    )

    done_query = _make_query(data=f"ab:{sid}:done")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:0"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:0"))  # toggle off
        assert adapter._agent_buttons[sid]["selected"] == set()
        await _dispatch(adapter, done_query)

    # Empty Done resolves visibly but must not inject an ambiguous bare
    # "none" into the long-lived agent conversation.
    adapter.handle_message.assert_not_awaited()
    done_query.answer.assert_awaited_once_with(text="✅ None")
    done_query.delete_message.assert_awaited_once()
    assert sid not in adapter._agent_buttons


@pytest.mark.asyncio
async def test_multi_select_none_button_acknowledges_without_agent_turn():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons(
        {"multi": True, "options": [("A", "1"), ("B", "2")]}
    )

    query = _make_query(data=f"ab:{sid}:none")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await _dispatch(adapter, query)

    adapter.handle_message.assert_not_awaited()
    query.answer.assert_awaited_once_with(text="✅ None")
    query.delete_message.assert_awaited_once()
    assert sid not in adapter._agent_buttons


@pytest.mark.asyncio
async def test_expired_state_answers_gracefully():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()

    query = _make_query(data="ab:deadbeef:0")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await _dispatch(adapter, query)

    adapter.handle_message.assert_not_awaited()
    answer_text = query.answer.await_args.kwargs.get("text", "")
    assert "expired" in answer_text.lower()


@pytest.mark.asyncio
async def test_unauthorized_user_blocked():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons(
        {"multi": False, "options": [("A", "1")]}
    )

    query = _make_query(data=f"ab:{sid}:0")
    # Empty allowlist + no message handler => fail-closed, nobody authorized.
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}, clear=False):
        await _dispatch(adapter, query)

    adapter.handle_message.assert_not_awaited()
    # State survives — an unauthorized tap must not consume it
    assert sid in adapter._agent_buttons
    answer_text = query.answer.await_args.kwargs.get("text", "")
    assert "not authorized" in answer_text.lower()


@pytest.mark.asyncio
async def test_invalid_index_rejected():
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons(
        {"multi": False, "options": [("A", "1")]}
    )

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:99"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:notanint"))

    adapter.handle_message.assert_not_awaited()
    assert sid in adapter._agent_buttons


# ===========================================================================
# Registry bound
# ===========================================================================

def test_registry_caps_state_count():
    adapter = _make_adapter()
    for _ in range(adapter._AGENT_BUTTONS_MAX_STATES + 20):
        adapter._register_agent_buttons({"multi": False, "options": [("A", "1")]})
    assert len(adapter._agent_buttons) == adapter._AGENT_BUTTONS_MAX_STATES


def test_registry_evicts_oldest_first():
    adapter = _make_adapter()
    first_id = adapter._register_agent_buttons({"multi": False, "options": [("A", "1")]})
    for _ in range(adapter._AGENT_BUTTONS_MAX_STATES):
        adapter._register_agent_buttons({"multi": False, "options": [("A", "1")]})
    assert first_id not in adapter._agent_buttons


# ===========================================================================
# Multiple :::buttons blocks in one message -> split into separate messages
# ===========================================================================

def test_split_segments_returns_single_for_zero_or_one_block():
    adapter = _make_adapter()
    assert adapter._split_agent_buttons_segments("plain text") == ["plain text"]
    one = "Pick\n:::buttons\nA => a\nB => b\n:::"
    assert adapter._split_agent_buttons_segments(one) == [one]


def test_split_segments_splits_each_block():
    adapter = _make_adapter()
    content = (
        "Heads up\n"
        "Recap text\n:::buttons\nApprove => approve:1:recap\n:::\n"
        "Todos text\n:::buttons\nApprove => approve:1:todos\n:::\n"
        "Email text\n:::buttons\nApprove => approve:1:email\n:::"
    )
    segs = adapter._split_agent_buttons_segments(content)
    assert len(segs) == 3
    for seg in segs:
        assert seg.count(":::buttons") == 1
    assert "recap" in segs[0]
    assert "todos" in segs[1]
    assert "email" in segs[2]


# ===========================================================================
# Backend-command buttons (AAS-88 Part 1b) — Approve/Deny run silently,
# no agent turn.
# ===========================================================================

def _write_helper(bin_dir, name="commit_piece.py", body=None, executable=True):
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(body or (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('Approved. ' + ' '.join(sys.argv[1:]))\n"
    ))
    if executable:
        path.chmod(0o755)
    return path


def _write_reminders_helper(bin_dir, fail_args=None):
    """Fake reminders.py cross-off: exits nonzero when one of the given
    --text values is present, otherwise prints a terse per-item line (as
    the real Part 2 terse output does)."""
    fail_args = fail_args or []
    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"FAIL = {fail_args!r}\n"
        "argv = sys.argv[1:]\n"
        "if any(a in argv for a in FAIL):\n"
        "    print('boom', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print('Crossed off: ' + ' '.join(argv))\n"
    )
    return _write_helper(bin_dir, name="reminders.py", body=body)


@pytest.mark.asyncio
async def test_backend_command_tap_runs_helper_posts_confirmation_no_turn(tmp_path):
    _write_helper(tmp_path / "bin")
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock(return_value=None)
    sid = adapter._register_agent_buttons({
        "multi": False,
        "options": [
            ("Approve", "cmd:commit_piece.py --source-id not_x --piece todos --action approve"),
            ("Deny", "cmd:commit_piece.py --source-id not_x --piece todos --action deny"),
        ],
    })

    query = _make_query(data=f"ab:{sid}:0")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*", "HERMES_HOME": str(tmp_path)}, clear=False):
        await _dispatch(adapter, query)

    # No agent turn started at all.
    adapter.handle_message.assert_not_awaited()
    # Bubble deleted, confirmation posted via the adapter send path.
    query.delete_message.assert_awaited_once()
    adapter.send.assert_awaited_once()
    sent_chat_id, sent_text = adapter.send.await_args.args
    assert sent_chat_id == str(query.message.chat_id)
    assert "Approved" in sent_text
    assert "not_x" in sent_text
    assert "todos" in sent_text
    assert "approve" in sent_text
    assert sid not in adapter._agent_buttons


@pytest.mark.asyncio
async def test_multi_select_backend_commands_run_on_done_without_agent_turn(tmp_path):
    """AAS-88 Part 1: every selected command still runs, but the chat gets
    ONE short summary line, not the joined raw stdout of each command."""
    _write_helper(tmp_path / "bin")
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock(return_value=None)
    sid = adapter._register_agent_buttons({
        "multi": True,
        "options": [
            ("Reminder A", "cmd:commit_piece.py --item a"),
            ("Reminder B", "cmd:commit_piece.py --item b"),
            ("Reminder C", "cmd:commit_piece.py --item c"),
        ],
    })

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*", "HERMES_HOME": str(tmp_path)}, clear=False):
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:0"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:2"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:done"))

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    confirmation = adapter.send.await_args.args[1]
    # Every selected command ran (both a and c were tapped) ...
    # ... but the chat only sees a one-line count, never the raw per-command
    # stdout or a newline-joined blob.
    assert "\n" not in confirmation
    assert "--item a" not in confirmation
    assert "--item c" not in confirmation
    assert "Approved" not in confirmation
    assert "2" in confirmation
    assert sid not in adapter._agent_buttons


@pytest.mark.asyncio
async def test_multi_select_backend_commands_homogeneous_summary(tmp_path):
    """When every selected command is the same helper+subcommand (e.g. all
    reminders.py cross-off), the summary names the count with a short noun,
    e.g. "Crossed off 3 reminders." -- not raw JSON stdout."""
    _write_reminders_helper(tmp_path / "bin")
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock(return_value=None)
    sid = adapter._register_agent_buttons({
        "multi": True,
        "options": [
            ("Reminder A", "cmd:reminders.py cross-off --text a"),
            ("Reminder B", "cmd:reminders.py cross-off --text b"),
            ("Reminder C", "cmd:reminders.py cross-off --text c"),
        ],
    })

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*", "HERMES_HOME": str(tmp_path)}, clear=False):
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:0"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:1"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:2"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:done"))

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    confirmation = adapter.send.await_args.args[1]
    assert confirmation == "Crossed off 3 reminders."
    assert "\n" not in confirmation
    assert '{"status"' not in confirmation


@pytest.mark.asyncio
async def test_multi_select_backend_commands_partial_failure_one_summary_line(tmp_path):
    """One command failing must not raise, drop the others, or flood the
    chat -- it folds into the same single summary line."""
    _write_reminders_helper(tmp_path / "bin", fail_args=["b"])
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock(return_value=None)
    sid = adapter._register_agent_buttons({
        "multi": True,
        "options": [
            ("Reminder A", "cmd:reminders.py cross-off --text a"),
            ("Reminder B", "cmd:reminders.py cross-off --text b"),
            ("Reminder C", "cmd:reminders.py cross-off --text c"),
        ],
    })

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*", "HERMES_HOME": str(tmp_path)}, clear=False):
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:0"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:1"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:2"))
        await _dispatch(adapter, _make_query(data=f"ab:{sid}:done"))

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    confirmation = adapter.send.await_args.args[1]
    assert confirmation == "Crossed off 2 of 3 reminders, 1 failed."
    assert "\n" not in confirmation


@pytest.mark.asyncio
async def test_feedback_tap_still_injects_message_event():
    """Give-feedback stays a plain inject payload -> normal agent turn."""
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    sid = adapter._register_agent_buttons({
        "multi": False,
        "options": [
            ("Approve", "cmd:commit_piece.py --source-id not_x --piece todos --action approve"),
            ("Give feedback", "feedback:not_x:todos"),
        ],
    })

    query = _make_query(data=f"ab:{sid}:1")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await _dispatch(adapter, query)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "feedback:not_x:todos"
    assert event.force_queue is True


@pytest.mark.asyncio
async def test_two_rapid_backend_command_taps_both_complete(tmp_path):
    _write_helper(tmp_path / "bin")
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock(return_value=None)
    sid_a = adapter._register_agent_buttons({
        "multi": False,
        "options": [("Approve", "cmd:commit_piece.py --source-id not_x --piece todos --action approve")],
    })
    sid_b = adapter._register_agent_buttons({
        "multi": False,
        "options": [("Approve", "cmd:commit_piece.py --source-id not_x --piece reminders --action approve")],
    })

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*", "HERMES_HOME": str(tmp_path)}, clear=False):
        await asyncio.gather(
            _dispatch(adapter, _make_query(data=f"ab:{sid_a}:0")),
            _dispatch(adapter, _make_query(data=f"ab:{sid_b}:0")),
        )

    adapter.handle_message.assert_not_awaited()
    assert adapter.send.await_count == 2
    texts = [call.args[1] for call in adapter.send.await_args_list]
    assert any("todos" in t for t in texts)
    assert any("reminders" in t for t in texts)


@pytest.mark.asyncio
async def test_backend_command_rejects_path_traversal():
    adapter = _make_adapter()
    result = await adapter._run_backend_command("../../etc/passwd --wat")
    assert "failed" in result.lower()


@pytest.mark.asyncio
async def test_backend_command_rejects_helper_outside_bin_dir(tmp_path):
    other_dir = tmp_path / "not-bin"
    other_dir.mkdir()
    _write_helper(other_dir, name="evil.py")
    (tmp_path / "bin").mkdir()

    adapter = _make_adapter()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False):
        result = await adapter._run_backend_command("evil.py")
    assert "failed" in result.lower() or "not a registered" in result.lower()


@pytest.mark.asyncio
async def test_backend_command_rejects_non_executable_helper(tmp_path):
    _write_helper(tmp_path / "bin", executable=False)
    adapter = _make_adapter()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False):
        result = await adapter._run_backend_command("commit_piece.py")
    assert "not a registered" in result.lower()


@pytest.mark.asyncio
async def test_send_multiple_blocks_sends_one_message_per_block():
    adapter = _make_adapter()
    mock_msg = MagicMock()
    mock_msg.message_id = 900
    adapter._bot.send_message = AsyncMock(return_value=mock_msg)

    content = (
        "Recap\n:::buttons\nApprove => approve:1:recap\n:::\n"
        "Todos\n:::buttons\nApprove => approve:1:todos\n:::\n"
        "Email\n:::buttons\nApprove => approve:1:email\n:::"
    )
    result = await adapter.send("123", content)

    assert result.success is True
    assert adapter._bot.send_message.call_count == 3
    for call in adapter._bot.send_message.call_args_list:
        assert call.kwargs["reply_markup"] is not None
        assert ":::" not in call.kwargs["text"]
    assert len(adapter._agent_buttons) == 3
