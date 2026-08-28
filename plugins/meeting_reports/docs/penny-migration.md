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

`PipelineEventLog` is in-process bookkeeping only. It cannot suppress a real kanban task's own lifecycle events. If Penny's existing pipeline dispatches Granola/Pocket/Google Meet processing (or any per-source worker) as kanban tasks, create the parent task and every child with `notify_mode="silent"` (children of a silent parent can pass `notify_mode="inherit"` instead of repeating `"silent"`):

```python
parent_id = kb.create_task(
    conn, title="Process Granola meeting", assignee="penny-worker",
    notify_mode="silent",
)
child_id = kb.create_task(
    conn, title="Summarize transcript", parents=(parent_id,),
    assignee="penny-worker", notify_mode="inherit",
)
```

An ordinary kanban task (`notify_mode` omitted or `"default"`) still notifies its subscribers exactly as before. That default must not change for any OTHER task on Penny's board. Only the tasks that back a "Process" run should be created silent. Verify this with a real gateway notifier tick against a test board, not by reading the dispatching code: create the silent task, complete/block/crash it, run one `_kanban_notifier_watcher` tick, and confirm the subscribed adapter received nothing.

## 3. Generate the report at the plugin edge

After Penny's existing summary payload is complete:

1. map summary, decisions, owned action items, and proposed delegations into `generate_report`
2. preserve the source meeting id and archive reference in `source`
3. if Penny's pipeline already files action items or notes somewhere (a doc, a tracker), pass the outcome as `filing_verdict` (e.g. `"filed"`, `"not_filed"`) and the exact locations as `filed_destinations`
4. expose the generated HTML through a session-checked route that checks `store.is_available(report_id)`
5. pass that URL as `report_url`
6. retain the canonical JSON in Penny's `$HERMES_HOME/meeting_reports/`

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
- the underlying kanban task(s) for a "Process" run are created with `notify_mode="silent"` (or `"inherit"` for children), and completing/blocking/crashing one produces no notifier delivery
- an ordinary (non-meeting) kanban task on the same board still notifies exactly as before
- one report-ready event produces one compact card in the originating thread, carrying the filing verdict and exact filed destinations
- Slack and Telegram accept/reject actions persist a verdict
- both platforms collect notes through their native input flow
- duplicate and conflicting clicks leave the first terminal verdict unchanged
- Dismiss hides the card without changing review state
- no raw transcript, JSON, or child artifact is sent by default
- no start acknowledgment, task id, or parent/child completion ping reaches the originating chat
- expired reports fail availability checks
- cleanup removes JSON and HTML and revokes the published URL

Run the focused suite:

```bash
scripts/run_tests.sh tests/plugins/test_meeting_reports_*.py
```
