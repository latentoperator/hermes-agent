import json

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from tools import kanban_tools


def test_create_task_persists_model_override(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review hopewell-dev change",
            assignee="wren",
            model_override="claude-opus-4-8",
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.model_override == "claude-opus-4-8"


def test_hopewell_dev_task_creates_dependent_opus_review_card(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="implement demo feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        children = kb.child_ids(conn, task_id)
        assert len(children) == 1
        review = kb.get_task(conn, children[0])

    assert review is not None
    assert review.title == "review: implement demo feature"
    assert review.created_by == "hopewell-dev-review-gate"
    assert review.assignee == "wren"
    assert review.status == "todo"
    assert review.workspace_kind == "dir"
    assert review.workspace_path == str(repo)
    assert review.model_override == "anthropic/claude-opus-4.8"
    assert review.skills == ["github-code-review"]
    assert review.max_retries == 1
    assert "google/gemini-3-pro-preview" in (review.body or "")

    with kb.connect_closing() as conn:
        assert kb.parent_ids(conn, review.id) == [task_id]


def test_non_hopewell_dev_task_does_not_create_review_card(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    other_repo = tmp_path / "other" / "demo-repo"
    other_repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="implement external feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(other_repo),
        )
        assert kb.child_ids(conn, task_id) == []


def test_review_card_does_not_create_nested_review_card(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review: implement demo feature",
            assignee="wren",
            workspace_kind="dir",
            workspace_path=str(repo),
            skills=["github-code-review"],
            model_override="anthropic/claude-opus-4.8",
        )
        assert kb.child_ids(conn, task_id) == []


def test_kanban_cli_create_accepts_model_override(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    import argparse

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    args = parser.parse_args(
        [
            "kanban",
            "create",
            "review hopewell-dev change",
            "--assignee",
            "wren",
            "--model",
            "claude-opus-4-8",
            "--json",
        ]
    )

    exit_code = kanban_cli.kanban_command(args)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_override"] == "claude-opus-4-8"


def test_kanban_tool_create_accepts_model_override(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    raw = kanban_tools._handle_create(
        {
            "title": "review hopewell-dev change",
            "assignee": "wren",
            "model_override": "claude-opus-4-8",
        }
    )
    result = json.loads(raw)

    assert result.get("ok") is True, result
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, result["task_id"])
    assert task is not None
    assert task.model_override == "claude-opus-4-8"
