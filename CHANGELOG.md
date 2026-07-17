# Changelog

## 2026-07-16

- Added restart-safe Kanban supervision for production gateways: an independent systemd dispatcher preserves in-flight workers across gateway/dispatcher restarts, notifier ownership is independently configurable, task filing records idempotency/project/repository/deployment metadata, and transactional resource locks serialize shared checkouts and live deployment targets without consuming retry budget.
- Upgraded `/tasks` and its `/agents` alias into a unified, human-readable fleet-work view. The query-only gateway command now labels live conversations by profile and safe session title, and groups compact Kanban rows by running, waiting for input, and queued, with assignees, short IDs, elapsed time, per-group truncation, and isolated board-read failures.
- Added actionable Telegram STT transcript echoes with native clipboard-copy buttons and a separate Done action. Done safely removes the bot transcript and attempts to remove the original recording when Telegram permissions and message references allow it; long transcripts use labelled 256-character copy parts to respect the Bot API limit.

## 2026-07-14

- Added direct execution of multi-select Telegram `cmd:` button sets when the user taps Done. This lets reminder nudges select and cross off several items in one action without starting an agent turn. Updated `plugins/platforms/telegram/adapter.py` and `tests/gateway/test_telegram_agent_buttons.py`.