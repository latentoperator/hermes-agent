"""Tests for Kanban dispatch-group tracking and agent feedback notices.

Verifies:
  - Schema migration: new tables exist after init_db.
  - Dispatch group lifecycle: create, enroll, recompute, drain, notice emit.
  - Dedup: identical blocked/drained notices aren't duplicated.
  - Unblock reactivation: group re-enters active state after unblock.
  - Notice retrieval and ack.
  - Tool integration: kanban_create auto-enrolls, kanban_block emits notices.
  - Prompt formatting helpers.
"""

from __future__ import annotations

import json
import os

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_kanban_db(monkeypatch, tmp_path):
    """Isolate HERMES_HOME and return a connected kanban_db + conn."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    return kb, conn


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def test_dispatch_tables_exist_after_init(monkeypatch, tmp_path):
    """After init_db, dispatch_groups, dispatch_group_tasks, and
    dispatch_notices tables must exist."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "dispatch_groups" in tables
        assert "dispatch_group_tasks" in tables
        assert "dispatch_notices" in tables
    finally:
        conn.close()


def test_dispatch_notices_indexes_exist(monkeypatch, tmp_path):
    """Indexes for pending-notice lookup and group-task joins must exist."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        indexes = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_dispatch_notices_pending" in indexes
        assert "idx_dispatch_group_tasks_group" in indexes
        assert "idx_dispatch_group_tasks_task" in indexes
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dispatch group lifecycle
# ---------------------------------------------------------------------------

def test_get_or_create_dispatch_group_idempotent(monkeypatch, tmp_path):
    """Same profile+session returns the same group id on repeated calls."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        g1 = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        g2 = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        assert g1 == g2
        # Only one row in dispatch_groups
        cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM dispatch_groups WHERE id = ?", (g1,)
        ).fetchone()["n"]
        assert cnt == 1
    finally:
        conn.close()


def test_get_or_create_dispatch_group_different_sessions(monkeypatch, tmp_path):
    """Different sessions for the same profile produce different group ids."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        g1 = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        g2 = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-2",
        )
        assert g1 != g2
    finally:
        conn.close()


def test_add_task_to_dispatch_group(monkeypatch, tmp_path):
    """Enrolling a task in a group creates a row and is idempotent."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        tid = kb.create_task(conn, title="test-task", assignee="peer")
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        kb.add_task_to_dispatch_group(conn, gid, tid)
        # Idempotent
        kb.add_task_to_dispatch_group(conn, gid, tid)
        rows = conn.execute(
            "SELECT task_id FROM dispatch_group_tasks WHERE group_id = ?",
            (gid,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["task_id"] == tid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Group state recomputation
# ---------------------------------------------------------------------------

def test_recompute_dispatch_group_active(monkeypatch, tmp_path):
    """A group with tasks in active statuses reports 'active'."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        tid = kb.create_task(conn, title="active-task", assignee="peer")
        kb.claim_task(conn, tid)  # → running
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        kb.add_task_to_dispatch_group(conn, gid, tid)
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "active"
    finally:
        conn.close()


def test_recompute_dispatch_group_drained(monkeypatch, tmp_path):
    """A group where all tasks are done/blocked/archived reports 'drained'."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        tid = kb.create_task(conn, title="done-task", assignee="peer")
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        kb.add_task_to_dispatch_group(conn, gid, tid)
        # Complete the task → done
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid)
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "drained"
    finally:
        conn.close()


def test_recompute_dispatch_group_mixed(monkeypatch, tmp_path):
    """One active + one done → still 'active'."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        t1 = kb.create_task(conn, title="active", assignee="peer")
        t2 = kb.create_task(conn, title="done", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.add_task_to_dispatch_group(conn, gid, t2)
        kb.claim_task(conn, t2)
        kb.complete_task(conn, t2)  # t2 → done
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "active"  # t1 still ready
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notice emission and dedup
# ---------------------------------------------------------------------------

def test_emit_dispatch_notice_blocked(monkeypatch, tmp_path):
    """Emit a blocked notice for a dispatch group."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        nid = kb.emit_dispatch_notice(
            conn, group_id=gid, kind="blocked",
            payload={"task_id": "t_test", "title": "test"},
        )
        assert nid is not None
        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-1")
        assert len(notices) == 1
        assert notices[0]["kind"] == "blocked"
        assert notices[0]["payload"]["task_id"] == "t_test"
    finally:
        conn.close()


def test_emit_dispatch_notice_dedup(monkeypatch, tmp_path):
    """Duplicate blocked notice for same group+kind → not emitted twice."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        n1 = kb.emit_dispatch_notice(
            conn, group_id=gid, kind="blocked",
            payload={"task_id": "t_test"},
        )
        n2 = kb.emit_dispatch_notice(
            conn, group_id=gid, kind="blocked",
            payload={"task_id": "t_test"},
        )
        assert n1 is not None
        assert n2 is None  # deduped
    finally:
        conn.close()


def test_emit_dispatch_notice_session_id_null(monkeypatch, tmp_path):
    """Notices for groups without session_id are retrievable with session_id=None."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id=None,
        )
        kb.emit_dispatch_notice(
            conn, group_id=gid, kind="drained",
            payload={"total": 3},
        )
        notices = kb.get_pending_dispatch_notices(
            conn, "wren", session_id=None,
        )
        assert len(notices) == 1
        # session_id="sess-1" should NOT match the null-session notice
        notices2 = kb.get_pending_dispatch_notices(
            conn, "wren", session_id="sess-1",
        )
        assert len(notices2) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notice ack
# ---------------------------------------------------------------------------

def test_ack_dispatch_notice(monkeypatch, tmp_path):
    """Acking a notice removes it from pending results."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        nid = kb.emit_dispatch_notice(
            conn, group_id=gid, kind="blocked",
            payload={"task_id": "t_test"},
        )
        assert nid is not None
        ok = kb.ack_dispatch_notice(conn, nid)
        assert ok
        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-1")
        assert len(notices) == 0
    finally:
        conn.close()


def test_ack_all_dispatch_notices(monkeypatch, tmp_path):
    """Ack all clears every pending notice for a profile+session."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        kb.emit_dispatch_notice(
            conn, group_id=gid, kind="blocked",
            payload={"task_id": "t1"},
        )
        kb.emit_dispatch_notice(
            conn, group_id=gid, kind="drained",
            payload={"total": 2},
        )
        # Wait, drain is deduped — emit for different group
        gid2 = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1", board="alt",
        )
        kb.emit_dispatch_notice(
            conn, group_id=gid2, kind="drained",
            payload={"total": 1},
        )
        count = kb.ack_all_dispatch_notices(conn, "wren", "sess-1")
        assert count > 0
        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-1")
        assert len(notices) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# check_task_dispatch_blocked
# ---------------------------------------------------------------------------

def test_check_task_dispatch_blocked_emits_notice(monkeypatch, tmp_path):
    """When a tracked task is blocked (via block_task), a blocked notice fires.
    The group stays active because another task is still active."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        t1 = kb.create_task(conn, title="will-block", assignee="peer")
        t2 = kb.create_task(conn, title="stays-active", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.add_task_to_dispatch_group(conn, gid, t2)
        kb.claim_task(conn, t1)
        kb.block_task(conn, t1, reason="need input")

        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-1")
        blocked_emits = [n for n in notices if n["kind"] == "blocked"]
        assert len(blocked_emits) >= 1
        assert blocked_emits[0]["payload"]["task_id"] == t1
        # Group still has t2 active → no drain notice
        drain_emits = [n for n in notices if n["kind"] == "drained"]
        assert len(drain_emits) == 0
    finally:
        conn.close()


def test_check_task_dispatch_blocked_drains_when_last_active(monkeypatch, tmp_path):
    """Blocking the last active task emits both blocked and drain notices."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        t1 = kb.create_task(conn, title="only-task", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.claim_task(conn, t1)
        kb.block_task(conn, t1, reason="blocked")

        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-1")
        kinds = {n["kind"] for n in notices}
        assert "blocked" in kinds
        assert "drained" in kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# recompute_all_dispatch_groups
# ---------------------------------------------------------------------------

def test_recompute_all_dispatch_groups_emits_drain(monkeypatch, tmp_path):
    """recompute_all_dispatch_groups emits drain for groups not yet drained.

    complete_task now calls recompute_dispatch_groups_for_task internally,
    so the drain notice is emitted during completion.  recompute_all_dispatch_groups
    must NOT double-emit when called after — idempotency matters.
    """
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        # Verify drain notice was emitted by complete_task's internal recompute.
        t1 = kb.create_task(conn, title="task-a", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.claim_task(conn, t1)
        kb.complete_task(conn, t1)

        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-1", limit=100)
        drain_notices = [n for n in notices if n["kind"] == "drained"]
        assert len(drain_notices) >= 1, (
            "complete_task should emit drain notice via internal recompute"
        )

        # recompute_all_dispatch_groups must be idempotent — no double drain.
        emitted = kb.recompute_all_dispatch_groups(conn)
        assert emitted == [], (
            "recompute_all_dispatch_groups must not re-emit already-drained notices"
        )

        # Ack the first drain notice, then directly set task back to ready
        # so recompute_all_dispatch_groups sees active→drained on its own.
        for n in drain_notices:
            kb.ack_dispatch_notice(conn, n["notice_id"])
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (t1,))
        conn.execute("UPDATE dispatch_groups SET state = 'active' WHERE id = ?", (gid,))
        kb.complete_task(conn, t1)

        # complete_task already drained again — confirm recompute_all idempotent.
        emitted2 = kb.recompute_all_dispatch_groups(conn)
        assert emitted2 == [], "idempotent after second complete"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unblock / reactivation
# ---------------------------------------------------------------------------

def test_dispatch_group_reactivates_after_unblock(monkeypatch, tmp_path):
    """When a blocked task is unblocked, the group goes back to active
    (even if it previously drained via blocked)."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-1",
        )
        t1 = kb.create_task(conn, title="block-me", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.claim_task(conn, t1)
        kb.block_task(conn, t1, reason="review needed")
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "drained"
        # Unblock → task goes back to ready
        kb.unblock_task(conn, t1)
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "active"
        # Complete it
        kb.claim_task(conn, t1)
        kb.complete_task(conn, t1)
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "drained"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

def test_format_dispatch_notice_blocked():
    """Format a blocked notice as compact agent-readable text."""
    from hermes_cli.kanban_db import format_dispatch_notice_for_prompt
    notice = {
        "kind": "blocked",
        "payload": {"task_id": "t_abc123", "title": "Fix auth bug"},
    }
    text = format_dispatch_notice_for_prompt(notice)
    assert "t_abc123" in text
    assert "Fix auth bug" in text
    assert "blocked" in text.lower()


def test_format_dispatch_notice_drained():
    """Format a drained notice with by-status counts."""
    from hermes_cli.kanban_db import format_dispatch_notice_for_prompt
    notice = {
        "kind": "drained",
        "payload": {
            "total": 5,
            "by_status": {"done": 3, "blocked": 2},
        },
    }
    text = format_dispatch_notice_for_prompt(notice)
    assert "5 tasks" in text or "5" in text
    assert "3 completed" in text
    assert "2 blocked" in text


def test_build_dispatch_notices_context_empty():
    """Empty list returns empty string."""
    from hermes_cli.kanban_db import build_dispatch_notices_context
    assert build_dispatch_notices_context([]) == ""


def test_build_dispatch_notices_context_multiple():
    """Multiple notices produce a context block."""
    from hermes_cli.kanban_db import build_dispatch_notices_context
    notices = [
        {"kind": "blocked", "payload": {"task_id": "t_a", "title": "A"}},
        {"kind": "drained", "payload": {"total": 1, "by_status": {"done": 1}}},
    ]
    ctx = build_dispatch_notices_context(notices)
    assert "Dispatch notices" in ctx
    assert "t_a" in ctx
    assert "drained" in ctx


# ---------------------------------------------------------------------------
# Tool integration: kanban_create auto-enrollment
# ---------------------------------------------------------------------------

def test_kanban_create_auto_enrolls_interactive_session_in_dispatch_group(monkeypatch, tmp_path):
    """Interactive agent kanban_create auto-enrolls the task in a dispatch group
    for that profile+session."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "wren")
    monkeypatch.setenv("HERMES_SESSION_ID", "acp-sess-test")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "auto-enrolled",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True, f"unexpected error: {d}"
    new_tid = d["task_id"]

    conn2 = kb.connect()
    try:
        # Verify the task is in a dispatch group
        rows = conn2.execute(
            "SELECT group_id FROM dispatch_group_tasks WHERE task_id = ?",
            (new_tid,),
        ).fetchall()
        assert len(rows) == 1, f"task {new_tid} not in any dispatch group"
        gid = rows[0]["group_id"]
        # Verify the group exists and has the right profile
        g = conn2.execute(
            "SELECT origin_profile, origin_session FROM dispatch_groups WHERE id = ?",
            (gid,),
        ).fetchone()
        assert g is not None
        assert g["origin_profile"] == "wren"
        assert g["origin_session"] == "acp-sess-test"
    finally:
        conn2.close()


def test_kanban_worker_create_does_not_enroll_dispatch_group(monkeypatch, tmp_path):
    """Dispatcher-spawned workers must not create origin-profile drain notices
    for child cards; those notices wake/pollute the non-assignee profile later."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "sterling")
    monkeypatch.setenv("HERMES_SESSION_ID", "worker-sess-test")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        parent_tid = kb.create_task(
            conn, title="parent", assignee="sterling",
        )
        kb.claim_task(conn, parent_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_tid)

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "child should not notify origin",
        "assignee": "zelda",
        "parents": [parent_tid],
    })
    d = json.loads(out)
    assert d["ok"] is True, f"unexpected error: {d}"
    new_tid = d["task_id"]

    conn2 = kb.connect()
    try:
        cnt = conn2.execute(
            "SELECT COUNT(*) AS n FROM dispatch_group_tasks WHERE task_id = ?",
            (new_tid,),
        ).fetchone()["n"]
        assert cnt == 0, "worker-created child task should not be dispatch-group enrolled"
    finally:
        conn2.close()


def test_kanban_create_no_enroll_without_profile(monkeypatch, tmp_path):
    """Without HERMES_PROFILE, dispatch groups are not touched."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        parent_tid = kb.create_task(
            conn, title="parent-no-profile", assignee="peer",
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_tid)

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "no-enroll",
        "assignee": "peer",
        "parents": [parent_tid],
    })
    d = json.loads(out)
    assert d["ok"] is True, f"unexpected error: {d}"
    new_tid = d["task_id"]

    conn2 = kb.connect()
    try:
        cnt = conn2.execute(
            "SELECT COUNT(*) AS n FROM dispatch_group_tasks WHERE task_id = ?",
            (new_tid,),
        ).fetchone()["n"]
        assert cnt == 0, "task should not be enrolled without HERMES_PROFILE"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Tool integration: kanban_block triggers notice
# ---------------------------------------------------------------------------

def test_kanban_block_triggers_dispatch_notice(monkeypatch, tmp_path):
    """When a task enrolled in a dispatch group is blocked via kanban_block,
    a blocked notice is emitted."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "wren")
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-block-test")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="will-be-blocked", assignee="peer")
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-block-test",
        )
        kb.add_task_to_dispatch_group(conn, gid, tid)
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "needs review"})
    d = json.loads(out)
    assert d.get("ok") is True, f"block failed: {d}"

    conn2 = kb.connect()
    try:
        notices = kb.get_pending_dispatch_notices(
            conn2, "wren", "sess-block-test",
        )
        assert len(notices) >= 1, "expected at least one blocked notice"
        blocked = [n for n in notices if n["kind"] == "blocked"]
        assert len(blocked) >= 1
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_dispatch_group_nonexistent_task_graceful(monkeypatch, tmp_path):
    """check_task_dispatch_blocked on a task not in any group is a no-op."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        emitted = kb.check_task_dispatch_blocked(conn, "t_nonexistent")
        assert emitted == []
    finally:
        conn.close()


def test_emit_notice_for_nonexistent_group(monkeypatch, tmp_path):
    """emit_dispatch_notice for a missing group returns None."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        nid = kb.emit_dispatch_notice(
            conn, group_id="nonexistent", kind="drained",
        )
        assert nid is None
    finally:
        conn.close()


def test_recompute_state_nonexistent_group(monkeypatch, tmp_path):
    """recompute_dispatch_group_state for a missing group returns 'drained'."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        state = kb.recompute_dispatch_group_state(conn, "nonexistent")
        assert state == "drained"
    finally:
        conn.close()


def test_ack_nonexistent_notice(monkeypatch, tmp_path):
    """Acking a nonexistent notice returns False."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        ok = kb.ack_dispatch_notice(conn, 999999)
        assert not ok
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regression: two tasks blocking → two separate blocked notices (Finding 1)
# ---------------------------------------------------------------------------

def test_two_blocked_tasks_produce_two_notices(monkeypatch, tmp_path):
    """When two tracked tasks in the same group are blocked, each emits its
    own blocked notice — not deduped into one."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-2block",
        )
        t1 = kb.create_task(conn, title="first-block", assignee="peer")
        t2 = kb.create_task(conn, title="second-block", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.add_task_to_dispatch_group(conn, gid, t2)

        # Block both
        kb.claim_task(conn, t1)
        kb.block_task(conn, t1, reason="review")
        kb.claim_task(conn, t2)
        kb.block_task(conn, t2, reason="review")

        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-2block")
        blocked_notices = [n for n in notices if n["kind"] == "blocked"]
        assert len(blocked_notices) == 2, (
            f"expected 2 blocked notices, got {len(blocked_notices)}: {blocked_notices}"
        )
        task_ids_blocked = {n["payload"]["task_id"] for n in blocked_notices}
        assert task_ids_blocked == {t1, t2}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regression: unblock → complete → second drain (Finding 2)
# ---------------------------------------------------------------------------

def test_unblock_then_complete_emits_second_drain(monkeypatch, tmp_path):
    """After a blocked task drains the group, unblocking+completing it
    should produce a second drain notice (active→drained transition)."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-redrain",
        )
        t1 = kb.create_task(conn, title="block-redrain", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)

        # Block the only task → group drains
        kb.claim_task(conn, t1)
        kb.block_task(conn, t1, reason="review")
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "drained"

        # Collect first-wave notices
        notices_1 = kb.get_pending_dispatch_notices(conn, "wren", "sess-redrain")
        drain_count_1 = len([n for n in notices_1 if n["kind"] == "drained"])
        assert drain_count_1 == 1

        # Ack them so they don't mask the second drain
        kb.ack_all_dispatch_notices(conn, "wren", "sess-redrain")

        # Unblock → group should be active again
        kb.unblock_task(conn, t1)
        kb.recompute_all_dispatch_groups(conn)
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "active"

        # Complete → group drains a second time
        kb.claim_task(conn, t1)
        kb.complete_task(conn, t1)
        kb.recompute_all_dispatch_groups(conn)

        notices_2 = kb.get_pending_dispatch_notices(conn, "wren", "sess-redrain")
        drain_count_2 = len([n for n in notices_2 if n["kind"] == "drained"])
        assert drain_count_2 == 1, (
            f"expected second drain notice after unblock+complete, "
            f"got {drain_count_2}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regression: block_task direct call fires dispatch notice (Finding 3)
# ---------------------------------------------------------------------------

def test_block_task_direct_triggers_dispatch_notice(monkeypatch, tmp_path):
    """Calling block_task() directly (not via kanban_block tool) still
    emits a blocked dispatch notice and recomputes group state."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-direct",
        )
        t1 = kb.create_task(conn, title="direct-block", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.claim_task(conn, t1)

        # Direct call — not through kanban_tools._handle_block
        ok = kb.block_task(conn, t1, reason="circuit breaker trip")
        assert ok

        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-direct")
        blocked = [n for n in notices if n["kind"] == "blocked"]
        assert len(blocked) >= 1, (
            f"block_task direct call should emit blocked notice, "
            f"got {len(blocked)}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regression: triage is an active status
# ---------------------------------------------------------------------------

def test_triage_status_is_active(monkeypatch, tmp_path):
    """Tasks with status 'triage' keep the dispatch group active."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-triage",
        )
        # Manually create a task and force it to triage status
        now = int(__import__("time").time())
        tid = "t_triage_test_01"
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at) "
            "VALUES (?, ?, 'triage', 'peer', ?)",
            (tid, "triage-task", now),
        )
        kb.add_task_to_dispatch_group(conn, gid, tid)
        state = kb.recompute_dispatch_group_state(conn, gid)
        assert state == "active", f"triage task should keep group active, got {state}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regression: blocked notice dedup is per-task, not per-group
# ---------------------------------------------------------------------------

def test_blocked_notice_dedup_per_task_not_per_group(monkeypatch, tmp_path):
    """The dedup check for blocked notices matches on (group_id, kind,
    task_id), so the SAME task blocking twice is deduped but a DIFFERENT
    task is NOT."""
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-dedup",
        )
        t1 = kb.create_task(conn, title="dedup-a", assignee="peer")
        t2 = kb.create_task(conn, title="dedup-b", assignee="peer")
        kb.add_task_to_dispatch_group(conn, gid, t1)
        kb.add_task_to_dispatch_group(conn, gid, t2)

        # Same task blocked twice — second emit should be deduped
        kb.claim_task(conn, t1)
        kb.block_task(conn, t1, reason="first block")
        # Force another blocked notice attempt for same task
        nid_dup = kb.emit_dispatch_notice(
            conn, group_id=gid, kind="blocked",
            payload={"task_id": t1, "title": "dedup-a"},
        )
        assert nid_dup is None, "same task blocked twice should be deduped"

        # Different task blocked — should get its own notice
        kb.claim_task(conn, t2)
        kb.block_task(conn, t2, reason="second block")
        notices = kb.get_pending_dispatch_notices(conn, "wren", "sess-dedup")
        blocked = [n for n in notices if n["kind"] == "blocked"]
        task_ids = {n["payload"]["task_id"] for n in blocked}
        assert task_ids == {t1, t2}, (
            f"both tasks should have notices, got {task_ids}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Conversation-loop injection tests
# ---------------------------------------------------------------------------


def test_conversation_loop_injection_no_profile(monkeypatch, tmp_path):
    """Without HERMES_PROFILE, the injection is a no-op and user_message
    is unchanged."""
    monkeypatch.delenv("HERMES_PROFILE", raising=False)

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    # Replicate the injection logic inline
    import os as _os
    _dispatch_prefix = ""
    origin_profile = _os.environ.get("HERMES_PROFILE")
    assert origin_profile is None  # precondition

    user_message = "hello"
    if origin_profile:
        _dispatch_prefix = "SHOULD NOT APPEAR"
    if _dispatch_prefix:
        user_message = _dispatch_prefix + "\n\n" + user_message
    assert user_message == "hello"


def test_conversation_loop_injection_drains_notices(monkeypatch, tmp_path):
    """When HERMES_PROFILE is set and pending notices exist, the injection
    prepends them to user_message and defers the ack (notices remain
    unacked until the deferred-ack path runs after a completed turn)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "wren")

    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        gid = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-inject",
        )
        nid = kb.emit_dispatch_notice(
            conn, group_id=gid, kind="drained",
            payload={"total": 2, "by_status": {"done": 2}},
        )
        assert nid is not None
    finally:
        conn.close()

    # Replicate the injection logic (fetch only — no immediate ack)
    import os as _os
    _dispatch_prefix = ""
    _dispatch_notice_ids = []
    origin_profile = _os.environ.get("HERMES_PROFILE")
    session_id = "sess-inject"
    if origin_profile:
        try:
            from hermes_cli import kanban_db as _kb
            _conn = _kb.connect()
            try:
                _notices = _kb.get_pending_dispatch_notices(
                    _conn, origin_profile, session_id,
                )
                if _notices:
                    _dispatch_prefix = _kb.build_dispatch_notices_context(_notices)
                    _dispatch_notice_ids = [
                        n["notice_id"] for n in _notices
                    ]
            finally:
                _conn.close()
        except Exception:
            pass

    user_message = "hello"
    if _dispatch_prefix:
        user_message = _dispatch_prefix + "\n\n" + user_message

    assert "drained" in user_message
    assert "2 completed" in user_message or "2" in user_message
    assert user_message.endswith("hello")

    # Notices should still be unacked (deferred ack hasn't run yet)
    conn2 = kb.connect()
    try:
        remaining = kb.get_pending_dispatch_notices(conn2, "wren", "sess-inject")
        assert len(remaining) == 1, "notices should still be unacked before deferred ack"
    finally:
        conn2.close()

    # Simulate the deferred ack (as run after a completed turn)
    if _dispatch_notice_ids:
        conn3 = kb.connect()
        try:
            acked = kb.ack_dispatch_notices(conn3, _dispatch_notice_ids)
            assert acked == 1
        finally:
            conn3.close()

    # Now notices should be gone
    conn4 = kb.connect()
    try:
        remaining2 = kb.get_pending_dispatch_notices(conn4, "wren", "sess-inject")
        assert len(remaining2) == 0, "notices should be acked after deferred ack"
    finally:
        conn4.close()


def test_conversation_loop_injection_no_notices(monkeypatch, tmp_path):
    """When HERMES_PROFILE is set but no pending notices exist, user_message
    is unchanged."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "wren")

    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)  # noqa: B023

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    import os as _os
    _dispatch_prefix = ""
    origin_profile = _os.environ.get("HERMES_PROFILE")
    session_id = "sess-no-notices"
    if origin_profile:
        try:
            from hermes_cli import kanban_db as _kb
            _conn = _kb.connect()
            try:
                _notices = _kb.get_pending_dispatch_notices(
                    _conn, origin_profile, session_id,
                )
                if _notices:
                    _dispatch_prefix = _kb.build_dispatch_notices_context(_notices)
                    _kb.ack_all_dispatch_notices(
                        _conn, origin_profile, session_id,
                    )
            finally:
                _conn.close()
        except Exception:
            pass

    user_message = "hello"
    if _dispatch_prefix:
        user_message = _dispatch_prefix + "\n\n" + user_message

    assert user_message == "hello"


# ---------------------------------------------------------------------------
# Regression: >5 notices silently dropped (default limit was 5)
# ---------------------------------------------------------------------------

def test_get_pending_notices_default_limit_bumped(monkeypatch, tmp_path):
    """Default limit (None = unlimited) returns all notices; explicit limit caps results.

    Regression test for the bug where conversation_loop called
    get_pending_dispatch_notices without a limit, got at most 5 notices,
    then ack_all_dispatch_notices acked everything — notices 6+ were
    silently lost.  The fix removes the implicit limit and adds targeted-ack.
    """
    kb, conn = _fresh_kanban_db(monkeypatch, tmp_path)
    try:
        # Emit 7 distinct blocked notices (unique task_ids beat dedup).
        g = kb.get_or_create_dispatch_group(
            conn, profile="wren", session_id="sess-flood",
        )
        notice_ids = []
        for i in range(7):
            nid = kb.emit_dispatch_notice(
                conn, group_id=g, kind="blocked",
                payload={"idx": i, "task_id": f"t_{i:03d}"},
            )
            assert nid is not None, f"notice {i} should be emitted"
            notice_ids.append(nid)

        # New default (None = unlimited) returns all 7 — the old default
        # silently drop 2.
        notices_all = kb.get_pending_dispatch_notices(
            conn, "wren", "sess-flood",
        )
        assert len(notices_all) == 7, (
            f"default limit should return all 7, got {len(notices_all)}"
        )

        # Explicit small limit still caps correctly.
        notices_3 = kb.get_pending_dispatch_notices(
            conn, "wren", "sess-flood", limit=3,
        )
        assert len(notices_3) == 3, (
            f"limit=3 should return 3, got {len(notices_3)}"
        )

        # Verify targeted ack (new function) only removes specified notices.
        acked = kb.ack_dispatch_notices(conn, notice_ids[:3])
        assert acked == 3
        remaining = kb.get_pending_dispatch_notices(
            conn, "wren", "sess-flood",
        )
        assert len(remaining) == 4, (
            f"4 should remain after targeted ack of 3, got {len(remaining)}"
        )

        # Verify ack_all removes everything.
        acked_all = kb.ack_all_dispatch_notices(conn, "wren", "sess-flood")
        assert acked_all == 4
        after = kb.get_pending_dispatch_notices(
            conn, "wren", "sess-flood",
        )
        assert len(after) == 0

    finally:
        conn.close()
