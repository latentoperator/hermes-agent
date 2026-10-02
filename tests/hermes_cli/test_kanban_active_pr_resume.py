"""A release resumes the existing PR only after an outside continuation event."""
from pathlib import Path
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    # Real domain writes, but a deterministic clock keeps the event ordering clear.
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    kb.init_db()
    with kbc.connect() as conn:
        yield conn, clock


def _pr(conn, tid):
    kb.add_comment(conn, tid, author="dev", body="PR https://github.com/example/repo/pull/9")


def _dispatch(conn, tid, expected):
    result = kbd.dispatch_once(conn, dry_run=True)
    assert (tid in [s[0] for s in result.spawned]) is expected
    assert dict(result.respawn_guarded).get(tid) == (None if expected else "active_pr")


def test_review_dependency_completion_resumes_existing_pr(board):
    """t_a52f0189: PR -> wait on review -> review done -> ready -> spawn."""
    conn, clock = board
    tid = kb.create_task(conn, title="release existing PR", assignee="dev")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    review = kb.create_task(conn, title="exact-head review", assignee="reviewer")
    kb.link_tasks(conn, review, tid, expected_child_run_id=claimed.current_run_id)
    _pr(conn, tid)
    clock[0] += 2
    assert kb.block_task(conn, tid, reason="wait for review", kind="dependency")
    assert kb.get_task(conn, tid).status == "todo"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    clock[0] += 2
    assert kb.claim_task(conn, review)
    assert kb.complete_task(conn, review, summary="PASS exact head")
    assert kb.get_task(conn, tid).status == "ready"
    assert kbd.check_respawn_guard(conn, tid) is None
    _dispatch(conn, tid, True)
    assert kb.claim_task(conn, tid) is not None
    clock[0] += 2
    # A crash/recovery after the authorized claim cannot reuse the old review.
    assert kb.block_task(conn, tid, reason="worker stalled", kind="transient")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "promoted", None)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    # A fresh PR post consumes the continuation; the old review is not a free pass.
    clock[0] += 2
    _pr(conn, tid)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    _dispatch(conn, tid, False)


def test_explicit_unblock_resumes_existing_pr(board):
    conn, clock = board
    tid = kb.create_task(conn, title="unblock release", assignee="dev")
    assert kb.claim_task(conn, tid)
    _pr(conn, tid)
    clock[0] += 2
    assert kb.block_task(conn, tid, reason="operator decision", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"
    _dispatch(conn, tid, True)


def test_operator_reclaim_same_profile_resumes_existing_pr(board):
    conn, clock = board
    tid = kb.create_task(conn, title="reclaim release", assignee="dev")
    assert kb.claim_task(conn, tid)
    _pr(conn, tid)
    clock[0] += 2
    assert kb.reassign_task(conn, tid, "dev", reclaim_first=True)
    assert kb.get_task(conn, tid).status == "ready"
    _dispatch(conn, tid, True)


@pytest.mark.parametrize("kind,payload", [
    ("status", {"status": "ready"}),
    ("promoted_manual", {"actor": "operator"}),
])
def test_explicit_ready_requeue_resumes_existing_pr(board, kind, payload):
    conn, clock = board
    tid = kb.create_task(conn, title="requeue release", assignee="dev")
    _pr(conn, tid)
    clock[0] += 2
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kind, payload)
    _dispatch(conn, tid, True)


@pytest.mark.parametrize("kind,payload", [
    ("reclaimed", {"retry_status": "ready"}),  # automatic TTL/dead-worker recovery
    ("reclaimed", {"manual": True, "retry_status": "review"}),
    ("promoted", None),  # no parent completion: ordinary recovery, not dependency resume
    ("status", {"status": "blocked"}),
    ("unblocked", {"status": "todo"}),
    ("unblocked", "malformed"),
    ("assigned", {"from": "dev", "assignee": "dev"}),
])
def test_worker_recovery_and_nonready_events_do_not_lift_guard(board, kind, payload):
    conn, clock = board
    tid = kb.create_task(conn, title="no external continuation", assignee="dev")
    _pr(conn, tid)
    clock[0] += 2
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kind, payload)
    _dispatch(conn, tid, False)


def test_same_worker_block_loop_and_reposted_pr_stay_guarded(board):
    conn, clock = board
    tid = kb.create_task(conn, title="duplicate protection", assignee="dev")
    assert kb.claim_task(conn, tid)
    _pr(conn, tid)
    clock[0] += 2
    # No open dependency: the domain layer parks this rather than authorizing a resume.
    assert kb.block_task(conn, tid, reason="wait", kind="dependency")
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.recompute_ready(conn) == 0
    _pr(conn, tid)
    clock[0] += 2
    # Model a legacy untyped recovery loop; its promotion is not outside authorization.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kb._append_event(conn, tid, "promoted", None)
    _dispatch(conn, tid, False)


def test_same_second_continuation_remains_fail_closed(board):
    conn, _ = board
    tid = kb.create_task(conn, title="ambiguous order", assignee="dev")
    _pr(conn, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "status", {"status": "ready"})
    _dispatch(conn, tid, False)


def test_malformed_resume_payload_is_fail_closed(board):
    conn, clock = board
    tid = kb.create_task(conn, title="invalid event", assignee="dev")
    _pr(conn, tid)
    clock[0] += 2
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "unblocked", None)
        conn.execute(
            "UPDATE task_events SET payload = 'not-json' WHERE task_id = ? AND kind = 'unblocked'",
            (tid,),
        )
    _dispatch(conn, tid, False)
