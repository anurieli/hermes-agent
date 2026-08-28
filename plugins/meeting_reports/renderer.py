"""Render a :class:`MeetingReport` to a self-contained HTML document.

The HTML is a single file - inline CSS, no external requests - so it keeps
working after the JSON that produced it has been cleaned up, right up until
the file itself is deleted by ``store.cleanup_expired()``.
"""

from __future__ import annotations

import html as _html
from typing import Any

from plugins.meeting_reports.models import MeetingReport

_REVIEW_LABELS = {
    "pending": ("Pending review", "#8a6d00", "#fff6e0"),
    "accepted": ("Accepted", "#146c2e", "#e6f6ea"),
    "accepted_with_notes": ("Accepted (with notes)", "#146c2e", "#e6f6ea"),
    "rejected": ("Rejected", "#8a1f11", "#fbe9e7"),
    "rejected_with_notes": ("Rejected (with notes)", "#8a1f11", "#fbe9e7"),
}


def _e(value: Any) -> str:
    return _html.escape(str(value or ""))


def _list_items(items: list[str]) -> str:
    if not items:
        return '<p class="muted">None recorded.</p>'
    return "<ul>" + "".join(f"<li>{_e(item)}</li>" for item in items) + "</ul>"


def _action_items_table(report: MeetingReport) -> str:
    if not report.action_items:
        return '<p class="muted">No action items.</p>'
    rows = []
    for item in report.action_items:
        rows.append(
            "<tr>"
            f"<td>{_e(item.text)}</td>"
            f"<td>{_e(item.owner or '-')}</td>"
            f"<td>{_e(item.due or '-')}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Action item</th><th>Owner</th><th>Due</th></tr>"
        f"</thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _filing_section(report: MeetingReport) -> str:
    verdict = report.filing_verdict or "not filed"
    if not report.filed_destinations:
        return f"<p>{_e(verdict)}</p>"
    destinations = "".join(f"<li>{_e(dest)}</li>" for dest in report.filed_destinations)
    return f"<p>{_e(verdict)}</p><ul>{destinations}</ul>"


def _delegations_table(report: MeetingReport) -> str:
    if not report.proposed_delegations:
        return '<p class="muted">No delegations proposed.</p>'
    rows = []
    for item in report.proposed_delegations:
        toolsets = ", ".join(item.toolsets) if item.toolsets else "-"
        rows.append(
            "<tr>"
            f"<td>{_e(item.goal)}</td>"
            f"<td>{_e(item.target_agent or '-')}</td>"
            f"<td>{_e(item.rationale or '-')}</td>"
            f"<td>{_e(toolsets)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Goal</th><th>Target</th><th>Rationale</th>"
        f"<th>Toolsets</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        '<p class="muted">Proposed only - nothing here has been dispatched. '
        "A delegation runs only after explicit approval, and approval alone "
        "never triggers dispatch either.</p>"
    )


def render_html(report: MeetingReport) -> str:
    """Return a polished, self-contained HTML document for ``report``."""
    label, fg, bg = _REVIEW_LABELS.get(report.review.status, _REVIEW_LABELS["pending"])
    participants = (
        ", ".join(report.participants) if report.participants else "Not recorded"
    )
    confidence = report.confidence or "unknown"
    source_kind = report.source.get("kind") if isinstance(report.source, dict) else None
    source_ref = (
        report.source.get("reference") if isinstance(report.source, dict) else None
    )

    review_notes_html = ""
    if report.review.notes:
        review_notes_html = f'<p class="review-notes">“{_e(report.review.notes)}”</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_e(report.title)} - Meeting Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 16px;
    background: #f5f6f8; color: #1c1e21;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
  }}
  main {{
    max-width: 760px; margin: 0 auto; background: #fff;
    border-radius: 14px; padding: 32px 36px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  h1 {{ margin: 0 0 4px; font-size: 24px; }}
  h2 {{ margin: 28px 0 8px; font-size: 15px; text-transform: uppercase; letter-spacing: 0.04em; color: #555; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
  .badge {{
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 600; color: {fg}; background: {bg};
  }}
  .ttl {{ font-size: 12px; color: #888; margin-top: 4px; }}
  p {{ margin: 8px 0; }}
  p.muted {{ color: #888; font-style: italic; }}
  ul {{ margin: 8px 0; padding-left: 22px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e6e6e6; vertical-align: top; }}
  th {{ color: #666; font-weight: 600; font-size: 12px; text-transform: uppercase; }}
  .review-notes {{ background: #f7f7f9; border-left: 3px solid #ccc; padding: 8px 12px; font-style: italic; }}
  footer {{ margin-top: 28px; font-size: 12px; color: #999; }}
</style>
</head>
<body>
<main>
  <h1>{_e(report.title)}</h1>
  <div class="meta">
    {_e(source_kind or "meeting")}{" · " + _e(source_ref) if source_ref else ""}<br>
    Participants: {_e(participants)}<br>
    Confidence: {_e(confidence)}{" - " + _e(report.confidence_notes) if report.confidence_notes else ""}
  </div>

  <span class="badge">{_e(label)}</span>
  {review_notes_html}
  <div class="ttl">Report expires {_e(report.expires_at.isoformat().replace("+00:00", "Z"))}</div>

  <h2>Summary</h2>
  <p>{_e(report.summary) or '<span class="muted">No summary available.</span>'}</p>

  <h2>Decisions</h2>
  {_list_items(report.decisions)}

  <h2>Filing</h2>
  {_filing_section(report)}

  <h2>Action items</h2>
  {_action_items_table(report)}

  <h2>Proposed delegations</h2>
  {_delegations_table(report)}

  <footer>Generated by the Hermes portable meeting-processing kit. report_id={_e(report.report_id)}</footer>
</main>
</body>
</html>
"""


__all__ = ["render_html"]
