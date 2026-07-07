import json

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from tools import kanban_tools


def test_create_task_persists_provider_and_model_override(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="review hopewell-dev change",
            assignee="wren",
            provider_override="deepseek",
            model_override="deepseek-v4-pro",
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.provider_override == "deepseek"
    assert task.model_override == "deepseek-v4-pro"


def test_hopewell_dev_task_creates_dependent_default_model_review_card(tmp_path, monkeypatch):
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
    assert review.provider_override is None
    assert review.model_override is None
    assert review.skills == ["github-code-review"]
    assert review.max_retries == 1
    assert "default model" in (review.body or "")
    assert "gemini" not in (review.body or "").lower()

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
            "--provider",
            "deepseek",
            "--model",
            "deepseek-v4-pro",
            "--json",
        ]
    )

    exit_code = kanban_cli.kanban_command(args)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider_override"] == "deepseek"
    assert payload["model_override"] == "deepseek-v4-pro"


def test_kanban_tool_create_accepts_model_override(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    raw = kanban_tools._handle_create(
        {
            "title": "review hopewell-dev change",
            "assignee": "wren",
            "provider_override": "deepseek",
            "model_override": "deepseek-v4-pro",
        }
    )
    result = json.loads(raw)

    assert result.get("ok") is True, result
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, result["task_id"])
    assert task is not None
    assert task.provider_override == "deepseek"
    assert task.model_override == "deepseek-v4-pro"


def test_default_model_review_gate_failure_does_not_create_model_fallback(tmp_path, monkeypatch):
    """Default-model review-gate cards block on failure instead of silently
    switching to a different pinned review model."""
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))

    with kb.connect_closing() as conn:
        # Create implementation + auto-review card. The review uses the
        # assignee profile's default model by default.
        impl_id = kb.create_task(
            conn,
            title="implement demo feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        children = kb.child_ids(conn, impl_id)
        assert len(children) == 1
        review = kb.get_task(conn, children[0])
        assert review is not None
        assert review.model_override is None
        assert review.created_by == "hopewell-dev-review-gate"

        # Complete the parent so the review card can be promoted to
        # ready (dependencies must be satisfied for the dispatch path).
        kb.complete_task(conn, impl_id, result="done")
        kb.recompute_ready(conn)  # promotes review card to ready

        # Now that it's ready, simulate the review card hitting the
        # circuit breaker (max_retries=1 means first failure trips it).
        blocked = kb._record_task_failure(
            conn,
            review.id,
            error="model unavailable",
            outcome="crash",
            release_claim=True,
            end_run=True,
        )
        assert blocked  # breaker should trip

        # The review card should now be blocked and no fallback card should
        # have been created because no explicit review model/fallback is set.
        blocked_review = kb.get_task(conn, review.id)
        assert blocked_review.status == "blocked"
        assert kb.child_ids(conn, impl_id) == [review.id]


def test_pinned_review_gate_failure_creates_configured_fallback(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_PROVIDER", "review-provider")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_MODEL", "review-primary")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_FALLBACK_PROVIDER", "fallback-provider")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_FALLBACK_MODEL", "review-fallback")

    with kb.connect_closing() as conn:
        impl_id = kb.create_task(
            conn,
            title="implement demo feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        children = kb.child_ids(conn, impl_id)
        assert len(children) == 1
        review = kb.get_task(conn, children[0])
        assert review is not None
        assert review.provider_override == "review-provider"
        assert review.model_override == "review-primary"

        kb.complete_task(conn, impl_id, result="done")
        kb.recompute_ready(conn)
        blocked = kb._record_task_failure(
            conn,
            review.id,
            error="model unavailable",
            outcome="crash",
            release_claim=True,
            end_run=True,
        )
        assert blocked

        all_children = kb.child_ids(conn, impl_id)
        assert len(all_children) == 2
        fallback = next(kb.get_task(conn, cid) for cid in all_children if cid != review.id)
        assert fallback is not None
        assert fallback.created_by == "hopewell-dev-review-gate"
        assert review.provider_override == "review-provider"
        assert fallback.provider_override == "fallback-provider"
        assert fallback.model_override == "review-fallback"
        assert fallback.skills == ["github-code-review"]
        assert kb.parent_ids(conn, fallback.id) == [impl_id]


def test_non_review_gate_card_does_not_create_fallback(
    tmp_path, monkeypatch
):
    """Only review-gate cards created by the review gate can trigger a
    configured fallback; normal cards do not."""
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
        # No fallback because created_by != REVIEW_GATE_CREATED_BY
        assert kb.child_ids(conn, task_id) == []


def test_review_required_block_creates_ready_closed_loop_review_card(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="implement closed loop feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
            auto_review_gate=False,
        )
        assert kb.block_task(conn, source_id, reason="review-required: branch pushed")
        source = kb.get_task(conn, source_id)
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert source is not None
    assert source.status == "blocked"
    assert len(reviews) == 1
    review = reviews[0]
    assert review.status == "ready"
    assert review.assignee == "code-reviewer"
    assert review.workspace_path == str(repo)
    assert review.idempotency_key == f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:1"
    assert f"Source task: {source_id}" in (review.body or "")


def test_review_loop_approval_unblocks_source_task(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="implement approved feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
            auto_review_gate=False,
        )
        assert kb.block_task(conn, source_id, reason="review-required: ready")
        review = next(
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        )
        assert kb.complete_task(
            conn,
            review.id,
            summary="PASS: looks good",
            metadata={"verdict": "approved"},
        )
        source = kb.get_task(conn, source_id)
        comments = kb.list_comments(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "ready"
    assert any("approved" in comment.body for comment in comments)
    assert "review_approved" in event_kinds


def test_review_loop_allows_second_round_before_loop_breaker(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_MAX_ROUNDS", "2")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="implement changes feature",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
            auto_review_gate=False,
        )
        assert kb.block_task(conn, source_id, reason="review-required: round one")
        review1 = next(
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        )
        assert kb.complete_task(
            conn,
            review1.id,
            summary="BLOCK: fix this",
            metadata={"verdict": "changes_requested"},
        )
        assert kb.get_task(conn, source_id).status == "ready"
        assert kb.block_task(conn, source_id, reason="review-required: round two")
        source = kb.get_task(conn, source_id)
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert source is not None
    assert source.status == "blocked"
    assert len(reviews) == 2
    assert sorted(review.idempotency_key for review in reviews) == [
        f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:1",
        f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:2",
    ]
