"""Idempotent, metadata-complete Kanban task filing helper."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from hermes_cli import kanban_db as kb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-kanban-file-task",
        description=(
            "File one retry-safe Kanban task with explicit repository/project "
            "and deployment ownership metadata."
        ),
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--assignee", required=True)
    parser.add_argument("--body")
    parser.add_argument("--project", help="Hermes project id or slug")
    parser.add_argument(
        "--repo",
        help=(
            "Absolute shared repository path. Uses workspace_kind=dir and is "
            "serialized against other tasks in the same checkout. Prefer "
            "--project for isolated implementation worktrees."
        ),
    )
    parser.add_argument(
        "--deployment-target",
        help="Canonical live service/environment; equal targets are serialized",
    )
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--parent", action="append", default=[])
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--board")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.project and args.repo:
        raise SystemExit("choose --project or --repo, not both")
    workspace_kind = "dir" if args.repo else "scratch"
    workspace_path = None
    if args.repo:
        workspace_path = str(Path(args.repo).expanduser().resolve(strict=False))
        if not Path(workspace_path).is_absolute():
            raise SystemExit("--repo must resolve to an absolute path")
    with kb.connect_closing(board=args.board) as conn:
        task_id = kb.create_task(
            conn,
            title=args.title,
            body=args.body,
            assignee=args.assignee,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            project_id=args.project,
            deployment_target=args.deployment_target,
            idempotency_key=args.idempotency_key,
            parents=tuple(args.parent),
            priority=args.priority,
            board=args.board,
        )
        task = kb.get_task(conn, task_id)
    print(json.dumps({
        "task_id": task_id,
        "project_id": task.project_id if task else None,
        "workspace_kind": task.workspace_kind if task else None,
        "workspace_path": task.workspace_path if task else None,
        "deployment_target": task.deployment_target if task else None,
        "idempotency_key": task.idempotency_key if task else None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
