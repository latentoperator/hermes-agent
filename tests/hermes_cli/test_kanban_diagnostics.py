"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48






def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"


def test_stranded_in_ready_silent_below_threshold():
    """A ready task only 10 min old should NOT fire."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 10 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_non_ready_status():
    """Tasks not in ready status are out of scope (running tasks have
    their own crash / failure rules)."""
    now = 100_000
    for status in ("running", "blocked", "done", "todo", "triage"):
        task = _task(status=status, assignee="demo")
        events = [_event("created", ts=now - 6 * 3600)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        assert [d for d in diags if d.kind == "stranded_in_ready"] == [], status


def test_stranded_in_ready_skips_unassigned_tasks():
    """Empty assignee = `skipped_unassigned` on the dispatcher already.
    Don't double-flag here."""
    now = 100_000
    task = _task(status="ready", assignee="", claim_lock=None)
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_claimed_tasks():
    """A live claim_lock means a worker is on it — even an old one. Don't
    second-guess: the run-level liveness signal owns that decision."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", claim_lock="run_xyz",
    )
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_uses_latest_ready_transition():
    """When multiple ready-transition events exist, the rule should
    age-from the most recent — a task reclaimed 20 min ago is NOT
    stranded for 6h even if it was first created 6h ago."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [
        _event("created", ts=now - 6 * 3600),       # 6 h ago
        _event("reclaimed", ts=now - 20 * 60),      # 20 min ago — wins
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_severity_escalates_with_age():
    """warning → error → critical at 2x and 6x threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    # Default threshold = 1800s.
    cases = [
        (45 * 60, "warning"),    # 1.5x → warning
        (90 * 60, "error"),      # 3x → error
        (4 * 3600, "critical"),  # 8x → critical
    ]
    for age, expected in cases:
        events = [_event("created", ts=now - age)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1, f"age={age}"
        assert stranded[0].severity == expected, (
            f"age={age} expected {expected}, got {stranded[0].severity}"
        )


def test_stranded_in_ready_respects_config_override():
    """Config override changes the threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [_event("created", ts=now - 10 * 60)]  # 10 min
    # Default 30 min — wouldn't fire.
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []
    # Lower the threshold to 5 min — now it fires.
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
        config={"stranded_threshold_seconds": 5 * 60},
    )
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


def test_stranded_in_ready_falls_back_to_created_at():
    """When events have no ready-transition kind, the rule falls back
    to the task's ``created_at`` so an ancient stranded task isn't
    invisible just because its events got pruned."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", created_at=now - 4 * 3600,
    )
    # No qualifying events.
    events = [_event("commented", ts=now - 100)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].data["age_seconds"] == 4 * 3600


def test_stranded_in_ready_names_respawn_guard_reason():
    """Guarded ready tasks should name the guard, not guess about worker pools."""
    now = 100_000
    task = _task(status="ready", assignee="demo", id="t_guarded")
    events = [
        _event("created", ts=now - 4 * 3600),
        _event("respawn_guarded", ts=now - 60, reason="active_pr"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    d = stranded[0]
    assert "respawn guard" in d.title
    assert d.data["respawn_guard_reason"] == "active_pr"
    assert any("--ignore-guards t_guarded" in a.payload.get("command", "") for a in d.actions)


def test_stranded_in_ready_works_on_real_db_row(kanban_home):
    """Round-trip through real kanban_db.connect() — confirms the rule
    works on sqlite3.Row objects, not just dicts."""
    import time as _t
    conn = kb.connect()
    try:
        # Create a task and force its created_at into the past.
        tid = kb.create_task(conn, title="stranded one", assignee="ghost")
        old_ts = int(_t.time()) - 90 * 60  # 90 min old
        conn.execute(
            "UPDATE tasks SET status = 'ready', created_at = ? WHERE id = ?",
            (old_ts, tid),
        )
        conn.commit()

        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall())
        # Override created event timestamps too so age calc lines up.
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (old_ts, tid),
        )
        conn.commit()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ?", (tid,),
        ).fetchall())

        diags = kd.compute_task_diagnostics(task_row, events, [])
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1
        assert stranded[0].data["assignee"] == "ghost"
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
