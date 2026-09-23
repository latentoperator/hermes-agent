"""Creation-time protected-write grants use the real board and file-tool paths."""
import json


import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from tools.file_tools import patch_tool


@pytest.fixture
def worker(tmp_path, monkeypatch):
    import tools.file_tools_write_guards as guards

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setenv("HERMES_PROFILE", "dante")
    monkeypatch.setattr(guards, "_protected_instruction_config", lambda: (True, []))
    from tools.terminal_tool import set_approval_callback
    calls = []
    set_approval_callback(lambda *a, **kw: calls.append((a, kw)) or "deny")
    yield workspace, calls, monkeypatch
    set_approval_callback(None)


def _claim(workspace, monkeypatch, grant=None):
    with connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="fixture", assignee="dante", workspace_kind="dir",
            workspace_path=str(workspace), allow_protected=grant,
        )
        task = kb.claim_task(conn, task_id)
        assert task is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    return task_id


def _patch(target, text):
    return json.loads(patch_tool(mode="patch", patch=(
        "*** Begin Patch\n"
        f"*** Add File: {target}\n"
        f"+{text}\n"
        "*** End Patch"
    )))


def test_pre_authorized_write_succeeds_without_prompt_and_is_audited(worker):
    workspace, calls, monkeypatch = worker
    task_id = _claim(workspace, monkeypatch, ["AGENTS.md"])
    target = workspace / "AGENTS.md"
    result = _patch(target, "authorized")
    assert not result.get("error"), result
    assert target.read_text() == "authorized"
    assert calls == []
    with connect_closing() as conn:
        events = kb.list_events(conn, task_id)
    assert events[0].payload and events[0].payload["allow_protected"] == ["AGENTS.md"]
    assert [e.payload["paths"] for e in events if e.kind == "protected_write_authorized"] == [[str(target)]]


def test_ungranted_write_returns_immediately_without_prompt_or_card_block(worker):
    workspace, calls, monkeypatch = worker
    task_id = _claim(workspace, monkeypatch)
    target = workspace / "AGENTS.md"
    result = _patch(target, "defer")
    assert "If optional" in result["error"]
    assert not target.exists() and calls == []
    with connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        assert task and task.status == "running"
        kb.add_comment(conn, task_id, "dante", "Deferred AGENTS.md: +defer")
        assert kb.list_comments(conn, task_id)[-1].body.endswith("+defer")


def test_creation_grant_rejects_traversal_symlink_and_wrong_run(worker):
    workspace, calls, monkeypatch = worker
    with connect_closing() as conn:
        for invalid in ("../AGENTS.md", "/etc/AGENTS.md", "*.md", "nested//AGENTS.md"):
            with pytest.raises(ValueError):
                kb.create_task(conn, title="invalid", allow_protected=[invalid])
    _claim(workspace, monkeypatch, ["AGENTS.md"])
    (workspace / "AGENTS.md").symlink_to(workspace.parent / "AGENTS.md")
    assert "BLOCKED" in _patch(workspace / "AGENTS.md", "no")["error"]
    (workspace / "AGENTS.md").unlink()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")
    assert "BLOCKED" in _patch(workspace / "AGENTS.md", "no")["error"]
    assert not (workspace / "AGENTS.md").exists() and calls == []


def test_essential_write_decision_card_yes_resumes_and_consumes_once(worker):
    from gateway import decision_cards as dc

    workspace, calls, monkeypatch = worker
    task_id = _claim(workspace, monkeypatch)
    target = workspace / "AGENTS.md"
    assert "BLOCKED" in _patch(target, "approved")["error"]
    monkeypatch.setenv("HERMES_DECISION_QUEUE_DB", str(workspace.parent / "decisions.db"))
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    yes_action = "write the listed AGENTS.md once"
    card = dc.create_card(
        question="Approve the exact repo-instruction write?",
        context=f"Task {task_id} — Yes: {yes_action}\n{target}",
        default_action="Do not write.", fire_at="after approval",
        requested_by="dante kanban worker", source_ref=f"{task_id}:protected",
        originating_profile="dante", action_kind="protected_instruction_write",
        action_payload={"action": "write_protected_instruction_files", "task_id": task_id,
                        "paths": [str(target)], "yes_action": yes_action},
    )
    with connect_closing() as conn:
        assert kb.block_task(conn, task_id, reason="Essential AGENTS.md edit requires approval", kind="needs_input")
    dc.handle_action(card.id, "yes", actor="Chris", actor_id="42", actor_platform="discord")
    with connect_closing() as conn:
        assert kb.unblock_task(conn, task_id)
        task = kb.claim_task(conn, task_id)
        assert task and task.current_run_id
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    result = _patch(target, "approved")
    assert not result.get("error"), result
    assert target.read_text() == "approved"
    with dc.connect() as conn:
        events = conn.execute("SELECT event_type FROM decision_card_events WHERE card_id=? ORDER BY id",
                              (card.id,)).fetchall()
    assert [row["event_type"] for row in events][-1] == "protected_write_claimed"
    assert calls == []
