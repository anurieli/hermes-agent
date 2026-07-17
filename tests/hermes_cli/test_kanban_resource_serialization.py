"""Regression coverage for repository/deployment serialization."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_file_task import main as file_task


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


def test_same_deployment_target_is_serialized_without_failure(board, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    first = kb.create_task(
        board,
        title="deploy one",
        assignee="worker",
        deployment_target="olympus:hermes-gateway-penny",
    )
    second = kb.create_task(
        board,
        title="deploy two",
        assignee="worker",
        deployment_target="olympus:hermes-gateway-penny",
    )

    result = kb.dispatch_once(board, spawn_fn=lambda _task, _workspace: 999_001)

    assert [item[0] for item in result.spawned] == [first]
    assert result.skipped_resource_locked == [(second, first)]
    blocked = kb.get_task(board, second)
    assert blocked.status == "ready"
    assert blocked.consecutive_failures == 0


def test_shared_dir_is_serialized_but_distinct_dirs_are_parallel(board, tmp_path):
    shared = tmp_path / "repo"
    other = tmp_path / "other"
    first = kb.create_task(
        board, title="edit shared one", assignee="worker",
        workspace_kind="dir", workspace_path=str(shared),
    )
    second = kb.create_task(
        board, title="edit shared two", assignee="worker",
        workspace_kind="dir", workspace_path=str(shared / ".." / "repo"),
    )
    third = kb.create_task(
        board, title="edit other", assignee="worker",
        workspace_kind="dir", workspace_path=str(other),
    )

    assert kb.claim_task(board, first) is not None
    assert kb.claim_task(board, second) is None
    assert kb.find_resource_conflict(board, second) == first
    assert kb.claim_task(board, third) is not None


def test_isolated_worktrees_for_same_repo_can_run_in_parallel(board, tmp_path):
    first = kb.create_task(
        board, title="branch one", assignee="worker",
        workspace_kind="worktree", workspace_path=str(tmp_path / "repo" / ".worktrees" / "one"),
    )
    second = kb.create_task(
        board, title="branch two", assignee="worker",
        workspace_kind="worktree", workspace_path=str(tmp_path / "repo" / ".worktrees" / "two"),
    )

    assert kb.claim_task(board, first) is not None
    assert kb.claim_task(board, second) is not None


def test_deployment_target_migrates_existing_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(tasks)")}
    assert "deployment_target" in columns
    assert "idx_tasks_deployment_target" in indexes


def test_filing_helper_is_idempotent_and_records_resources(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    argv = [
        "--title", "deploy", "--assignee", "worker",
        "--repo", str(repo),
        "--deployment-target", "olympus:gateway",
        "--idempotency-key", "deploy-42",
    ]
    assert file_task(argv) == 0
    first = capsys.readouterr().out
    assert file_task(argv) == 0
    second = capsys.readouterr().out

    import json
    first_data = json.loads(first)
    second_data = json.loads(second)
    assert first_data["task_id"] == second_data["task_id"]
    assert first_data["workspace_path"] == str(repo.resolve())
    assert first_data["deployment_target"] == "olympus:gateway"
