"""Tests for the independent notifier_in_gateway gate."""

import asyncio
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._kanban_sub_fail_counts = {}
    return runner


def _fake_config(*, notifier=True, dispatch=True):
    return {"kanban": {
        "notifier_in_gateway": notifier,
        "dispatch_in_gateway": dispatch,
    }}


def test_notifier_watcher_skips_when_notifier_disabled():
    runner = _make_runner()
    with patch("hermes_cli.config.load_config", return_value=_fake_config(notifier=False)):
        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            asyncio.run(runner._kanban_notifier_watcher())
    mock_connect.assert_not_called()


def test_notifier_watcher_env_override_disables(monkeypatch):
    runner = _make_runner()
    monkeypatch.setenv("HERMES_KANBAN_NOTIFIER_IN_GATEWAY", "false")
    with patch("hermes_cli.config.load_config") as mock_load_config:
        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            asyncio.run(runner._kanban_notifier_watcher())
    mock_load_config.assert_not_called()
    mock_connect.assert_not_called()


def test_notifier_runs_when_dispatch_is_external():
    """External dispatch must not suppress chat delivery."""
    runner = _make_runner(with_adapter=True)
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch(
        "hermes_cli.config.load_config",
        return_value=_fake_config(notifier=True, dispatch=False),
    ):
        with patch.object(
            _kb, "list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch("asyncio.to_thread", side_effect=fake_to_thread):
                    asyncio.run(runner._kanban_notifier_watcher())

    assert past_gate, "notifier should run when dispatch_in_gateway=false"
