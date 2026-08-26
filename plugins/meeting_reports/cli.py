"""Operator CLI for the portable meeting-processing kit (``hermes meeting-report``)."""

from __future__ import annotations

import argparse
import json

from plugins.meeting_reports.review import REVIEW_ACTIONS
from plugins.meeting_reports.pipeline import review_report
from plugins.meeting_reports.store import (
    MeetingReportStore,
    default_reports_dir,
    get_default_store,
)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="meeting_report_action")

    list_p = subs.add_parser("list", aliases=["ls"], help="List stored meeting reports")
    list_p.add_argument("--include-expired", action="store_true")

    show_p = subs.add_parser("show", help="Show a stored meeting report as JSON")
    show_p.add_argument("report_id")

    render_p = subs.add_parser(
        "render", help="Print the path to a report's rendered HTML"
    )
    render_p.add_argument("report_id")

    review_p = subs.add_parser("review", help="Apply a review decision to a report")
    review_p.add_argument("report_id")
    review_p.add_argument("action", choices=REVIEW_ACTIONS)
    review_p.add_argument("--actor", default=None)
    review_p.add_argument("--notes", default=None)

    cleanup_p = subs.add_parser("cleanup", help="Delete every report past its 24h TTL")

    subparser.set_defaults(func=meeting_report_command)


def meeting_report_command(args: argparse.Namespace) -> int:
    action = getattr(args, "meeting_report_action", None)
    if not action:
        print("Usage: hermes meeting-report {list|show|render|review|cleanup}")
        return 2

    store = get_default_store()
    if action in {"list", "ls"}:
        _cmd_list(store, args)
    elif action == "show":
        _cmd_show(store, args)
    elif action == "render":
        _cmd_render(store, args)
    elif action == "review":
        _cmd_review(store, args)
    elif action == "cleanup":
        _cmd_cleanup(store)
    else:
        print(f"Unknown meeting-report action: {action}")
        return 2
    return 0


def _cmd_list(store: MeetingReportStore, args: argparse.Namespace) -> None:
    reports = store.list_reports(
        include_expired=bool(getattr(args, "include_expired", False))
    )
    if not reports:
        print(f"No meeting reports found in {default_reports_dir()}.")
        return
    print(f"\n{len(reports)} meeting report(s):\n")
    for report in reports:
        print(f"  ◆ {report.report_id}")
        print(f"    title: {report.title}")
        print(f"    review: {report.review.status}")
        print(f"    expires: {report.expires_at.isoformat().replace('+00:00', 'Z')}")
        print()


def _cmd_show(store: MeetingReportStore, args: argparse.Namespace) -> None:
    report = store.load(args.report_id)
    if report is None or not store.is_available(args.report_id):
        print(f"Unknown or expired report: {args.report_id}")
        return
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))


def _cmd_render(store: MeetingReportStore, args: argparse.Namespace) -> None:
    report = store.load(args.report_id)
    if report is None or not store.is_available(args.report_id):
        print(f"Unknown or expired report: {args.report_id}")
        return
    store.save(report, render=True)
    print(report.report_html_path)


def _cmd_review(store: MeetingReportStore, args: argparse.Namespace) -> None:
    outcome = review_report(
        args.report_id, args.action, actor=args.actor, notes=args.notes, store=store
    )
    if outcome is None:
        print(f"Unknown or expired report: {args.report_id}")
        return
    verb = "applied" if outcome.changed else "already decided (idempotent no-op)"
    print(
        f"{args.report_id}: {outcome.previous_status} -> {outcome.report.review.status} ({verb})"
    )


def _cmd_cleanup(store: MeetingReportStore) -> None:
    deleted = store.cleanup_expired()
    if not deleted:
        print("Nothing to clean up.")
        return
    print(f"Deleted {len(deleted)} expired report(s):")
    for report_id in deleted:
        print(f"  - {report_id}")
