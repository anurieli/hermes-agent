# Changelog

## 2026-08-05

- Fixed a Slack Socket Mode failure that made an agent go permanently deaf while every health signal stayed green. `slack_sdk`'s `AsyncSocketModeClient.connect()` is an unbounded `while True` whose only exit is a successful `ws_connect`; it never consults `self.closed`, so a reconnect in flight when the client closes keeps retrying against the closed aiohttp session every `ping_interval`, forever. Cody hit this on 2026-08-03: last inbound Slack message 08:27, then ~1,500 retries at a 10-second cadence over 43 hours with zero adapter-level restarts. The gateway stayed `active (running)`, `NRestarts` stayed 0, and outbound `chat.postMessage` kept working on a separate client, so his hourly alerts still arrived and nothing looked broken. Added `_harden_socket_client`, which supervises `connect()` and cancels the retry task once the client is closed, turning an invisible infinite loop into a normal exception the adapter watchdog can act on. Reordered `_stop_socket_mode_handler` to cancel our task *before* closing the handler, so the loop unwinds while its session is still valid, and to unconditionally cancel the client-internal futures that `close()` only cancels on a clean run. Updated `plugins/platforms/slack/adapter.py`.

## 2026-07-21

- Fixed empty Telegram multi-select Done/None actions leaking a bare `none` into the active agent conversation. They now acknowledge and dismiss deterministically without generating an unrelated agent turn; selected actions retain their existing behavior. Added callback and selection-render regression coverage.

## 2026-07-16

- Added restart-safe Kanban supervision for production gateways: an independent systemd dispatcher preserves in-flight workers across gateway/dispatcher restarts, notifier ownership is independently configurable, task filing records idempotency/project/repository/deployment metadata, and transactional resource locks serialize shared checkouts and live deployment targets without consuming retry budget.
- Upgraded `/tasks` and its `/agents` alias into a unified, human-readable fleet-work view. The query-only gateway command now labels live conversations by profile and safe session title, and groups compact Kanban rows by running, waiting for input, and queued, with assignees, short IDs, elapsed time, per-group truncation, and isolated board-read failures.
- Added actionable Telegram STT transcript echoes with native clipboard-copy buttons and a separate Done action. Done safely removes the bot transcript and attempts to remove the original recording when Telegram permissions and message references allow it; long transcripts use labelled 256-character copy parts to respect the Bot API limit.

## 2026-07-14

- Added direct execution of multi-select Telegram `cmd:` button sets when the user taps Done. This lets reminder nudges select and cross off several items in one action without starting an agent turn. Updated `plugins/platforms/telegram/adapter.py` and `tests/gateway/test_telegram_agent_buttons.py`.