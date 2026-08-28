"""Silent-by-default internal event log for the meeting pipeline.

A meeting run goes through several internal stages (resolve meeting,
fetch transcript, summarize, run source-selected processing subagents,
render report). None of that belongs in the originating chat - only the
ONE report-ready event should ever produce user-visible output (see
``pipeline.deliver_completion_card``). This module gives the pipeline a
place to record every stage transition without any of them being able to
leak into chat by accident: an event is silent unless a caller explicitly
marks it ``silent=False``, and even then nothing here sends anything - it's
the pipeline's job to route the one non-silent event to
``deliver_completion_card``.

Kanban interop: this module's own bookkeeping (``PipelineEventLog``) is
entirely in-process and never touches the ``tasks``/``task_events`` tables,
so it cannot by itself silence a meeting run's underlying kanban work. If a
meeting run is dispatched as one or more real kanban tasks (a parent task,
worker/child tasks, or both), those tasks emit their OWN ``completed`` /
``blocked`` / ``crashed`` / ``timed_out`` events through the normal kanban
lifecycle regardless of what this module records. An ordinary child task
still notifies its subscribers by default. The only way to keep that traffic
out of chat is to create those kanban tasks with
``notify_mode="silent"`` (or ``"inherit"`` for children of a silent parent);
see ``hermes_cli.kanban_db.create_task`` / ``decompose_triage_task`` and
``gateway.kanban_watchers._is_notify_silent``, which is what actually gates
delivery. :func:`is_kanban_silent` only tells you whether a *kind string* is
one of kanban's own ``TERMINAL_KINDS``. It is useful for asserting this module's
``"meeting:*"`` kinds never collide with that set, not for suppressing a
dispatched task's real lifecycle events.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Mapping, Optional

# Every internal pipeline event kind this module emits. Kept out of any
# kanban TERMINAL_KINDS set by construction (see is_kanban_silent).
INTERNAL_KIND_PREFIX = "meeting:"

# The one kind that is allowed to be non-silent - a report finished
# rendering and is ready for a completion card.
REPORT_READY_KIND = "meeting:report_ready"


@dataclass(frozen=True)
class PipelineEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    silent: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.kind == REPORT_READY_KIND and self.silent:
            # Construction-time guard: this specific kind must never be
            # accidentally silenced, or the pipeline's one user-visible
            # event disappears.
            object.__setattr__(self, "silent", False)


class PipelineEventLog:
    """In-memory record of a single pipeline run's internal events."""

    def __init__(self) -> None:
        self._events: list[PipelineEvent] = []

    def record(
        self,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        silent: bool = True,
    ) -> PipelineEvent:
        event = PipelineEvent(kind=kind, payload=dict(payload or {}), silent=silent)
        self._events.append(event)
        return event

    def stage(self, name: str, **payload: Any) -> PipelineEvent:
        """Record a silent internal stage transition."""
        return self.record(f"{INTERNAL_KIND_PREFIX}{name}", payload, silent=True)

    def report_ready(self, report_id: str) -> PipelineEvent:
        """Record the ONE non-silent, report-ready event."""
        for event in self._events:
            if event.kind != REPORT_READY_KIND:
                continue
            if event.payload.get("report_id") != report_id:
                raise ValueError("one pipeline event log cannot publish two reports")
            return event
        return self.record(REPORT_READY_KIND, {"report_id": report_id}, silent=False)

    @property
    def events(self) -> list[PipelineEvent]:
        return list(self._events)

    @property
    def visible_events(self) -> list[PipelineEvent]:
        """Events that are allowed to reach the originating chat."""
        return [event for event in self._events if not event.silent]

    @property
    def silent_events(self) -> list[PipelineEvent]:
        return [event for event in self._events if event.silent]


def is_kanban_silent(kind: str) -> bool:
    """True when ``kind`` is NOT in the kanban notifier's TERMINAL_KINDS.

    Delegates to the real constant rather than re-declaring it, so this
    stays correct if the notifier's terminal-kind set ever changes.
    Fails safe (returns True - "treat as silent") if the gateway module
    isn't importable, e.g. in a minimal test environment.
    """
    try:
        from gateway.kanban_watchers import TERMINAL_KINDS
    except Exception:
        return True
    return kind not in TERMINAL_KINDS


async def run_silent_fanout(
    jobs: Mapping[str, Awaitable[Any]],
    *,
    events: Optional[PipelineEventLog] = None,
) -> dict[str, Any]:
    """Run independent processing jobs concurrently with silent events.

    Callers provide their own source-specific worker or subagent coroutines.
    This helper only coordinates processing. It never reads or dispatches the
    ``proposed_delegations`` recorded in a report.
    """
    events = events or PipelineEventLog()
    events.stage("fanout_started", jobs=list(jobs))

    async def _run(name: str, awaitable: Awaitable[Any]) -> tuple[str, bool, Any]:
        events.stage("stage_started", stage=name)
        try:
            result = await awaitable
        except Exception as exc:
            events.stage("stage_failed", stage=name, error_type=type(exc).__name__)
            return name, False, exc
        events.stage("stage_completed", stage=name)
        return name, True, result

    pairs = await asyncio.gather(*(_run(name, job) for name, job in jobs.items()))
    failures = [result for _name, ok, result in pairs if not ok]
    if failures:
        events.stage("fanout_failed", failure_count=len(failures))
        raise failures[0]
    events.stage("fanout_completed", jobs=list(jobs))
    return {name: result for name, _ok, result in pairs}


__all__ = [
    "INTERNAL_KIND_PREFIX",
    "REPORT_READY_KIND",
    "PipelineEvent",
    "PipelineEventLog",
    "is_kanban_silent",
    "run_silent_fanout",
]
