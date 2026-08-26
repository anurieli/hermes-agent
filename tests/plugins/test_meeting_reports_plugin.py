"""Plugin registration wiring for meeting_reports."""

from __future__ import annotations

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from plugins.meeting_reports import register
from plugins.meeting_reports.cards import (
    CALLBACK_PREFIX,
    SLACK_OPEN_ACTION_ID,
    SLACK_VIEW_CALLBACK_ID,
)


def _registered_manager() -> PluginManager:
    manager = PluginManager()
    register(PluginContext(PluginManifest(name="meeting_reports"), manager))
    return manager


def test_register_adds_cli_command():
    manager = _registered_manager()
    entry = manager._cli_commands["meeting-report"]
    assert entry["plugin"] == "meeting_reports"
    assert callable(entry["setup_fn"])
    assert callable(entry["handler_fn"])


def test_register_adds_slack_action_and_view_handlers():
    manager = _registered_manager()
    action_ids = [entry[0] for entry in manager.get_slack_action_handlers()]
    assert SLACK_OPEN_ACTION_ID in action_ids
    assert any(hasattr(action_id, "match") for action_id in action_ids)
    view_ids = [entry[0] for entry in manager.get_slack_view_handlers()]
    assert SLACK_VIEW_CALLBACK_ID in view_ids


def test_register_adds_telegram_callback_and_text_interceptor():
    manager = _registered_manager()
    prefixes = [entry[0] for entry in manager.get_telegram_callback_handlers()]
    assert f"{CALLBACK_PREFIX}:" in prefixes
    assert len(manager.get_telegram_text_interceptors()) == 1


def test_registration_surfaces_reject_non_callables_and_empty_keys():
    manager = PluginManager()
    ctx = PluginContext(PluginManifest(name="x"), manager)
    with pytest.raises(ValueError):
        ctx.register_telegram_callback_handler("foo:", "not-callable")
    with pytest.raises(ValueError):
        ctx.register_telegram_callback_handler("", lambda: None)
    with pytest.raises(ValueError):
        ctx.register_slack_view_handler("", lambda: None)
    with pytest.raises(ValueError):
        ctx.register_telegram_text_interceptor("not-callable")


def test_force_discovery_registry_reset_clears_new_handler_types():
    manager = _registered_manager()
    assert manager.get_slack_view_handlers()
    assert manager.get_telegram_text_interceptors()
    manager._slack_view_handlers.clear()
    manager._telegram_text_interceptors.clear()
    assert manager.get_slack_view_handlers() == []
    assert manager.get_telegram_text_interceptors() == []
