"""Tests for persist-by-default model switching.

Covers:
- ``parse_model_flags`` recognises ``--session`` (and keeps ``--global``).
- ``resolve_persist_behavior`` applies the config-gated default and the
  ``--session`` / ``--global`` overrides.
- The default (no flags) persists, so a plain ``/model <name>`` survives
  ``/new``. Temporary switches require ``--session`` or ``--once``.
"""

from unittest.mock import patch

from hermes_cli.model_switch import parse_model_flags, resolve_persist_behavior


# ---------------------------------------------------------------------------
# parse_model_flags
# ---------------------------------------------------------------------------


class TestParseModelFlagsSession:
    def test_no_flags(self):
        assert parse_model_flags("sonnet") == ("sonnet", "", False, False, False)


    def test_unicode_dash_session_normalized(self):
        # Telegram/iOS auto-converts -- to en/em dashes.
        assert parse_model_flags("sonnet \u2013session") == (
            "sonnet",
            "",
            False,
            False,
            True,
        )


# ---------------------------------------------------------------------------
# resolve_persist_behavior
# ---------------------------------------------------------------------------


class TestResolvePersistBehavior:
    def test_no_flags_persist_when_config_missing(self):
        with _config({}):
            assert resolve_persist_behavior(False, False) is True

    def test_no_flags_persist_when_model_config_is_flat(self):
        with _config({"model": "gpt-5.6-terra"}):
            assert resolve_persist_behavior(False, False) is True

    def test_session_flag_always_session_only(self):
        # --session opts out even if the config default is True.
        with _config({"model": {"persist_switch_by_default": True}}):
            assert resolve_persist_behavior(False, True) is False


    def test_no_provider_uses_config_default(self):
        # No --provider → respects config default (True).
        with _config({"model": {"persist_switch_by_default": True}}):
            assert resolve_persist_behavior(False, False, explicit_provider="") is True

    def test_provider_switch_persists_without_temporary_flag(self):
        with _config({}):
            assert (
                resolve_persist_behavior(
                    False, False, explicit_provider="openai-codex"
                )
                is True
            )

    def test_once_flag_is_temporary(self):
        with _config({}):
            assert resolve_persist_behavior(False, False, is_once=True) is False


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


class _config:
    """Context manager that patches ``load_config`` to return a fixed dict."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def __enter__(self):
        self._patch = patch(
            "hermes_cli.config.load_config",
            return_value=self.cfg,
        )
        # resolve_persist_behavior imports load_config lazily inside the
        # function, so patching the source module is sufficient.
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
