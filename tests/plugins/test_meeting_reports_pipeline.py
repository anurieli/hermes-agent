"""pipeline.generate_report / deliver_completion_card - one card, no dumps."""

from __future__ import annotations

import asyncio

from plugins.meeting_reports.pipeline import deliver_completion_card, generate_report
from plugins.meeting_reports.store import MeetingReportStore


class _FakeSendResult:
    def __init__(self, success=True):
        self.success = success


class _NativeCardAdapter:
    """Implements send_meeting_report_card - the preferred delivery path."""

    def __init__(self):
        self.calls = []

    async def send_meeting_report_card(
        self,
        *,
        chat_id,
        report_id,
        title,
        body,
        buttons,
        report_url=None,
        metadata=None,
    ):
        self.calls.append({
            "chat_id": chat_id,
            "report_id": report_id,
            "title": title,
            "body": body,
            "buttons": buttons,
            "report_url": report_url,
            "metadata": metadata,
        })
        return _FakeSendResult()

    async def send(
        self, chat_id, text, metadata=None
    ):  # pragma: no cover - must not be used
        raise AssertionError(
            "send() should not be called when send_meeting_report_card exists"
        )


class _PlainSendAdapter:
    """No native card support - must degrade to ONE plain-text send()."""

    def __init__(self):
        self.calls = []

    async def send(self, chat_id, text, metadata=None):
        self.calls.append({"chat_id": chat_id, "text": text})
        return _FakeSendResult()


def test_generate_report_persists_and_renders(tmp_path):
    store = MeetingReportStore(tmp_path / "meeting_reports")
    report = generate_report(
        title="Weekly Sync",
        summary="Shipped the migration doc.",
        source={"kind": "manual"},
        decisions=["Ship Friday"],
        action_items=[{"text": "Write release notes", "owner": "Ada"}],
        proposed_delegations=[{"goal": "Draft release notes", "target_agent": "penny"}],
        store=store,
    )

    assert report.report_id
    assert store.load(report.report_id) is not None
    assert report.report_html_path is not None


def test_deliver_completion_card_uses_native_adapter_method(tmp_path):
    store = MeetingReportStore(tmp_path / "meeting_reports")
    report = generate_report(
        title="Weekly Sync",
        summary="s",
        report_url="https://example.test/reports/mtgrpt-1",
        store=store,
    )
    adapter = _NativeCardAdapter()

    asyncio.run(
        deliver_completion_card(
            adapter,
            "chat-1",
            report,
            metadata={"thread_id": "origin-thread"},
        )
    )

    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["chat_id"] == "chat-1"
    assert call["report_id"] == report.report_id
    assert call["report_url"] == "https://example.test/reports/mtgrpt-1"
    assert call["metadata"] == {"thread_id": "origin-thread"}


def test_deliver_completion_card_degrades_to_plain_text_not_attachment_dump(tmp_path):
    store = MeetingReportStore(tmp_path / "meeting_reports")
    report = generate_report(
        title="Weekly Sync",
        summary="s",
        report_url="https://example.test/reports/plain",
        store=store,
    )
    adapter = _PlainSendAdapter()

    asyncio.run(deliver_completion_card(adapter, "chat-1", report))

    assert len(adapter.calls) == 1
    text = adapter.calls[0]["text"]
    # The compact card text, not the raw canonical JSON / rendered HTML.
    assert "<html" not in text.lower()
    assert '"report_id"' not in text
    assert "https://example.test/reports/plain" in text
    assert len(text) < 600


def test_one_delivery_invocation_sends_one_card(tmp_path):
    store = MeetingReportStore(tmp_path / "meeting_reports")
    report = generate_report(title="Weekly Sync", summary="s", store=store)
    adapter = _NativeCardAdapter()

    asyncio.run(deliver_completion_card(adapter, "chat-1", report))
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["report_id"] == report.report_id
