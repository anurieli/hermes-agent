"""Durable local storage for meeting reports.

Canonical JSON drives everything else, so the JSON is the file of record;
the rendered HTML is a derived artifact regenerated whenever the report
changes (e.g. a review transition). Both are profile-scoped under
``get_hermes_home() / "meeting_reports"`` and never under ``Path.home()``;
so each Hermes profile keeps its own reports (see AGENTS.md "State files").

TTL is enforced by :func:`cleanup_expired`, which deletes both the JSON and
the HTML for any report past ``expires_at``. Nothing here reaches back into
kanban, chat delivery, or dispatch. This module only persists and expires
bytes on disk.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

from hermes_constants import get_hermes_home
from plugins.meeting_reports.models import MeetingReport
from plugins.meeting_reports.renderer import render_html

DEFAULT_DIRNAME = "meeting_reports"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$")

# One process-wide lock. Report volume is low (meeting cadence, not
# request cadence) so a single lock is simpler than per-report locking and
# still never contends in practice.
_LOCK = threading.RLock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_reports_dir() -> Path:
    return get_hermes_home() / DEFAULT_DIRNAME


class InvalidReportIdError(ValueError):
    """Raised when a report_id isn't safe to use as a filename component."""


def _validate_report_id(report_id: str) -> str:
    report_id = str(report_id or "").strip()
    if not _ID_RE.match(report_id):
        raise InvalidReportIdError(f"Invalid report_id: {report_id!r}")
    return report_id


class MeetingReportStore:
    """File-backed store for :class:`MeetingReport` objects."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else default_reports_dir()

    def _json_path(self, report_id: str) -> Path:
        return self.root / f"{_validate_report_id(report_id)}.json"

    def _html_path(self, report_id: str) -> Path:
        return self.root / f"{_validate_report_id(report_id)}.html"

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Optional[Path] = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                delete=False,
                suffix=".tmp",
            ) as tmp:
                tmp.write(content)
                tmp.flush()
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @contextmanager
    def review_lock(self):
        """Serialize review read-modify-write cycles across processes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with (self.root / ".review.lock").open("a+", encoding="utf-8") as lock_file:
                try:
                    import fcntl
                except ImportError:  # pragma: no cover - Windows fallback
                    fcntl = None
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save(self, report: MeetingReport, *, render: bool = True) -> MeetingReport:
        """Persist derived HTML, then ``report``'s canonical JSON.

        Returns the same report with ``report_html_path`` populated when
        ``render`` is True.
        """
        with _LOCK:
            html_path: Optional[Path] = None
            if render:
                html_path = self._html_path(report.report_id)
                report.report_html_path = str(html_path)
            # Serialize before touching either artifact so invalid source
            # metadata cannot leave an orphaned HTML file.
            json_content = json.dumps(report.to_dict(), indent=2, sort_keys=True)
            if html_path is not None:
                self._atomic_write(html_path, render_html(report))
            self._atomic_write(self._json_path(report.report_id), json_content)
            return report

    def load(self, report_id: str) -> Optional[MeetingReport]:
        with _LOCK:
            try:
                path = self._json_path(report_id)
            except InvalidReportIdError:
                return None
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return MeetingReport.from_dict(payload)
            except (
                OSError,
                json.JSONDecodeError,
                ValueError,
                TypeError,
                AttributeError,
            ):
                return None

    def is_available(self, report_id: str, *, now: Optional[datetime] = None) -> bool:
        """True when the report exists on disk and has not expired."""
        report = self.load(report_id)
        if report is None:
            return False
        return not report.is_expired(now=now)

    def delete(self, report_id: str) -> bool:
        with _LOCK:
            deleted = False
            for path in (self._json_path(report_id), self._html_path(report_id)):
                try:
                    path.unlink()
                    deleted = True
                except FileNotFoundError:
                    pass
            return deleted

    def list_reports(self, *, include_expired: bool = True) -> list[MeetingReport]:
        with _LOCK:
            if not self.root.exists():
                return []
            reports: list[MeetingReport] = []
            for path in sorted(self.root.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    report = MeetingReport.from_dict(payload)
                except (
                    OSError,
                    json.JSONDecodeError,
                    ValueError,
                    TypeError,
                    AttributeError,
                ):
                    continue
                if include_expired or not report.is_expired():
                    reports.append(report)
            reports.sort(key=lambda r: r.created_at, reverse=True)
            return reports

    def cleanup_expired(self, *, now: Optional[datetime] = None) -> list[str]:
        """Delete every expired report's JSON + HTML. Returns deleted ids."""
        now = now or _utc_now()
        with _LOCK:
            deleted: list[str] = []
            for report in self.list_reports(include_expired=True):
                if report.is_expired(now=now):
                    self.delete(report.report_id)
                    deleted.append(report.report_id)
            return deleted


_DEFAULT_STORE: Optional[MeetingReportStore] = None
_DEFAULT_STORE_ROOT: Optional[Path] = None


def get_default_store() -> MeetingReportStore:
    """Return a process-wide default store, re-created if HERMES_HOME moves.

    Tests and multi-profile runs frequently change ``HERMES_HOME`` between
    calls (e.g. via a temp dir fixture); caching by root avoids handing back
    a store pointed at a stale profile directory.
    """
    global _DEFAULT_STORE, _DEFAULT_STORE_ROOT
    root = default_reports_dir()
    if _DEFAULT_STORE is None or _DEFAULT_STORE_ROOT != root:
        _DEFAULT_STORE = MeetingReportStore(root)
        _DEFAULT_STORE_ROOT = root
    return _DEFAULT_STORE


__all__ = [
    "DEFAULT_DIRNAME",
    "InvalidReportIdError",
    "MeetingReportStore",
    "default_reports_dir",
    "get_default_store",
]
