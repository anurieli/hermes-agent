# Portable meeting reports

`meeting_reports` is a source-agnostic plugin kit for turning an existing meeting summary into one reviewable completion report. It deliberately does not ingest recordings, fetch transcripts, summarize meetings, or dispatch proposed work. Existing meeting-source plugins keep those responsibilities.

Enable it in the profile that owns meeting processing, then restart that profile's gateway:

```bash
hermes plugins enable meeting_reports
```

## Contract

The source pipeline owns ingestion and archive persistence. At the plugin edge it calls `generate_report(...)` with normalized fields:

```python
from plugins.meeting_reports import PipelineEventLog, generate_report
from plugins.meeting_reports.pipeline import new_report_id, route_pipeline_event

log = PipelineEventLog()
log.stage("fetching_transcript")
log.stage("summarizing")
report_id = new_report_id()
report_url = publisher.url_for(report_id, ttl_seconds=86_400)

report = generate_report(
    report_id=report_id,
    title=summary.title or "Meeting",
    summary=summary.summary or "",
    source={"kind": "teams", "reference": summary.meeting_ref.meeting_id},
    participants=summary.participants,
    decisions=summary.key_decisions,
    action_items=[{"text": item} for item in summary.action_items],
    proposed_delegations=[
        {
            "goal": suggestion.goal,
            "target_agent": suggestion.target_agent,
            "rationale": suggestion.rationale,
            "toolsets": suggestion.toolsets,
        }
        for suggestion in suggestions
    ],
    # Production integrations should expose the generated HTML through a
    # session-checked route that enforces the same 24-hour TTL.
    report_url=report_url,
    store=report_store,
    events=log,
)

for event in log.events:
    await route_pipeline_event(
        event,
        adapter=origin_adapter,
        chat_id=origin_chat_id,
        store=report_store,
        metadata=origin_metadata,
    )
```

`generate_report` writes canonical JSON and a self-contained HTML report under `$HERMES_HOME/meeting_reports/`. It emits exactly one visible `meeting:report_ready` event. Stage and subagent events are silent by default, and `route_pipeline_event` ignores every event except `meeting:report_ready`.

`run_silent_fanout(...)` concurrently awaits source-selected processing workers or subagents and records only silent lifecycle events. It accepts coroutines supplied by the existing source pipeline. It never reads or dispatches the report's `proposed_delegations`.

## Canonical schema

The JSON report includes:

- source metadata and participants
- summary and decisions
- owned action items
- proposed delegations
- confidence and optional confidence notes
- review state
- creation time, expiry time, and TTL
- optional externally published `report_url`
- local rendered HTML path

`proposed_delegations` are suggestions only. They contain no task id, dispatched flag, or execution status. Accepting a report changes only its review state. No review path imports or calls `delegate_task`.

## Completion card

Telegram and Slack expose `send_meeting_report_card(...)`. The card is compact and contains:

- one report link when a TTL-bound `report_url` is supplied
- summary plus counts, not raw JSON or attachment dumps
- Accept
- Accept with notes
- Reject
- Reject with notes
- Dismiss

Slack collects notes in a modal. Telegram sends a bounded ForceReply prompt and consumes only the reply to that prompt. Review transitions are idempotent: after the first terminal decision, later clicks cannot change the stored verdict.

## TTL and cleanup

The default TTL is 86,400 seconds. Availability checks fail after expiry. Cleanup removes both canonical JSON and rendered HTML:

```bash
hermes meeting-report cleanup
```

Run cleanup from the owning profile's environment. Example:

```bash
HERMES_HOME="$HOME/.hermes/profiles/penny" hermes meeting-report cleanup
```

The preferred `report_url` is a session-checked route that reads `<report_id>.html` only while `store.is_available(report_id)` is true. Snapshot publishers must also refresh the HTML after review and revoke or delete the URL when local cleanup runs. The local store cannot delete remote artifacts it does not own.

## Operator commands

```bash
hermes meeting-report list
hermes meeting-report show <report-id>
hermes meeting-report render <report-id>
hermes meeting-report review <report-id> accept
hermes meeting-report review <report-id> accept_with_notes --notes "Looks good"
hermes meeting-report review <report-id> reject
hermes meeting-report review <report-id> reject_with_notes --notes "Needs correction"
hermes meeting-report cleanup
```

## Tests

```bash
scripts/run_tests.sh tests/plugins/test_meeting_reports_*.py
```
