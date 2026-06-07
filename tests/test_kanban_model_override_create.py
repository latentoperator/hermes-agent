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


def test_opus_review_gate_failure_creates_gemini_fallback(tmp_path, monkeypatch):
    """When the circuit breaker trips on an Opus review-gate card,
    a Gemini fallback review card is automatically created."""
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))

    with kb.connect_closing() as conn:
        # Create implementation + auto-review (Opus) card
        impl_id = kb.create_task(
            conn,
            title="implement demo feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        children = kb.child_ids(conn, impl_id)
        assert len(children) == 1
        opus_review = kb.get_task(conn, children[0])
        assert opus_review.model_override == "anthropic/claude-opus-4.8"
        assert opus_review.created_by == "hopewell-dev-review-gate"

        # Complete the parent so the review card can be promoted to
        # ready (dependencies must be satisfied for the dispatch path).
        kb.complete_task(conn, impl_id, result="done")
        kb.recompute_ready(conn)  # promotes review card to ready

        # Now that it's ready, simulate the Opus card hitting the
        # circuit breaker (max_retries=1 means first failure trips it).
        blocked = kb._record_task_failure(
            conn,
            opus_review.id,
            error="model unavailable",
            outcome="crash",
            release_claim=True,
            end_run=True,
        )
        assert blocked  # breaker should trip

        # The Opus card should now be blocked
        opus = kb.get_task(conn, opus_review.id)
        assert opus.status == "blocked"

        # A Gemini fallback card should have been created
        all_children = kb.child_ids(conn, impl_id)
        # There should be 2 children: Opus (blocked) + Gemini (ready/todo)
        assert len(all_children) == 2

        gemini_card = None
        for cid in all_children:
            if cid != opus_review.id:
                gemini_card = kb.get_task(conn, cid)
                break

        assert gemini_card is not None
        assert gemini_card.created_by == "hopewell-dev-review-gate"
        assert gemini_card.model_override == "google/gemini-3-pro-preview"
        assert gemini_card.skills == ["github-code-review"]
        assert gemini_card.workspace_path == str(repo)
        assert gemini_card.workspace_kind == "dir"
        # It should be a child of the implementation card
        assert kb.parent_ids(conn, gemini_card.id) == [impl_id]


def test_non_review_gate_card_does_not_create_gemini_fallback(
    tmp_path, monkeypatch
):
    """Only review-gate cards (created_by='hopewell-dev-review-gate')
    trigger Gemini fallback; normal cards do not."""
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review: some PR",
            assignee="wren",
            model_override="anthropic/claude-opus-4.8",
            skills=["github-code-review"],
            max_retries=1,
        )
        # Confirm no children before failure
        assert kb.child_ids(conn, task_id) == []

        blocked = kb._record_task_failure(
            conn,
            task_id,
            error="model unavailable",
            outcome="crash",
            release_claim=True,
            end_run=True,
        )
        assert blocked
        # No Gemini fallback because created_by != REVIEW_GATE_CREATED_BY
        assert kb.child_ids(conn, task_id) == []
