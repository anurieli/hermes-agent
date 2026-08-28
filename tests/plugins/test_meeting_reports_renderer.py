"""HTML rendering: self-contained document, no external requests, filing
verdict and exact destinations are present when set."""

from __future__ import annotations

from plugins.meeting_reports.models import MeetingReport
from plugins.meeting_reports.renderer import render_html


def _report(**overrides) -> MeetingReport:
    kwargs = dict(
        report_id="mtgrpt-render-1",
        title="Weekly Sync",
        summary="Shipped the migration doc.",
        decisions=["Ship Friday"],
    )
    kwargs.update(overrides)
    return MeetingReport(**kwargs)


def test_render_html_is_self_contained():
    html = render_html(_report())
    assert "<html" in html
    assert "http://" not in html
    assert "https://" not in html


def test_render_html_shows_filing_verdict_and_destinations():
    html = render_html(
        _report(
            filing_verdict="filed",
            filed_destinations=["Notion: Meeting Notes / Weekly Sync"],
        )
    )
    assert "Filing" in html
    assert "filed" in html
    assert "Notion: Meeting Notes / Weekly Sync" in html


def test_render_html_shows_not_filed_when_verdict_absent():
    html = render_html(_report())
    assert "not filed" in html


def test_render_html_escapes_destination_text():
    html = render_html(
        _report(filed_destinations=["<script>alert(1)</script>"])
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
