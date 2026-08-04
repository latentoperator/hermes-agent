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


def test_review_gate_card_gets_goal_mode_when_repo_has_review_contract(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    (repo / "REVIEW-RULES.md").write_text("Run the project review checklist.\n")
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
        review = kb.get_task(conn, kb.child_ids(conn, task_id)[0])

    assert review is not None
    assert review.created_by == kb.REVIEW_GATE_CREATED_BY
    assert review.goal_mode is True
    assert review.goal_max_turns == 3


def test_review_gate_card_stays_one_shot_without_review_contract(tmp_path, monkeypatch):
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
        review = kb.get_task(conn, kb.child_ids(conn, task_id)[0])

    assert review is not None
    assert review.created_by == kb.REVIEW_GATE_CREATED_BY
    assert review.goal_mode is False
    assert review.goal_max_turns is None


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


def test_review_loop_uses_dispatcher_profile_policy_for_any_source_profile(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / ".hermes"
    dante_home = root / "profiles" / "dante"
    wren_home = root / "profiles" / "wren"
    repo = tmp_path / "client-repos" / "demo"
    dante_home.mkdir(parents=True)
    wren_home.mkdir(parents=True)
    repo.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "kanban:\n"
        "  dispatcher_profile: wren\n"
        "  review_gate:\n"
        "    enabled: false\n"
        "    assignee: dante\n"
    )
    (dante_home / "config.yaml").write_text(
        "kanban:\n"
        "  review_gate:\n"
        "    enabled: false\n"
        "    closed_loop_enabled: false\n"
        "    assignee: dante\n"
    )
    (wren_home / "config.yaml").write_text(
        "kanban:\n"
        "  review_gate:\n"
        "    enabled: false\n"
        "    closed_loop_enabled: true\n"
        "    assignee: code-reviewer\n"
        "    skills: [github-code-review]\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(dante_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", raising=False)
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="Dante implementation using dispatcher review policy",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
            auto_review_gate=False,
        )
        assert kb.block_task(conn, source_id, reason="review-required: branch ready")
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert len(reviews) == 1
    assert reviews[0].assignee == "code-reviewer"
    assert reviews[0].skills == ["github-code-review"]


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
    assert review.workspace_kind == "dir"
    assert review.workspace_path == str(repo)
    assert review.branch_name is None
    assert review.idempotency_key == f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:1"
    body = review.body or ""
    assert f"Source task: {source_id}" in body
    assert f"Workspace: {repo}" in body
    assert f"Review source: {repo}" in body


def test_review_loop_reuses_existing_source_worktree_as_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    worktree = root / "demo-repo" / ".worktrees" / "source-task"
    worktree.mkdir(parents=True)
    branch = "dante/source-task-security-fix"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="repair security-sensitive refund behavior",
            assignee="dante",
            workspace_kind="worktree",
            workspace_path=str(worktree),
            branch_name=branch,
            auto_review_gate=False,
        )
        assert kb.block_task(conn, source_id, reason="review-required: branch ready")
        review = next(
            task
            for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        )

    # The source branch is already checked out here. A reviewer must inspect
    # that checkout directly instead of asking the dispatcher for a second
    # worktree on the same branch.
    assert review.workspace_kind == "dir"
    assert review.workspace_path == str(worktree)
    assert review.branch_name is None
    assert kb.resolve_workspace(review) == worktree
    body = review.body or ""
    assert f"Source task: {source_id}" in body
    assert f"Workspace: {worktree}" in body
    assert f"Review source: {worktree}" in body
    assert f"Branch: {branch}" in body


def test_review_required_dependency_block_stays_blocked_and_routes_review(tmp_path, monkeypatch):
    """The explicit review prefix must win over a mistaken dependency kind."""
    db_path = tmp_path / "kanban.db"
    repo = tmp_path / "shared-runtime"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="build safety-critical shared runtime consumer",
            assignee="wren",
            workspace_kind="dir",
            workspace_path=str(repo),
            auto_review_gate=False,
        )
        assert kb.block_task(
            conn,
            source_id,
            reason="review-required: implementation frozen for review",
            kind="dependency",
        )
        source = kb.get_task(conn, source_id)
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]
        promoted = kb.recompute_ready(conn)
        source_after_recompute = kb.get_task(conn, source_id)
        events = kb.list_events(conn, source_id)

    assert source is not None
    assert source.status == "blocked"
    assert source.block_kind == "needs_input"
    assert len(reviews) == 1
    assert reviews[0].status == "ready"
    assert promoted == 0
    assert source_after_recompute is not None
    assert source_after_recompute.status == "blocked"
    event_kinds = [event.kind for event in events]
    assert "review_loop_requested" in event_kinds
    assert "dependency_wait" not in event_kinds


def test_review_loop_card_gets_goal_mode_when_repo_has_review_contract(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    (repo / ".github").mkdir()
    (repo / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("Review checklist\n")
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
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert len(reviews) == 1
    review = reviews[0]
    assert review.goal_mode is True
    assert review.goal_max_turns == 3


def test_review_loop_card_stays_one_shot_without_review_contract(tmp_path, monkeypatch):
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
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert len(reviews) == 1
    review = reviews[0]
    assert review.goal_mode is False
    assert review.goal_max_turns is None


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


def test_review_loop_can_run_without_create_time_review_gate(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "demo-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="implementation should not get create-time review gate",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        after_create = kb.list_tasks(conn, include_archived=True)
        assert [task for task in after_create if task.created_by == kb.REVIEW_GATE_CREATED_BY] == []

        assert kb.block_task(conn, source_id, reason="review-required: PR ready")
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert len(reviews) == 1
    assert reviews[0].assignee == "code-reviewer"
    assert reviews[0].idempotency_key == f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:1"


def test_review_loop_uses_structured_review_source_for_scratch_card(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    root = tmp_path / "hopewell-dev"
    repo = root / "gathings-agent"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="Dante PR follow-up in scratch",
            assignee="dante",
            workspace_kind="scratch",
            auto_review_gate=False,
        )
        kb.add_comment(
            conn,
            source_id,
            "dante",
            "review-required handoff:\n```json\n"
            + json.dumps(
                {
                    "review_source": {
                        "repo_path": str(repo),
                        "branch": "dante/gathings-deploy-runner",
                        "head_sha": "abc123",
                        "pr_url": "https://github.com/latentoperator/gathings-agent/pull/92",
                        "changed_files": ["scripts/deploy.sh"],
                        "tests_run": ["bash -n scripts/deploy.sh"],
                    }
                }
            )
            + "\n```",
        )
        assert kb.block_task(conn, source_id, reason="review-required: PR ready")
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert len(reviews) == 1
    review = reviews[0]
    assert review.status == "ready"
    assert review.assignee == "code-reviewer"
    assert review.workspace_kind == "dir"
    assert review.workspace_path == str(repo)
    assert review.idempotency_key == f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:1"
    assert f"Source task: {source_id}" in (review.body or "")
    assert f"Review source: {repo}" in (review.body or "")
    assert "dante/gathings-deploy-runner" in (review.body or "")


def test_review_loop_routes_out_of_root_m365_scratch_handoff(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    allowed_root = tmp_path / "hopewell-dev"
    repo = tmp_path / ".hermes" / "openclaw-universe" / "mcp" / "m365" / "ms-365-mcp-server"
    worktree = tmp_path / ".hermes" / "kanban" / "workspaces" / "source-task" / "ms-365-mcp-server"
    repo.mkdir(parents=True)
    worktree.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(allowed_root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="Correct misleading M365 mailbox tool descriptions",
            assignee="wren",
            workspace_kind="scratch",
            auto_review_gate=False,
        )
        kb.add_comment(
            conn,
            source_id,
            "wren",
            "review-required handoff:\n"
            + json.dumps(
                {
                    "repo": str(repo),
                    "worktree": str(worktree),
                    "branch": "wren/source-task-m365-tool-descriptions",
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                    "changed_files": ["src/graph-tools.ts", "test/tool-schema.test.ts"],
                    "tests": {"full": "193/193 passed", "build": "passed"},
                }
            ),
        )
        assert kb.block_task(conn, source_id, reason="review-required: mailbox schema fix ready")
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert len(reviews) == 1
    review = reviews[0]
    assert review.assignee == "code-reviewer"
    assert review.workspace_kind == "dir"
    assert review.workspace_path == str(worktree)
    assert review.idempotency_key == f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:1"
    assert f"Source task: {source_id}" in (review.body or "")
    assert "wren/source-task-m365-tool-descriptions" in (review.body or "")
    assert "193/193 passed" in (review.body or "")


def test_review_loop_records_cannot_review_for_insufficient_scratch_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    repo = tmp_path / "other" / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")

    with kb.connect_closing() as conn:
        source_id = kb.create_task(
            conn,
            title="scratch card without a branch or diff",
            assignee="sterling",
            workspace_kind="scratch",
            auto_review_gate=False,
        )
        kb.add_comment(
            conn,
            source_id,
            "sterling",
            json.dumps({"review_source": {"repo_path": str(repo)}}),
        )
        assert kb.block_task(conn, source_id, reason="review-required: please review")
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]
        source = kb.get_task(conn, source_id)
        events = kb.list_events(conn, source_id)
        comments = kb.list_comments(conn, source_id)

    assert source is not None and source.status == "blocked"
    assert reviews == []
    unavailable = [event for event in events if event.kind == "review_escalation"]
    assert len(unavailable) == 1
    assert unavailable[0].payload is not None
    assert unavailable[0].payload["verdict"] == "cannot_review"
    assert unavailable[0].payload["reason"] == "insufficient_review_evidence"
    assert any("CANNOT_REVIEW" in comment.body for comment in comments)


def test_review_loop_routes_out_of_root_dir_and_worktree_tasks_for_any_assignee(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    allowed_root = tmp_path / "configured-root"
    dir_repo = tmp_path / "client-repos" / "dir-repo"
    worktree = tmp_path / "vendor-repos" / "worktree"
    dir_repo.mkdir(parents=True)
    worktree.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ROOTS", str(allowed_root))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")

    with kb.connect_closing() as conn:
        dir_source = kb.create_task(
            conn,
            title="Virgil implementation in a persistent repo",
            assignee="virgil",
            workspace_kind="dir",
            workspace_path=str(dir_repo),
            auto_review_gate=False,
        )
        worktree_source = kb.create_task(
            conn,
            title="Zelda implementation in a worktree",
            assignee="zelda",
            workspace_kind="worktree",
            workspace_path=str(worktree),
            branch_name="zelda/worktree-copy-fix",
            auto_review_gate=False,
        )
        assert kb.block_task(conn, dir_source, reason="review-required: dir diff ready")
        assert kb.block_task(conn, worktree_source, reason="review-required: worktree branch ready")
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]

    assert len(reviews) == 2
    assert {review.workspace_path for review in reviews} == {str(dir_repo), str(worktree)}
    assert all(review.assignee == "code-reviewer" for review in reviews)
    source_links = [kb._review_loop_source_from_task(review) for review in reviews]
    assert all(source_link is not None for source_link in source_links)
    assert {source_link[0] for source_link in source_links if source_link is not None} == {
        dir_source,
        worktree_source,
    }


def test_review_loop_trigger_is_explicit_and_idempotent_per_block(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    repo = tmp_path / "outside-root" / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "true")

    with kb.connect_closing() as conn:
        ordinary_id = kb.create_task(
            conn,
            title="ordinary blocked task",
            assignee="dante",
            workspace_kind="dir",
            workspace_path=str(repo),
            auto_review_gate=False,
        )
        assert kb.block_task(conn, ordinary_id, reason="waiting for an operator")

        source_id = kb.create_task(
            conn,
            title="explicit review handoff",
            assignee="portia",
            workspace_kind="dir",
            workspace_path=str(repo),
            auto_review_gate=False,
        )
        assert kb.block_task(conn, source_id, reason="review-required: diff ready")
        first = kb._maybe_create_review_loop_task(conn, source_id, "review-required: diff ready")
        second = kb._maybe_create_review_loop_task(conn, source_id, "review-required: diff ready")
        reviews = [
            task for task in kb.list_tasks(conn, include_archived=True)
            if task.created_by == kb.REVIEW_LOOP_CREATED_BY
        ]
        ordinary_events = kb.list_events(conn, ordinary_id)
        phantom = kb._maybe_create_review_loop_task(
            conn,
            "t_deadbeef",
            "review-required: phantom source",
        )

    assert first == second
    assert phantom == kb.REVIEW_LOOP_CANNOT_REVIEW
    assert len(reviews) == 1
    assert reviews[0].idempotency_key == f"{kb.REVIEW_LOOP_IDEMPOTENCY_PREFIX}:{source_id}:1"
    assert all(event.kind != "review_loop_requested" for event in ordinary_events)


def _configure_explicit_review_test(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_LOOP_ENABLED", "false")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_GATE_ASSIGNEE", "code-reviewer")


def _make_blocked_source_and_explicit_review(conn, *, assignee="code-reviewer"):
    source_id = kb.create_task(
        conn,
        title="implementation awaiting explicit review",
        assignee="dante",
        auto_review_gate=False,
    )
    assert kb.block_task(conn, source_id, reason="review-required: ready for explicit review")
    review_id = kb.create_task(
        conn,
        title="review: implementation awaiting explicit review",
        assignee=assignee,
        auto_review_gate=False,
    )
    return source_id, review_id


def test_explicit_review_metadata_approval_unblocks_source_task(tmp_path, monkeypatch):
    _configure_explicit_review_test(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        source_id, review_id = _make_blocked_source_and_explicit_review(conn)
        assert kb.complete_task(
            conn,
            review_id,
            summary="PASS: explicit review approved",
            metadata={"source_task_id": source_id, "verdict": "approved"},
        )
        source = kb.get_task(conn, source_id)
        comments = kb.list_comments(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "ready"
    assert any(f"via {review_id}" in comment.body and "approved" in comment.body for comment in comments)
    assert "review_approved" in event_kinds


def test_explicit_review_metadata_changes_requested_unblocks_source_for_fixes(tmp_path, monkeypatch):
    _configure_explicit_review_test(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        source_id, review_id = _make_blocked_source_and_explicit_review(conn)
        assert kb.complete_task(
            conn,
            review_id,
            summary="BLOCK: fix test coverage",
            metadata={"review": {"source_task_id": source_id, "verdict": "changes_requested"}},
        )
        source = kb.get_task(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "ready"
    assert "review_changes_requested" in event_kinds


def test_review_gate_relation_with_verdict_metadata_unblocks_source(tmp_path, monkeypatch):
    _configure_explicit_review_test(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        source_id, review_id = _make_blocked_source_and_explicit_review(conn)
        conn.execute(
            "UPDATE tasks SET created_by = ?, idempotency_key = ? WHERE id = ?",
            (kb.REVIEW_GATE_CREATED_BY, f"{kb.REVIEW_GATE_CREATED_BY}:{source_id}", review_id),
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="PASS: gate relation approved",
            metadata={"verdict": "approved"},
        )
        source = kb.get_task(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "ready"
    assert "review_approved" in event_kinds


def test_explicit_review_metadata_cannot_review_leaves_source_blocked(tmp_path, monkeypatch):
    _configure_explicit_review_test(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        source_id, review_id = _make_blocked_source_and_explicit_review(conn)
        assert kb.complete_task(
            conn,
            review_id,
            summary="CANNOT_REVIEW: no diff available",
            metadata={"source_task_id": source_id, "verdict": "cannot_review"},
        )
        source = kb.get_task(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "blocked"
    assert "review_escalation" in event_kinds
    assert "review_approved" not in event_kinds


def test_non_review_task_cannot_spoof_explicit_review_unblock(tmp_path, monkeypatch):
    _configure_explicit_review_test(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        source_id, review_id = _make_blocked_source_and_explicit_review(conn, assignee="dante")
        assert kb.complete_task(
            conn,
            review_id,
            summary="PASS: pretending to approve",
            metadata={"source_task_id": source_id, "verdict": "approved"},
        )
        source = kb.get_task(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "blocked"
    assert "review_approved" not in event_kinds


def test_explicit_review_accepts_implementation_task_metadata_key(tmp_path, monkeypatch):
    _configure_explicit_review_test(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        source_id, review_id = _make_blocked_source_and_explicit_review(conn)
        assert kb.complete_task(
            conn,
            review_id,
            summary="PASS: explicit review approved",
            metadata={"implementation_task": source_id, "verdict": "approved"},
        )
        source = kb.get_task(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "ready"
    assert "review_approved" in event_kinds


def test_explicit_review_accepts_source_task_metadata_key(tmp_path, monkeypatch):
    _configure_explicit_review_test(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        source_id, review_id = _make_blocked_source_and_explicit_review(conn)
        assert kb.complete_task(
            conn,
            review_id,
            summary="PASS: explicit review approved",
            metadata={"source_task": source_id, "verdict": "approved"},
        )
        source = kb.get_task(conn, source_id)
        event_kinds = [event.kind for event in kb.list_events(conn, source_id)]

    assert source is not None
    assert source.status == "ready"
    assert "review_approved" in event_kinds
