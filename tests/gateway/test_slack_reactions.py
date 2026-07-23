"""
Tests for inbound Slack reaction routing (reaction_added).

Only one narrow signal wakes the agent: a human adding a :white_check_mark:
reaction to a top-level message THIS workspace's bot authored. The adapter then
routes a normalized synthetic user MessageEvent through the same callback
regular messages use, waking the agent in that exact thread so the profile's
policy can treat the reaction as an explicit signal. The transport stays
profile-independent (it delivers the emoji, it does not interpret it).

Covers: valid root routing, wrong emoji, thread-reply rejection, wrong author,
timestamp mismatch, self / other-bot reaction, malformed items, allowed-channel
rejection, outer event_id dedup, multi-workspace client/bot identity,
restart-safe API verification (no reliance on local send-time state), and reply
recovery text.

slack-bolt may not be installed; we reuse the same import-time mock pattern as
test_slack.py so the adapter can be imported.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Mock the slack-bolt package if it's not installed (mirrors test_slack.py)
# ---------------------------------------------------------------------------

def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)

    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import gateway.platforms.slack as _slack_mod  # noqa: E402
_slack_mod.SLACK_AVAILABLE = True

from gateway.platforms.slack import SlackAdapter  # noqa: E402


_RAISE = object()  # sentinel: make conversations.history raise (fetch failure)


@pytest.fixture()
def adapter():
    config = PlatformConfig(
        enabled=True,
        token="xoxb-fake-token",
        extra={"allowed_channels": []},
    )
    a = SlackAdapter(config)
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._bot_user_id = TestReactionAddedRouting.BOT_UID
    a._team_bot_user_ids = {TestReactionAddedRouting.TEAM: TestReactionAddedRouting.BOT_UID}
    # Keep these tests isolated from constructor or cross-module Slack state.
    a._team_clients = {}
    a._channel_team = {}
    a._user_name_cache = {}
    a._running = True
    a.handle_message = AsyncMock()
    return a


class TestReactionAddedRouting:
    """Inbound Slack reaction_added routing onto bot-authored root messages."""

    BOT_UID = "U_BOT"
    HUMAN_UID = "U_HUMAN"
    TEAM = "T_MAIN"
    CHANNEL = "C_ROOM"
    MSG_TS = "1700000000.000100"
    EVENT_TS = "1700000001.000200"
    EVENT_ID = "Ev0REACTION1"

    def _reaction_event(
        self,
        *,
        reaction="white_check_mark",
        user=None,
        item_user=None,
        item_type="message",
        channel=None,
        item_ts=None,
        event_ts=None,
    ):
        item = {"type": item_type}
        if channel is not None:
            item["channel"] = channel
        elif item_type == "message":
            item["channel"] = self.CHANNEL
        if item_ts is not None:
            item["ts"] = item_ts
        elif item_type == "message":
            item["ts"] = self.MSG_TS
        event = {
            "type": "reaction_added",
            "user": user if user is not None else self.HUMAN_UID,
            "reaction": reaction,
            "item": item,
            "event_ts": event_ts if event_ts is not None else self.EVENT_TS,
        }
        if item_user is not None:
            event["item_user"] = item_user
        return event

    def _body(self, *, event_id=None, team_id=None):
        return {
            "event_id": event_id if event_id is not None else self.EVENT_ID,
            "team_id": team_id if team_id is not None else self.TEAM,
        }

    def _context(self, *, bot_user_id=None, team_id=None):
        return {
            "bot_user_id": bot_user_id if bot_user_id is not None else self.BOT_UID,
            "team_id": team_id if team_id is not None else self.TEAM,
        }

    def _client(self, *, history_message=None, is_dm=False, is_mpim=False):
        """Build a workspace-scoped client mock for a routing test."""
        client = AsyncMock()
        if history_message is _RAISE:
            client.conversations_history = AsyncMock(
                side_effect=Exception("no scope"),
            )
        else:
            msgs = [history_message] if history_message else []
            client.conversations_history = AsyncMock(
                return_value={"messages": msgs},
            )
        client.conversations_info = AsyncMock(
            return_value={"channel": {"is_im": is_dm, "is_mpim": is_mpim}},
        )

        async def human_lookup(*, user):
            return {
                "user": {
                    "id": user,
                    "is_bot": False,
                    "is_app_user": False,
                    "deleted": False,
                    "profile": {"display_name": "Human"},
                },
            }

        client.users_info = AsyncMock(side_effect=human_lookup)
        return client

    async def _run(self, adapter, event, *, client, body=None, context=None):
        await adapter._handle_reaction_added(
            event,
            body=body if body is not None else self._body(),
            context=context if context is not None else self._context(),
            client=client,
        )

    # ----- Valid routing -----

    @pytest.mark.asyncio
    async def test_valid_reaction_routes_synthetic_message(self, adapter):
        """A human check-mark on a bot root routes a synthetic in-thread event."""
        client = self._client(
            history_message={
                "ts": self.MSG_TS,
                "user": self.BOT_UID,
                "bot_id": "B1",
                "text": "[cody task=task-123 repo=hermes-agent]",
            },
        )
        await self._run(adapter, self._reaction_event(), client=client)

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.source.user_id == self.HUMAN_UID
        assert event.source.chat_id == self.CHANNEL
        assert event.source.thread_id == self.MSG_TS  # exact root thread
        assert event.message_id == self.EVENT_ID      # outer event_id
        assert ":white_check_mark:" in event.text
        assert "reaction" in event.text.lower()
        assert event.reply_to_message_id == self.MSG_TS
        # Reacted text preserved as recovery context.
        assert event.reply_to_text == "[cody task=task-123 repo=hermes-agent]"

    @pytest.mark.asyncio
    async def test_root_with_replies_routes(self, adapter):
        """A root that already has replies (thread_ts == its own ts) still routes."""
        client = self._client(
            history_message={
                "ts": self.MSG_TS,
                "user": self.BOT_UID,
                "bot_id": "B1",
                "thread_ts": self.MSG_TS,
                "text": "Root with replies",
            },
        )
        await self._run(adapter, self._reaction_event(), client=client)

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.source.thread_id == self.MSG_TS
        assert event.reply_to_text == "Root with replies"

    # ----- Wrong emoji / target rejections -----

    @pytest.mark.asyncio
    async def test_non_check_mark_reaction_ignored(self, adapter):
        """Only :white_check_mark: is a signal; other emoji are ignored."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        await self._run(
            adapter, self._reaction_event(reaction="eyes"), client=client,
        )
        adapter.handle_message.assert_not_awaited()
        # Cheap reject: never even fetched the message.
        client.conversations_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_thread_reply_target_rejected(self, adapter):
        """A reaction on a bot thread reply (not a root) is rejected."""
        client = self._client(
            history_message={
                "ts": self.MSG_TS,
                "user": self.BOT_UID,
                "bot_id": "B1",
                "thread_ts": "1699999999.000001",  # different root -> a reply
                "text": "Bot reply text",
            },
        )
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_on_non_bot_message_ignored(self, adapter):
        """A reaction on a message authored by someone else is ignored."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": "U_OTHER"},
        )
        await self._run(
            adapter, self._reaction_event(item_user="U_OTHER"), client=client,
        )
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_human_root_hermes_replied_under_is_rejected(self, adapter):
        """A human root that Hermes merely replied under is NOT bot-authored."""
        client = self._client(
            history_message={
                "ts": self.MSG_TS,
                "user": self.HUMAN_UID,  # human owns the root
                "thread_ts": self.MSG_TS,
                "text": "Human question that Hermes answered",
            },
        )
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timestamp_mismatch_rejected(self, adapter):
        """A fetched message whose ts differs from the reacted ts fails closed."""
        client = self._client(
            history_message={
                "ts": "1700000000.999999",  # not the reacted ts
                "user": self.BOT_UID,
                "bot_id": "B1",
            },
        )
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_not_awaited()

    # ----- Self / other-bot / malformed -----

    @pytest.mark.asyncio
    async def test_bot_self_reaction_ignored(self, adapter):
        """The bot reacting to its own message must not loop back."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        await self._run(
            adapter, self._reaction_event(user=self.BOT_UID), client=client,
        )
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_bot_reaction_ignored(self, adapter):
        """A different bot in the same workspace cannot wake the agent."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        client.users_info = AsyncMock(
            return_value={
                "user": {
                    "id": "U_OTHER_BOT",
                    "is_bot": True,
                    "profile": {"display_name": "Other bot"},
                },
            },
        )
        await self._run(
            adapter, self._reaction_event(user="U_OTHER_BOT"), client=client,
        )
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_actor_lookup_failure_ignored(self, adapter):
        """An unverifiable reaction actor fails closed."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        client.users_info = AsyncMock(side_effect=Exception("no users scope"))
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_user_id_matching_other_workspace_bot_is_not_suppressed(self, adapter):
        """Workspace-scoped user IDs are not compared across installations."""
        adapter._team_bot_user_ids["T_OTHER"] = "U_SHARED"
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        await self._run(
            adapter, self._reaction_event(user="U_SHARED"), client=client,
        )
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_malformed_reaction_ignored(self, adapter):
        """Non-message items and missing identifiers are ignored."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        # File reaction (item.type != message)
        await self._run(
            adapter,
            self._reaction_event(item_type="file", channel=None, item_ts=None),
            client=client,
        )
        # Message item but missing ts
        await self._run(
            adapter,
            self._reaction_event(item_ts="", channel=self.CHANNEL),
            client=client,
        )
        # Missing reacting user
        await self._run(adapter, self._reaction_event(user=""), client=client)
        adapter.handle_message.assert_not_awaited()

    # ----- Dedup on outer event_id -----

    @pytest.mark.asyncio
    async def test_duplicate_event_id_ignored(self, adapter):
        """A redelivered reaction (same outer event_id) routes only once."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        await self._run(adapter, self._reaction_event(), client=client)
        await self._run(adapter, self._reaction_event(), client=client)
        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_duplicate_without_event_id_ignored(self, adapter):
        """Fallback composite key deduplicates deliveries missing event_id."""
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        body = self._body(event_id="")
        await self._run(adapter, self._reaction_event(), client=client, body=body)
        await self._run(adapter, self._reaction_event(), client=client, body=body)
        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_same_event_id_in_different_workspaces_is_not_a_duplicate(self, adapter):
        """Outer event IDs are namespaced by installation team."""
        adapter._team_bot_user_ids = {"T_A": "U_BOT_A", "T_B": "U_BOT_B"}
        client_a = self._client(
            history_message={"ts": self.MSG_TS, "user": "U_BOT_A", "bot_id": "B_A"},
        )
        client_b = self._client(
            history_message={"ts": self.MSG_TS, "user": "U_BOT_B", "bot_id": "B_B"},
        )

        await self._run(
            adapter,
            self._reaction_event(),
            client=client_a,
            body=self._body(team_id="T_A"),
            context=self._context(bot_user_id="U_BOT_A", team_id="T_A"),
        )
        await self._run(
            adapter,
            self._reaction_event(),
            client=client_b,
            body=self._body(team_id="T_B"),
            context=self._context(bot_user_id="U_BOT_B", team_id="T_B"),
        )

        assert adapter.handle_message.await_count == 2

    # ----- Allowed channels -----

    @pytest.mark.asyncio
    async def test_allowed_channel_rejection(self, adapter):
        """A reaction in a channel outside the whitelist is ignored pre-dispatch."""
        adapter.config.extra["allowed_channels"] = ["C_ALLOWED"]
        client = self._client(
            history_message={"ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1"},
        )
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_not_awaited()
        # Rejected before verifying the message.
        client.conversations_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_channel_allows_listed_channel(self, adapter):
        """A reaction in a whitelisted channel routes normally."""
        adapter.config.extra["allowed_channels"] = [self.CHANNEL]
        client = self._client(
            history_message={
                "ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1",
                "text": "hi",
            },
        )
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mpim_bypasses_allowed_channel_whitelist(self, adapter):
        """MPIM (group DM) reactions are treated like DMs and skip the whitelist."""
        adapter.config.extra["allowed_channels"] = ["C_ALLOWED"]
        client = self._client(
            history_message={
                "ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1",
                "text": "hi",
            },
            is_mpim=True,
        )
        # MPIM channel ids share the "G" prefix with private channels, so
        # classification relies on conversations.info reporting is_mpim.
        event = self._reaction_event(channel="G_MPIM", item_ts=self.MSG_TS)
        await self._run(adapter, event, client=client)
        adapter.handle_message.assert_awaited_once()
        routed = adapter.handle_message.await_args.args[0]
        assert routed.source.chat_type == "dm"

    # ----- Multi-workspace identity -----

    @pytest.mark.asyncio
    async def test_multi_workspace_prefers_team_bot_and_client(self, adapter):
        """Per-team mappings override a primary Bolt context and client."""
        # Two installations; the event belongs to workspace B.
        adapter._team_bot_user_ids = {"T_A": "U_BOT_A", "T_B": "U_BOT_B"}
        adapter._bot_user_id = "U_BOT_A"  # primary is A, not the reacting ws
        client_b = self._client(
            history_message={
                "ts": self.MSG_TS, "user": "U_BOT_B", "bot_id": "B_B",
                "text": "posted by workspace B bot",
            },
        )
        client_primary = self._client(history_message=None)
        adapter._team_clients = {"T_A": client_primary, "T_B": client_b}
        await self._run(
            adapter,
            self._reaction_event(),
            client=client_primary,
            body=self._body(team_id="T_B"),
            context=self._context(bot_user_id="U_BOT_A", team_id="T_B"),
        )
        adapter.handle_message.assert_awaited_once()
        # The mapped workspace B client was queried, not Bolt's primary client.
        client_b.conversations_history.assert_awaited_once()
        client_primary.conversations_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_message_from_other_workspace_bot_rejected(self, adapter):
        """A root authored by a DIFFERENT workspace's bot is not our bot."""
        adapter._team_bot_user_ids = {"T_A": "U_BOT_A", "T_B": "U_BOT_B"}
        client_b = self._client(
            history_message={
                "ts": self.MSG_TS, "user": "U_BOT_A",  # authored by A, not B
                "bot_id": "B_A",
            },
        )
        await self._run(
            adapter,
            self._reaction_event(),
            client=client_b,
            body=self._body(team_id="T_B"),
            context=self._context(bot_user_id="U_BOT_B", team_id="T_B"),
        )
        adapter.handle_message.assert_not_awaited()

    # ----- Restart safety -----

    @pytest.mark.asyncio
    async def test_restart_safe_verifies_via_api_not_local_state(self, adapter):
        """After a restart (no local send-time state), API verification still routes."""
        # _bot_message_ts is empty, as it would be on a fresh process.
        assert not adapter._bot_message_ts
        client = self._client(
            history_message={
                "ts": self.MSG_TS, "user": self.BOT_UID, "bot_id": "B1",
                "text": "recovered root",
            },
        )
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_failure_fails_closed(self, adapter):
        """When the message can't be fetched, the reaction is ignored (no local trust)."""
        client = self._client(history_message=_RAISE)
        adapter._bot_message_ts.add(self.MSG_TS)  # stale local hint must NOT rescue it
        await self._run(adapter, self._reaction_event(), client=client)
        adapter.handle_message.assert_not_awaited()
