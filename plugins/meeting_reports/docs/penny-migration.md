# Penny migration guide

This guide moves Penny's existing meeting workflow to the portable report contract without replacing ingestion or archive code.

Enable the bundled plugin in Penny's profile and restart Penny's gateway before cutover:

```bash
HERMES_HOME="$HOME/.hermes/profiles/penny" hermes plugins enable meeting_reports
```

## 1. Keep existing inputs and archives

Do not replace source resolution, transcript retrieval, recording ingestion, or the conversation archive. Keep the existing payload as the source of truth and map its normalized summary into `plugins.meeting_reports.generate_report`.

## 2. Make internal progress silent

Create one `PipelineEventLog` for the run. Record transcript retrieval, archive reads, summarization, and optional subagent fan-out with `log.stage(...)`. Stage events default to `silent=True` and must not be delivered to the originating chat.

Do not forward kanban lifecycle notifications or child-agent attachments as meeting output. The only routed completion event is `meeting:report_ready`.

## 3. Generate the report at the plugin edge

After Penny's existing summary payload is complete:

1. map summary, decisions, owned action items, and proposed delegations into `generate_report`
2. preserve the source meeting id and archive reference in `source`
3. expose the generated HTML through a session-checked route that checks `store.is_available(report_id)`
4. pass that URL as `report_url`
5. retain the canonical JSON in Penny's `$HERMES_HOME/meeting_reports/`

Do not attach `review.md`, transcript text, source JSON, or child-agent artifacts to the chat by default.

## 4. Route one completion card to the origin

Feed the event log to `route_pipeline_event(...)` with the original adapter, chat id, thread metadata, and report store. Silent events return without delivery. The single `meeting:report_ready` event loads the canonical report and invokes the native Telegram or Slack card method.

The report URL and origin metadata must preserve the same user/session authorization boundary as the source meeting.

## 5. Keep review separate from execution

The card supports Accept, Accept with notes, Reject, Reject with notes, and Dismiss. Slack note actions use a modal. Telegram note actions use ForceReply and consume only the matching response.

A review records a verdict and optional notes. It never creates a kanban task, launches a subagent, or dispatches a proposed delegation. If accepted proposals should become work, Penny must expose a later explicit dispatch action outside this plugin.

## 6. Schedule cleanup

Run cleanup from Penny's profile environment at least hourly:

```bash
HERMES_HOME="$HOME/.hermes/profiles/penny" hermes meeting-report cleanup
```

The report route must stop serving at the same TTL. Confirm both local HTML and canonical JSON disappear after cleanup and that the URL no longer resolves. If Penny uploads static snapshots instead, it must refresh them after review and delete them during cleanup.

## 7. Cutover checks

Before switching production delivery:

- a meeting with no decisions or actions still renders correctly
- action-item owner and due fields survive the canonical round trip
- proposed delegations have no dispatch fields
- internal stage events produce no user-facing messages
- one report-ready event produces one compact card in the originating thread
- Slack and Telegram accept/reject actions persist a verdict
- both platforms collect notes through their native input flow
- duplicate and conflicting clicks leave the first terminal verdict unchanged
- Dismiss hides the card without changing review state
- no raw transcript, JSON, or child artifact is sent by default
- expired reports fail availability checks
- cleanup removes JSON and HTML and revokes the published URL

Run the focused suite:

```bash
scripts/run_tests.sh tests/plugins/test_meeting_reports_*.py
```
