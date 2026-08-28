"""Canonical, source-agnostic meeting report schema."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 24 * 60 * 60
REPORT_ID_MAX_CHARS = 36
_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$")
REVIEW_STATUSES = frozenset({
    "pending",
    "accepted",
    "accepted_with_notes",
    "rejected",
    "rejected_with_notes",
})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, *, fallback: Optional[datetime] = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    elif fallback is not None:
        parsed = fallback
    else:
        raise ValueError("datetime value is required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_required(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _clean_report_url(value: Any) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if len(text) > 2048 or any(character.isspace() for character in text):
        raise ValueError(
            "report_url must not contain whitespace and must be <= 2048 chars"
        )
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("report_url must be an absolute http(s) URL")
    return text


@dataclass
class ActionItem:
    text: str
    owner: Optional[str] = None
    due: Optional[str] = None

    def __post_init__(self) -> None:
        self.text = _clean_required(self.text, "action item text")
        self.owner = str(self.owner).strip() if self.owner else None
        self.due = str(self.due).strip() if self.due else None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "owner": self.owner, "due": self.due}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionItem":
        return cls(
            text=value.get("text", ""), owner=value.get("owner"), due=value.get("due")
        )


@dataclass
class ProposedDelegation:
    """A reviewable suggestion only. It is never an executable dispatch record."""

    goal: str
    target_agent: Optional[str] = None
    rationale: Optional[str] = None
    toolsets: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.goal = _clean_required(self.goal, "proposed delegation goal")
        self.target_agent = (
            str(self.target_agent).strip() if self.target_agent else None
        )
        self.rationale = str(self.rationale).strip() if self.rationale else None
        self.toolsets = [str(x).strip() for x in self.toolsets if str(x).strip()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "target_agent": self.target_agent,
            "rationale": self.rationale,
            "toolsets": list(self.toolsets),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProposedDelegation":
        return cls(
            goal=value.get("goal", ""),
            target_agent=value.get("target_agent"),
            rationale=value.get("rationale"),
            toolsets=list(value.get("toolsets") or []),
        )


@dataclass
class ReviewState:
    status: str = "pending"
    notes: Optional[str] = None
    actor: Optional[str] = None
    reviewed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.status = str(self.status).strip()
        if self.status not in REVIEW_STATUSES:
            raise ValueError(f"invalid review status: {self.status}")
        self.notes = str(self.notes).strip() if self.notes else None
        self.actor = str(self.actor).strip() if self.actor else None
        if self.reviewed_at is not None:
            self.reviewed_at = _parse_datetime(self.reviewed_at)

    @property
    def terminal(self) -> bool:
        return self.status != "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "notes": self.notes,
            "actor": self.actor,
            "reviewed_at": _iso(self.reviewed_at) if self.reviewed_at else None,
        }

    @classmethod
    def from_dict(cls, value: Optional[Mapping[str, Any]]) -> "ReviewState":
        value = value or {}
        reviewed_at = value.get("reviewed_at")
        return cls(
            status=value.get("status", "pending"),
            notes=value.get("notes"),
            actor=value.get("actor"),
            reviewed_at=_parse_datetime(reviewed_at) if reviewed_at else None,
        )


@dataclass
class MeetingReport:
    report_id: str
    title: str
    summary: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    participants: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    proposed_delegations: list[ProposedDelegation] = field(default_factory=list)
    confidence: Optional[str] = None
    confidence_notes: Optional[str] = None
    # Filing verdict: free-text status of whether/how this report's content
    # was filed into a destination system (e.g. "filed", "partially_filed",
    # "not_filed"). None when the source pipeline doesn't file anywhere.
    filing_verdict: Optional[str] = None
    # Exact destinations the report's content was filed to, e.g.
    # "Notion: Meeting Notes / Q3 Planning" or a destination URL. Plain
    # strings, same shape as ``decisions``. This is a record of what
    # already happened, never a dispatch instruction.
    filed_destinations: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    review: ReviewState = field(default_factory=ReviewState)
    report_url: Optional[str] = None
    report_html_path: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.report_id = str(self.report_id or "").strip()
        if not _REPORT_ID_RE.fullmatch(self.report_id):
            raise ValueError(
                "report_id must be 1-36 URL-safe characters and start with a letter or digit"
            )
        self.title = _clean_required(self.title, "title")
        self.summary = str(self.summary or "").strip()
        self.source = dict(self.source or {})
        self.participants = [
            str(x).strip() for x in self.participants if str(x).strip()
        ]
        self.decisions = [str(x).strip() for x in self.decisions if str(x).strip()]
        self.action_items = [
            x if isinstance(x, ActionItem) else ActionItem.from_dict(x)
            for x in self.action_items
        ]
        self.proposed_delegations = [
            x if isinstance(x, ProposedDelegation) else ProposedDelegation.from_dict(x)
            for x in self.proposed_delegations
        ]
        self.created_at = _parse_datetime(self.created_at)
        self.ttl_seconds = int(self.ttl_seconds)
        if self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not isinstance(self.review, ReviewState):
            self.review = ReviewState.from_dict(self.review)
        self.report_url = _clean_report_url(self.report_url)
        self.confidence = str(self.confidence).strip() if self.confidence else None
        self.confidence_notes = (
            str(self.confidence_notes).strip() if self.confidence_notes else None
        )
        self.filing_verdict = (
            str(self.filing_verdict).strip() if self.filing_verdict else None
        )
        self.filed_destinations = [
            str(x).strip() for x in self.filed_destinations if str(x).strip()
        ]
        self.report_html_path = (
            str(self.report_html_path) if self.report_html_path else None
        )
        self.schema_version = int(self.schema_version)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported meeting report schema version: {self.schema_version}"
            )

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.ttl_seconds)

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        current = _parse_datetime(now or utc_now())
        return current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "title": self.title,
            "source": dict(self.source),
            "participants": list(self.participants),
            "summary": self.summary,
            "decisions": list(self.decisions),
            "action_items": [item.to_dict() for item in self.action_items],
            "proposed_delegations": [
                item.to_dict() for item in self.proposed_delegations
            ],
            "confidence": self.confidence,
            "confidence_notes": self.confidence_notes,
            "filing_verdict": self.filing_verdict,
            "filed_destinations": list(self.filed_destinations),
            "created_at": _iso(self.created_at),
            "ttl_seconds": self.ttl_seconds,
            "expires_at": _iso(self.expires_at),
            "review": self.review.to_dict(),
            "report_url": self.report_url,
            "report_html_path": self.report_html_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MeetingReport":
        schema_version = int(value.get("schema_version", SCHEMA_VERSION))
        if schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported meeting report schema version: {schema_version}"
            )
        return cls(
            schema_version=schema_version,
            report_id=value.get("report_id", ""),
            title=value.get("title", ""),
            source=dict(value.get("source") or {}),
            participants=list(value.get("participants") or []),
            summary=value.get("summary", ""),
            decisions=list(value.get("decisions") or []),
            action_items=[
                ActionItem.from_dict(x) for x in value.get("action_items") or []
            ],
            proposed_delegations=[
                ProposedDelegation.from_dict(x)
                for x in value.get("proposed_delegations") or []
            ],
            confidence=value.get("confidence"),
            confidence_notes=value.get("confidence_notes"),
            filing_verdict=value.get("filing_verdict"),
            filed_destinations=list(value.get("filed_destinations") or []),
            created_at=_parse_datetime(value.get("created_at"), fallback=utc_now()),
            ttl_seconds=int(value.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
            review=ReviewState.from_dict(value.get("review")),
            report_url=value.get("report_url"),
            report_html_path=value.get("report_html_path"),
        )


def coerce_action_items(values: Optional[Sequence[Any]]) -> list[ActionItem]:
    return [
        x if isinstance(x, ActionItem) else ActionItem.from_dict(x)
        for x in values or []
    ]


def coerce_delegations(values: Optional[Sequence[Any]]) -> list[ProposedDelegation]:
    return [
        x if isinstance(x, ProposedDelegation) else ProposedDelegation.from_dict(x)
        for x in values or []
    ]
