"""Deferred workspace cleanup is transitive along parent links (#33774 follow-up).

A finished task keeps its scratch/worktree workspace while a child is still
active. On a serial chain A -> B -> C the old sweep leaked A forever: B
finishing while C was open returned before re-checking B's parents, and C
finishing removed B without walking on to A. ``kanban gc`` only looked at
archived tasks, so nothing ever reclaimed the leaked ``done`` workspaces.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_ops


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(*args: str) -> None:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo whose history is fully pushed to a local bare origin."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _scratch_task(conn, title: str, *, parents: tuple[str, ...] = ()) -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, parents=parents)
    ws = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
    kbw.set_workspace_path(conn, tid, ws)
    (ws / "handoff.txt").write_text(title, encoding="utf-8")
    return tid, ws


def _chain(conn, depth: int) -> list[tuple[str, Path]]:
    tasks = [_scratch_task(conn, "t0")]
    for i in range(1, depth):
        tasks.append(_scratch_task(conn, f"t{i}", parents=(tasks[-1][0],)))
    return tasks


def _mark_done_without_hook(conn, task_id: str) -> None:
    """A terminal transition that skipped ``_cleanup_workspace`` (a missed hook)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    kb.recompute_ready(conn)  # promote children the way a real completion would


def _complete(conn, task_id: str) -> None:
    assert kb.complete_task(conn, task_id, result="done")


def _gc() -> int:
    return kanban_ops._cmd_gc(argparse.Namespace(event_retention_days=0, log_retention_days=0))


# ---------------------------------------------------------------------------
# Serial chains
# ---------------------------------------------------------------------------


def test_three_deep_chain_in_order_leaves_nothing(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        (a, wa), (b, wb), (c, wc) = _chain(conn, 3)
        _complete(conn, a)
        assert wa.is_dir()  # B still needs A's handoff
        _complete(conn, b)
        # B went terminal while C is open: B stays, but A has no active child left.
        assert not wa.exists()
        assert wb.is_dir()
        _complete(conn, c)
    assert not wb.exists()
    assert not wc.exists()


def test_four_deep_chain_with_missed_hooks_is_swept_by_the_leaf(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        chain = _chain(conn, 4)
        for tid, _ws in chain[:3]:
            _mark_done_without_hook(conn, tid)
        assert all(ws.is_dir() for _tid, ws in chain)
        _complete(conn, chain[3][0])
    assert not any(ws.exists() for _tid, ws in chain)


def test_four_deep_chain_out_of_order_archive(kanban_home: Path) -> None:
    """The leaf is archived first, then the ancestors finish top-down."""
    with kbc.connect_closing() as conn:
        (a, wa), (b, wb), (c, wc), (d, wd) = _chain(conn, 4)
        assert kb.archive_task(conn, d)
        assert not wd.exists()
        _complete(conn, a)
        assert wa.is_dir()  # B open
        _complete(conn, b)
        assert not wa.exists()
        assert wb.is_dir()  # C open
        _complete(conn, c)
    assert not wb.exists()
    assert not wc.exists()


def test_three_deep_chain_middle_archived_before_parent_completes(kanban_home: Path) -> None:
    """B archived (terminal) while A is still running: A is untouched until A finishes."""
    with kbc.connect_closing() as conn:
        (a, wa), (b, wb), (c, wc) = _chain(conn, 3)
        assert kb.claim_task(conn, a) is not None
        assert kb.archive_task(conn, b)
        assert wa.is_dir()  # live owner of its own workspace
        assert wb.is_dir()  # C still open
        assert kb.archive_task(conn, c)
        assert wa.is_dir()
        assert not wb.exists()
        assert not wc.exists()
        assert kb.get_task(conn, a).status == "running"


# ---------------------------------------------------------------------------
# Diamonds
# ---------------------------------------------------------------------------


def test_diamond_parent_waits_for_both_branches(kanban_home: Path) -> None:
    """A -> {B, C} -> D."""
    with kbc.connect_closing() as conn:
        a, wa = _scratch_task(conn, "a")
        b, wb = _scratch_task(conn, "b", parents=(a,))
        c, wc = _scratch_task(conn, "c", parents=(a,))
        d, wd = _scratch_task(conn, "d", parents=(b, c))
        _complete(conn, a)
        _complete(conn, b)
        assert wa.is_dir()  # C still open
        _complete(conn, c)
        assert not wa.exists()
        assert wb.is_dir() and wc.is_dir()  # D still open
        _complete(conn, d)
    assert not any(ws.exists() for ws in (wb, wc, wd))


def test_diamond_with_missed_hooks_is_swept_once(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        a, wa = _scratch_task(conn, "a")
        b, wb = _scratch_task(conn, "b", parents=(a,))
        c, wc = _scratch_task(conn, "c", parents=(a,))
        d, wd = _scratch_task(conn, "d", parents=(b, c))
        for tid in (a, b, c):
            _mark_done_without_hook(conn, tid)
        _complete(conn, d)
    assert not any(ws.exists() for ws in (wa, wb, wc, wd))


# ---------------------------------------------------------------------------
# Guards survive the walk
# ---------------------------------------------------------------------------


def test_shared_ancestor_workspace_still_deferred(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        (a, wa), (b, wb), (c, _wc) = _chain(conn, 3)
        sharer = kb.create_task(conn, title="live sharer")
        kbw.set_workspace_path(conn, sharer, wa)
        for tid in (a, b):
            _mark_done_without_hook(conn, tid)
        _complete(conn, c)
        events = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind=?",
            (a, "workspace_cleanup_deferred_shared"),
        ).fetchone()[0]
    assert (wa / "handoff.txt").is_file()
    assert not wb.exists()
    assert events == 1


def test_unmanaged_ancestor_path_still_refused(kanban_home: Path, tmp_path: Path) -> None:
    user_src = tmp_path / "user-src"
    user_src.mkdir()
    (user_src / "main.py").write_text("print('mine')\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        (a, _wa), (b, wb), (c, _wc) = _chain(conn, 3)
        kbw.set_workspace_path(conn, a, user_src)
        for tid in (a, b):
            _mark_done_without_hook(conn, tid)
        _complete(conn, c)
    assert (user_src / "main.py").is_file()
    assert not wb.exists()


def _worktree_task(conn, repo: Path, title: str) -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title)
    wt = repo / ".worktrees" / tid
    kbw._ensure_git_worktree(repo, wt, f"wt/{tid}")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? "
            "WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


@pytest.mark.parametrize("dirty", [False, True])
def test_worktree_ancestor_reaped_only_when_clean(
    kanban_home: Path, repo: Path, dirty: bool
) -> None:
    with kbc.connect_closing() as conn:
        a, wt = _worktree_task(conn, repo, "worktree root")
        b, wb = _scratch_task(conn, "b", parents=(a,))
        c, _wc = _scratch_task(conn, "c", parents=(b,))
        if dirty:
            (wt / "uncommitted.txt").write_text("work", encoding="utf-8")
        for tid in (a, b):
            _mark_done_without_hook(conn, tid)
        _complete(conn, c)
    assert not wb.exists()
    assert wt.is_dir() is dirty


# ---------------------------------------------------------------------------
# kanban gc backstop for done tasks
# ---------------------------------------------------------------------------


def test_gc_sweeps_done_workspaces_a_hook_missed(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        done, w_done = _scratch_task(conn, "done, hook missed")
        _mark_done_without_hook(conn, done)
    assert _gc() == 0
    assert not w_done.exists()


def test_gc_keeps_done_workspace_an_active_child_needs(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        parent, w_parent = _scratch_task(conn, "parent")
        _scratch_task(conn, "child", parents=(parent,))
        _mark_done_without_hook(conn, parent)
    assert _gc() == 0
    assert (w_parent / "handoff.txt").is_file()


@pytest.mark.parametrize("status", ["todo", "ready", "running", "blocked", "review"])
def test_gc_never_touches_live_task_workspaces(kanban_home: Path, status: str) -> None:
    with kbc.connect_closing() as conn:
        tid, ws = _scratch_task(conn, status)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    assert _gc() == 0
    assert (ws / "handoff.txt").is_file()


def test_gc_refuses_unmanaged_done_scratch_path(kanban_home: Path, tmp_path: Path) -> None:
    user_src = tmp_path / "user-src"
    user_src.mkdir()
    (user_src / "main.py").write_text("print('mine')\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="mispointed scratch")
        kbw.set_workspace_path(conn, tid, user_src)
        _mark_done_without_hook(conn, tid)
    assert _gc() == 0
    assert (user_src / "main.py").is_file()


def test_gc_reports_removed_done_workspaces(kanban_home: Path, capsys) -> None:
    with kbc.connect_closing() as conn:
        for i in range(2):
            tid, _ws = _scratch_task(conn, f"done {i}")
            _mark_done_without_hook(conn, tid)
    assert _gc() == 0
    assert "GC complete: 2 workspace(s)" in capsys.readouterr().out
