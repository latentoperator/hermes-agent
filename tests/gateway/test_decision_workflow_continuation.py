"""Decision Card continuation of an exact parked Kanban workflow."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from agent import secret_scope
from cron import jobs as cron_jobs
from gateway import decision_cards as dc
from gateway import decision_workflow_continuation as continuation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _setup_parked_workflow(
    tmp_path: Path, monkeypatch, *, supervisor_profile: str = "virgil"
):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_DECISION_QUEUE_DB", str(root / "decision_cards.db"))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")

    with kbc.connect_closing(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title="parked workflow",
            assignee=supervisor_profile,
            tenant="tenant-a",
        )
        assert kb.block_task(conn, task_id, reason="waiting for exact approval")
        source_event_id = int(
            conn.execute(
                "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='blocked'",
                (task_id,),
            ).fetchone()[0]
        )
        initial_runs = int(
            conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
        )

    profile_home = (
        root
        if supervisor_profile in {"default", "hermes"}
        else root / "profiles" / supervisor_profile
    )
    profile_home.mkdir(parents=True, exist_ok=True)
    prompt = f"Supervise task {task_id} for tenant tenant-a without doing its work."
    with cron_jobs.use_cron_store(profile_home):
        job = cron_jobs.create_job(prompt=prompt, schedule="every 15m", name="exact supervisor")
        cron_jobs.pause_job(job["id"], reason="parked for Decision Card")

    yes_action = "resume the exact parked workflow"
    card = dc.create_card(
        question="Resume this exact parked workflow?",
        context=(
            f"Task {task_id} on board default for tenant tenant-a\n"
            f"Supervisor {supervisor_profile}/{job['id']}\n"
            f"Yes: {yes_action}"
        ),
        default_action="Keep the task blocked and its supervisor paused",
        fire_at="2099-01-01T00:00:00+00:00",
        requested_by="Virgil",
        source_ref=f"kanban:{task_id}:exact-stop",
        originating_profile=supervisor_profile,
        action_kind="kanban_workflow_resume",
        action_payload={
            "action": "resume_parked_kanban_workflow",
            "board": "default",
            "task_id": task_id,
            "tenant": "tenant-a",
            "source_event_id": source_event_id,
            "supervisor_profile": supervisor_profile,
            "supervisor_job_id": job["id"],
            "supervisor_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "expires_at": "2099-01-01T00:00:00+00:00",
            "yes_action": yes_action,
        },
    )
    return root, profile_home, task_id, job["id"], card, initial_runs


def _rewrite_payload(root: Path, card_id: str, **changes) -> None:
    with sqlite3.connect(root / "decision_cards.db") as conn:
        raw = conn.execute(
            "SELECT action_payload FROM decision_cards WHERE id=?", (card_id,)
        ).fetchone()[0]
        payload = json.loads(raw)
        payload.update(changes)
        conn.execute(
            "UPDATE decision_cards SET action_payload=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), card_id),
        )


def _workflow_event_count(root: Path, card_id: str, event_type: str) -> int:
    with sqlite3.connect(root / "decision_cards.db") as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM decision_card_events WHERE card_id=? AND event_type=?",
                (card_id, event_type),
            ).fetchone()[0]
        )


def _answer_yes_without_continuation(root: Path, card_id: str) -> dc.DecisionCard:
    now = "2026-09-09T18:51:59+00:00"
    with dc.connect(root / "decision_cards.db") as conn:
        conn.execute(
            "UPDATE decision_cards SET status='answered_yes', answer='yes', "
            "answered_by='Chris', answered_at=?, updated_at=? WHERE id=?",
            (now, now, card_id),
        )
        dc.record_event(
            conn,
            card_id,
            "button_yes",
            actor="Chris",
            details={"actor_id": "42", "actor_platform": "telegram"},
        )
        conn.commit()
        return dc.get_card(card_id, conn=conn)


def _record_orphan_claim(root: Path, card_id: str) -> None:
    with dc.connect(root / "decision_cards.db") as conn:
        dc.record_event(
            conn,
            card_id,
            "workflow_resume_claimed",
            actor="decision-workflow-continuation",
            details={"approved_by": "Chris"},
        )
        conn.commit()


def test_answered_yes_resumes_exact_task_and_supervisor_without_claiming_worker_or_external_action(
    tmp_path, monkeypatch
):
    root, profile_home, task_id, job_id, card, initial_runs = _setup_parked_workflow(
        tmp_path, monkeypatch
    )

    answered, receipt = dc.handle_action(
        card.id,
        "yes",
        actor="Chris",
        actor_id="42",
        actor_platform="telegram",
    )

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.current_run_id is None
        assert (
            conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
            == initial_runs
        )
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
    assert supervisor is not None
    assert supervisor["enabled"] is True
    assert supervisor["state"] == "scheduled"

    assert answered.answered_by == "Chris"
    assert answered.answered_at
    assert "no client action was executed" in receipt.lower()
    with sqlite3.connect(root / "decision_cards.db") as conn:
        row = conn.execute(
            "SELECT details_json FROM decision_card_events "
            "WHERE card_id=? AND event_type='workflow_resume_succeeded'",
            (card.id,),
        ).fetchone()
    assert row is not None
    assert '"worker_claimed": false' in row[0]
    assert '"external_action_executed": false' in row[0]


def test_answered_yes_resumes_exact_block_loop_triage_source(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    with kbc.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (task_id,))
            kb._append_event(
                conn,
                task_id,
                "block_loop_detected",
                {"reason": "exact approval stop", "source_status": "ready"},
            )
        source_event_id = int(
            conn.execute(
                "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='block_loop_detected'",
                (task_id,),
            ).fetchone()[0]
        )
    _rewrite_payload(root, card.id, source_event_id=source_event_id)

    _, receipt = dc.handle_action(
        card.id,
        "yes",
        actor="Chris",
        actor_id="42",
        actor_platform="telegram",
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "ready"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "scheduled"
    assert "workflow resumed" in receipt.lower()


def test_pending_card_does_not_resume_anything(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
    assert supervisor is not None and supervisor["state"] == "paused"
    assert dc.get_card(card.id).status == "pending"
    assert _workflow_event_count(root, card.id, "workflow_resume_succeeded") == 0


def test_no_decision_keeps_exact_workflow_parked(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    answered, _ = dc.handle_action(
        card.id, "no", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert answered.status == "answered_no"
    assert _workflow_event_count(root, card.id, "workflow_resume_claimed") == 0


def test_legacy_generic_yes_records_authority_but_does_not_resume(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, _, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    card = dc.create_card(
        question="Continue the parked work?",
        context="Approval is needed before the next step.",
        default_action="Keep the workflow parked",
        fire_at="2099-01-01T00:00:00+00:00",
        requested_by="Virgil",
        source_ref=f"kanban:{task_id}:legacy-generic",
        originating_profile="virgil",
        action_kind="decision_card_yes_action",
        action_payload={"task_id": task_id, "yes_action": "continue the parked work"},
    )
    answered, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert answered.status == "answered_yes"
    assert "asking agent should now run" in receipt
    assert _workflow_event_count(root, card.id, "workflow_resume_claimed") == 0


def test_unallowlisted_yes_cannot_resume_workflow(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    _, receipt = dc.handle_action(
        card.id, "yes", actor="Impostor", actor_id="99", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert "not explicitly allowlisted" in receipt


def test_multiplex_actor_allowlist_uses_active_profile_scope_not_process_env(
    tmp_path, monkeypatch
):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "99")
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({"TELEGRAM_ALLOWED_USERS": "42"})
    try:
        _, receipt = dc.handle_action(
            card.id,
            "yes",
            actor="Chris",
            actor_id="42",
            actor_platform="telegram",
        )
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "ready"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "scheduled"
    assert "workflow resumed" in receipt.lower()


@pytest.mark.parametrize(
    ("scope", "actor_id"),
    [({}, "42"), ({"TELEGRAM_ALLOWED_USERS": "42"}, "99")],
)
def test_multiplex_actor_allowlist_fails_closed_on_scoped_miss_or_cross_profile_actor(
    tmp_path, monkeypatch, scope, actor_id
):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", actor_id)
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope(scope)
    try:
        _, receipt = dc.handle_action(
            card.id,
            "yes",
            actor="Wrong profile actor",
            actor_id=actor_id,
            actor_platform="telegram",
        )
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert "not explicitly allowlisted" in receipt


def test_duplicate_yes_delivery_is_idempotent(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    first, _ = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )
    with cron_jobs.use_cron_store(profile_home):
        first_job = cron_jobs.get_job(job_id)
    with kbc.connect_closing(board="default") as conn:
        first_task_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='decision_resumed'", (task_id,)
        ).fetchone()[0]

    second, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with cron_jobs.use_cron_store(profile_home):
        second_job = cron_jobs.get_job(job_id)
    with kbc.connect_closing(board="default") as conn:
        second_task_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='decision_resumed'", (task_id,)
        ).fetchone()[0]
    assert second.answered_at == first.answered_at
    assert second_job["next_run_at"] == first_job["next_run_at"]
    assert second_task_events == first_task_events == 1
    assert _workflow_event_count(root, card.id, "workflow_resume_succeeded") == 1
    assert "workflow resumed" in receipt.lower()


def test_retry_recovers_a_persisted_claim_left_before_cross_store_work(
    tmp_path, monkeypatch
):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path, monkeypatch
    )
    _answer_yes_without_continuation(root, card.id)
    _record_orphan_claim(root, card.id)

    _, receipt = dc.handle_action(
        card.id,
        "yes",
        actor="Chris",
        actor_id="42",
        actor_platform="telegram",
    )

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
        assert supervisor is not None and supervisor["state"] == "scheduled"
    assert "workflow resumed" in receipt.lower()
    assert _workflow_event_count(root, card.id, "workflow_resume_succeeded") == 1


def test_retry_reconciles_termination_after_supervisor_resume(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path, monkeypatch
    )
    _answer_yes_without_continuation(root, card.id)
    _record_orphan_claim(root, card.id)
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.resume_job(job_id)
        assert supervisor is not None and supervisor["state"] == "scheduled"

    result = continuation.continue_answered_workflow(card.id)

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
        assert supervisor is not None and supervisor["state"] == "scheduled"
    assert result.status == "succeeded"
    assert _workflow_event_count(root, card.id, "workflow_resume_succeeded") == 1


def test_retry_reconciles_termination_after_task_transition(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path, monkeypatch
    )
    answered = _answer_yes_without_continuation(root, card.id)
    _record_orphan_claim(root, card.id)
    payload = json.loads(answered.action_payload)
    with kbc.connect_closing(board="default") as conn:
        resumed, reason = kb.resume_task_from_decision(
            conn,
            task_id,
            decision_card_id=card.id,
            source_event_id=payload["source_event_id"],
            approved_by=answered.answered_by,
            approved_at=answered.answered_at,
        )
        assert resumed, reason

    result = continuation.continue_answered_workflow(card.id)

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='decision_resumed'",
            (task_id,),
        ).fetchone()[0] == 1
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
        assert supervisor is not None and supervisor["state"] == "scheduled"
    assert result.status == "succeeded"


@pytest.mark.parametrize(
    "termination_stage",
    ["after_claim", "after_supervisor_resume", "after_task_transition"],
)
def test_abrupt_termination_rolls_back_claim_and_duplicate_yes_converges(
    tmp_path, monkeypatch, termination_stage
):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path, monkeypatch
    )
    original_validate = continuation.validate_workflow_resume_card
    original_resume = continuation.kb.resume_task_from_decision

    if termination_stage == "after_claim":
        monkeypatch.setattr(
            continuation,
            "validate_workflow_resume_card",
            lambda *_: (_ for _ in ()).throw(SystemExit("terminated after claim")),
        )
    elif termination_stage == "after_supervisor_resume":
        monkeypatch.setattr(
            continuation.kb,
            "resume_task_from_decision",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                SystemExit("terminated after supervisor resume")
            ),
        )
    else:
        def resume_then_terminate(*args, **kwargs):
            resumed, reason = original_resume(*args, **kwargs)
            assert resumed, reason
            raise SystemExit("terminated after task transition")

        monkeypatch.setattr(
            continuation.kb,
            "resume_task_from_decision",
            resume_then_terminate,
        )

    with pytest.raises(SystemExit, match="terminated after"):
        dc.handle_action(
            card.id,
            "yes",
            actor="Chris",
            actor_id="42",
            actor_platform="telegram",
        )

    assert _workflow_event_count(root, card.id, "workflow_resume_claimed") == 0
    assert _workflow_event_count(root, card.id, "workflow_resume_succeeded") == 0
    monkeypatch.setattr(
        continuation, "validate_workflow_resume_card", original_validate
    )
    monkeypatch.setattr(
        continuation.kb, "resume_task_from_decision", original_resume
    )

    _, receipt = dc.handle_action(
        card.id,
        "yes",
        actor="Chris",
        actor_id="42",
        actor_platform="telegram",
    )

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='decision_resumed'",
            (task_id,),
        ).fetchone()[0] == 1
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
        assert supervisor is not None and supervisor["state"] == "scheduled"
    assert "workflow resumed" in receipt.lower()
    assert _workflow_event_count(root, card.id, "workflow_resume_claimed") == 1
    assert _workflow_event_count(root, card.id, "workflow_resume_succeeded") == 1


def test_expired_or_stale_approval_fails_closed(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    _rewrite_payload(root, card.id, expires_at="2000-01-01T00:00:00+00:00")
    with sqlite3.connect(root / "decision_cards.db") as conn:
        conn.execute(
            "UPDATE decision_cards SET fire_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", card.id),
        )

    answered, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert answered.answered_by == "Chris" and answered.answered_at
    assert "expired" in receipt.lower()
    assert _workflow_event_count(root, card.id, "workflow_resume_failed") == 1


def test_scope_mismatch_fails_closed(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    with kbc.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET tenant='tenant-b' WHERE id=?", (task_id,))

    _, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert "tenant or assignee" in receipt


def test_newer_stop_event_makes_decision_stale(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    with kbc.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "blocked", {"reason": "new stop cycle"})

    _, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert "stale or mismatched" in receipt


def test_newer_stop_event_stays_stale_after_task_is_running(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path, monkeypatch
    )
    with kbc.connect_closing(board="default") as conn:
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(conn, task_id, reason="new approval lifecycle")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?",
                (task_id,),
            )
        assert kb.claim_task(conn, task_id, claimer="new-lifecycle-worker") is not None

    _, receipt = dc.handle_action(
        card.id,
        "yes",
        actor="Chris",
        actor_id="42",
        actor_platform="telegram",
    )

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
        assert supervisor is not None and supervisor["state"] == "paused"
    assert "stale or mismatched" in receipt


@pytest.mark.parametrize("supervisor_profile", ["default", "hermes"])
def test_default_profile_aliases_resolve_the_root_cron_store(
    tmp_path, monkeypatch, supervisor_profile
):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(
        tmp_path,
        monkeypatch,
        supervisor_profile=supervisor_profile,
    )

    _, receipt = dc.handle_action(
        card.id,
        "yes",
        actor="Chris",
        actor_id="42",
        actor_platform="telegram",
    )

    assert profile_home == root
    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"
    with cron_jobs.use_cron_store(root):
        supervisor = cron_jobs.get_job(job_id)
        assert supervisor is not None and supervisor["state"] == "scheduled"
    assert "workflow resumed" in receipt.lower()


def test_parent_gated_source_stays_parked(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    with kbc.connect_closing(board="default") as conn:
        parent = kb.create_task(conn, title="unfinished parent", assignee="virgil")
        kb.link_tasks(conn, parent_id=parent, child_id=task_id)

    _, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert "unfinished parent" in receipt


def test_already_running_source_is_not_claimed_again(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    with kbc.connect_closing(board="default") as conn:
        assert kb.unblock_task(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer="test-worker")
        assert claimed is not None
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0]

    _, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0] == run_count
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "scheduled"
    assert "no worker was claimed" in receipt.lower()


def test_completed_source_is_not_reopened_and_supervisor_stays_paused(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    with kbc.connect_closing(board="default") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))

    _, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "done"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job_id)["state"] == "paused"
    assert "already terminal" in receipt.lower()


def test_missing_supervisor_is_visible_and_task_stays_blocked(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.remove_job(job_id)

    answered, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    assert "recorded supervisor is missing" in receipt
    assert "recorded supervisor is missing" in answered.receipt
    assert _workflow_event_count(root, card.id, "workflow_resume_failed") == 1


def test_task_transition_failure_rolls_supervisor_back_and_is_visible(tmp_path, monkeypatch):
    root, profile_home, task_id, job_id, card, _ = _setup_parked_workflow(tmp_path, monkeypatch)
    monkeypatch.setattr(
        continuation.kb,
        "resume_task_from_decision",
        lambda *args, **kwargs: (False, "injected task transition failure"),
    )

    _, receipt = dc.handle_action(
        card.id, "yes", actor="Chris", actor_id="42", actor_platform="telegram"
    )

    with kbc.connect_closing(board="default") as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    with cron_jobs.use_cron_store(profile_home):
        supervisor = cron_jobs.get_job(job_id)
    assert supervisor is not None and supervisor["state"] == "paused"
    assert "injected task transition failure" in receipt
