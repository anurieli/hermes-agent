# Changelog

## 2026-07-23

### Added

- Added inbound Slack `reaction_added` routing for human `:white_check_mark:` reactions on Hermes-authored top-level messages.
- Preserved the reacting user identity and original Slack thread when dispatching reaction signals to the agent.
- Ignored other emoji, thread replies, malformed events, duplicate deliveries, bot/app reactions, unverified actors, excluded channels, and targets that cannot be verified against the current workspace bot.
- Added `reaction_added` and `reactions:read` to generated Slack app manifests.
- Documented the required Slack app manifest update and reinstall flow.
- Added focused gateway and manifest tests for reaction registration, routing, filtering, deduplication, and thread mapping.

### Files

- `gateway/platforms/slack.py`
- `hermes_cli/slack_cli.py`
- `tests/gateway/test_slack.py`
- `tests/gateway/test_slack_reactions.py`
- `tests/hermes_cli/test_slack_cli.py`
- `website/docs/user-guide/messaging/slack.md`
