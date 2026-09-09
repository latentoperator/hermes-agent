"""Resume one exact parked Kanban workflow after a Decision Card Yes.

This is a control-plane transition only: it can make the recorded Kanban task
runnable and re-enable its recorded supervisor.  It never claims a worker or
runs the task's external/domain action.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cron import jobs as cron_jobs
from hermes_constants import get_default_hermes_root
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

WORKFLOW_RESUME_ACTION_KIND = "kanban_workflow_resume"
WORKFLOW_RESUME_ACTION = "resume_parked_kanban_workflow"
_EXPECTED_KEYS = {
    "action",
    "board",
    "task_id",
    "tenant",
    "source_event_id",
    "supervisor_profile",
    "supervisor_job_id",
    "supervisor_prompt_sha256",
    "expires_at",
    "yes_action",
}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkflowContinuationError(RuntimeError):
    """The recorded workflow cannot be resumed safely."""


class _WorkflowReconciliationRequired(RuntimeError):
    """A cross-store side effect may have committed and must be reconciled."""


@dataclass(frozen=True)
class WorkflowContinuationResult:
    status: str
    receipt: str
    details: dict[str, Any]


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise WorkflowContinuationError("expires_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _payload(raw_payload: str | dict[str, Any]) -> dict[str, Any]:
    raw = raw_payload if isinstance(raw_payload, str) else json.dumps(raw_payload, sort_keys=True)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowContinuationError("action_payload is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _EXPECTED_KEYS:
        raise WorkflowContinuationError("workflow resume payload does not match the exact schema")
    if value.get("action") != WORKFLOW_RESUME_ACTION:
        raise WorkflowContinuationError("workflow resume action is invalid")
    for key in ("board", "task_id", "tenant", "supervisor_profile", "supervisor_job_id", "yes_action"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise WorkflowContinuationError(f"{key} must be a non-empty string")
        value[key] = value[key].strip()
    if not isinstance(value.get("source_event_id"), int) or value["source_event_id"] <= 0:
        raise WorkflowContinuationError("source_event_id must be a positive integer")
    if not _NAME_RE.fullmatch(value["board"]):
        raise WorkflowContinuationError("board is not canonical")
    if not _NAME_RE.fullmatch(value["supervisor_profile"]):
        raise WorkflowContinuationError("supervisor_profile is not canonical")
    if not _HASH_RE.fullmatch(str(value.get("supervisor_prompt_sha256") or "")):
        raise WorkflowContinuationError("supervisor_prompt_sha256 must be a lowercase SHA-256")
    _utc(str(value.get("expires_at") or ""))
    return value


def validate_workflow_resume_candidate(
    *,
    action_payload: str | dict[str, Any],
    question: str,
    context: str,
    fire_at: str,
    source_ref: str,
    originating_profile: str,
) -> dict[str, Any]:
    """Validate immutable payload/body bindings before a card is created or run."""
    action = _payload(action_payload)
    if originating_profile != action["supervisor_profile"]:
        raise WorkflowContinuationError("originating_profile does not match supervisor_profile")
    if not source_ref.startswith(f"kanban:{action['task_id']}:"):
        raise WorkflowContinuationError("source_ref does not bind the exact task_id")
    if _utc(fire_at) != _utc(action["expires_at"]):
        raise WorkflowContinuationError("expires_at must match the visible card fire time")
    visible = {line.strip() for line in f"{question}\n{context}".splitlines() if line.strip()}
    required = {
        f"Task {action['task_id']} on board {action['board']} for tenant {action['tenant']}",
        f"Supervisor {action['supervisor_profile']}/{action['supervisor_job_id']}",
        f"Yes: {action['yes_action']}",
    }
    if not required.issubset(visible):
        raise WorkflowContinuationError("task, supervisor, and Yes action must be exactly visible on the card")
    return action


def validate_workflow_resume_card(card) -> dict[str, Any]:
    return validate_workflow_resume_candidate(
        action_payload=card.action_payload,
        question=card.question,
        context=card.context,
        fire_at=card.fire_at,
        source_ref=card.source_ref,
        originating_profile=card.originating_profile,
    )


def _record(conn: sqlite3.Connection, card_id: str, event_type: str, details: dict[str, Any]) -> None:
    from gateway.decision_cards import record_event

    record_event(conn, card_id, event_type, actor="decision-workflow-continuation", details=details)


def _supervisor_profile_home(profile: str) -> Path:
    root = get_default_hermes_root()
    return root if profile in {"default", "hermes"} else root / "profiles" / profile


def _validate_source_stop(
    board_conn: sqlite3.Connection,
    *,
    task_id: str,
    task_status: str,
    source_event_id: int,
) -> None:
    latest = board_conn.execute(
        "SELECT id,kind FROM task_events WHERE task_id=? "
        "AND kind IN ('blocked','block_loop_detected') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest is None or int(latest["id"]) != source_event_id:
        raise WorkflowContinuationError("source task stop event is stale or mismatched")
    if task_status in {"blocked", "triage"}:
        expected_kind = "block_loop_detected" if task_status == "triage" else "blocked"
        if latest["kind"] != expected_kind:
            raise WorkflowContinuationError("source task stop kind does not match its status")


def _failure(
    conn: sqlite3.Connection,
    card,
    message: str,
    details: dict[str, Any],
    *,
    reconciliation_required: bool = False,
) -> WorkflowContinuationResult:
    receipt = f"Approval recorded, but the exact parked workflow was not resumed: {message}. No client action was executed."
    payload = {
        **details,
        "reason": message,
        "reconciliation_required": reconciliation_required,
        "external_action_executed": False,
        "worker_claimed": False,
    }
    _record(conn, card.id, "workflow_resume_failed", payload)
    conn.execute("UPDATE decision_cards SET receipt=?, updated_at=? WHERE id=?", (receipt, datetime.now(timezone.utc).isoformat(timespec="seconds"), card.id))
    conn.commit()
    return WorkflowContinuationResult("failed", receipt, payload)


def _approved_actor(conn: sqlite3.Connection, card) -> dict[str, str]:
    from gateway.decision_cards import _allowed_decision_card_actor_ids

    row = conn.execute(
        "SELECT actor,details_json FROM decision_card_events "
        "WHERE card_id=? AND event_type='button_yes' ORDER BY id DESC LIMIT 1",
        (card.id,),
    ).fetchone()
    if row is None or str(row["actor"] or "").strip() != card.answered_by:
        raise WorkflowContinuationError("button actor does not match answered_by")
    try:
        details = json.loads(str(row["details_json"] or "{}"))
    except json.JSONDecodeError:
        raise WorkflowContinuationError("button actor event is malformed")
    if not isinstance(details, dict):
        raise WorkflowContinuationError("button actor event is malformed")
    platform = str(details.get("actor_platform") or "").strip().lower()
    actor_id = str(details.get("actor_id") or "").strip()
    if not platform or actor_id not in _allowed_decision_card_actor_ids(platform):
        raise WorkflowContinuationError("button actor is not explicitly allowlisted")
    return {"approved_actor_id": actor_id, "approved_actor_platform": platform}


def continue_answered_workflow(
    card_id: str, *, decision_conn: sqlite3.Connection | None = None
) -> WorkflowContinuationResult:
    """Apply the dedicated workflow-resume action once and persist its outcome."""
    from gateway import decision_cards as dc

    connection_scope = dc.connect() if decision_conn is None else nullcontext(decision_conn)
    with connection_scope as decision_conn:
        card = dc.get_card(card_id, conn=decision_conn)
        if card.action_kind != WORKFLOW_RESUME_ACTION_KIND:
            return WorkflowContinuationResult(
                "not_applicable",
                card.receipt,
                {"worker_claimed": False, "external_action_executed": False},
            )
        if card.status != "answered_yes" or card.answer != "yes":
            return WorkflowContinuationResult(
                "not_ready",
                card.receipt,
                {"worker_claimed": False, "external_action_executed": False},
            )
        decision_conn.execute("BEGIN IMMEDIATE")
        card = dc.get_card(card_id, conn=decision_conn)
        prior = decision_conn.execute(
            "SELECT event_type,details_json FROM decision_card_events "
            "WHERE card_id=? AND event_type IN ('workflow_resume_succeeded','workflow_resume_failed') "
            "ORDER BY id DESC LIMIT 1",
            (card_id,),
        ).fetchone()
        if prior is not None:
            details = json.loads(str(prior["details_json"] or "{}"))
            details = details if isinstance(details, dict) else {}
            # Replaying the same terminal Yes is reconciliation, not new authority:
            # immutable failures stay final, while uncertain cross-store effects
            # are inspected again until task and supervisor converge.
            if (
                prior["event_type"] == "workflow_resume_succeeded"
                or details.get("reconciliation_required") is not True
            ):
                decision_conn.commit()
                return WorkflowContinuationResult(
                    "succeeded"
                    if prior["event_type"] == "workflow_resume_succeeded"
                    else "failed",
                    card.receipt,
                    details,
                )
        claimed = decision_conn.execute(
            "SELECT 1 FROM decision_card_events WHERE card_id=? "
            "AND event_type='workflow_resume_claimed' LIMIT 1",
            (card_id,),
        ).fetchone()
        if claimed is None:
            _record(
                decision_conn,
                card_id,
                "workflow_resume_claimed",
                {"approved_by": card.answered_by, "approved_at": card.answered_at},
            )
        reconciliation_started = False
        try:
            action = validate_workflow_resume_card(card)
            if card.status != "answered_yes" or card.answer != "yes":
                raise WorkflowContinuationError("decision is not answered Yes")
            if not card.answered_at or _utc(card.answered_at) > _utc(action["expires_at"]):
                raise WorkflowContinuationError("decision approval is expired")
            actor_details = _approved_actor(decision_conn, card)

            if not kb.board_exists(action["board"]):
                raise WorkflowContinuationError("recorded board does not exist")

            with kbc.connect_closing(board=action["board"]) as board_conn:
                task = kb.get_task(board_conn, action["task_id"])
                if task is None:
                    raise WorkflowContinuationError("source task is missing")
                if task.tenant != action["tenant"] or task.assignee != card.originating_profile:
                    raise WorkflowContinuationError("source task tenant or assignee does not match the card")
                task_before = task.status
                if task_before not in {
                    "blocked",
                    "triage",
                    "ready",
                    "running",
                    "done",
                    "archived",
                }:
                    raise WorkflowContinuationError(
                        f"source task status {task_before!r} is not resumable"
                    )
                _validate_source_stop(
                    board_conn,
                    task_id=task.id,
                    task_status=task_before,
                    source_event_id=action["source_event_id"],
                )
                if task_before in {"done", "archived"}:
                    details = {
                        "task_id": task.id,
                        "task_before": task_before,
                        "task_after": task_before,
                        "supervisor_changed": False,
                        "worker_claimed": False,
                        "external_action_executed": False,
                        "approved_by": card.answered_by,
                        "approved_at": card.answered_at,
                        **actor_details,
                    }
                    receipt = "Approval recorded; the source task is already terminal, so nothing was resumed. No client action was executed."
                    _record(decision_conn, card.id, "workflow_resume_succeeded", details)
                    decision_conn.execute("UPDATE decision_cards SET receipt=? WHERE id=?", (receipt, card.id))
                    decision_conn.commit()
                    return WorkflowContinuationResult("succeeded", receipt, details)
                if task_before in {"blocked", "triage"}:
                    unsatisfied = board_conn.execute(
                        "SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id "
                        "WHERE l.child_id=? AND p.status NOT IN ('done','archived') LIMIT 1",
                        (task.id,),
                    ).fetchone()
                    if unsatisfied:
                        raise WorkflowContinuationError("source task still has an unfinished parent")

            profile_home = _supervisor_profile_home(action["supervisor_profile"])
            reconciliation_started = True
            with cron_jobs.use_cron_store(profile_home):
                supervisor = cron_jobs.get_job(action["supervisor_job_id"])
                if supervisor is None:
                    raise WorkflowContinuationError("recorded supervisor is missing")
                prompt = str(supervisor.get("prompt") or "")
                prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
                if prompt_hash != action["supervisor_prompt_sha256"] or action["task_id"] not in prompt or action["tenant"] not in prompt:
                    raise WorkflowContinuationError("recorded supervisor scope does not match the card")
                supervisor_before = str(supervisor.get("state") or "")
                supervisor_snapshot = {
                    key: supervisor.get(key)
                    for key in (
                        "enabled",
                        "state",
                        "paused_at",
                        "paused_reason",
                        "next_run_at",
                        "manual_run_at",
                    )
                }
                supervisor_changed = not cron_jobs.is_job_runnable(supervisor)
                if supervisor_changed:
                    supervisor = cron_jobs.resume_job(action["supervisor_job_id"])
                    if supervisor is None or not cron_jobs.is_job_runnable(supervisor):
                        raise _WorkflowReconciliationRequired(
                            "supervisor resume readback failed"
                        )

            try:
                with kbc.connect_closing(board=action["board"]) as board_conn:
                    current = kb.get_task(board_conn, action["task_id"])
                    if current is None:
                        raise WorkflowContinuationError("source task disappeared before continuation")
                    _validate_source_stop(
                        board_conn,
                        task_id=current.id,
                        task_status=current.status,
                        source_event_id=action["source_event_id"],
                    )
                    if current.status in {"blocked", "triage"}:
                        try:
                            resumed, reason = kb.resume_task_from_decision(
                                board_conn,
                                current.id,
                                decision_card_id=card.id,
                                source_event_id=action["source_event_id"],
                                approved_by=card.answered_by,
                                approved_at=card.answered_at,
                            )
                        except Exception as exc:
                            raise _WorkflowReconciliationRequired(
                                str(exc).strip() or type(exc).__name__
                            ) from exc
                        if not resumed:
                            raise WorkflowContinuationError(reason or "source task continuation failed")
                    try:
                        current = kb.get_task(board_conn, current.id)
                    except Exception as exc:
                        raise _WorkflowReconciliationRequired(
                            str(exc).strip() or type(exc).__name__
                        ) from exc
                    if current is None or current.status not in {"ready", "running"}:
                        raise _WorkflowReconciliationRequired(
                            "source task transition readback failed"
                        )
                    task_after = current.status
            except Exception:
                if supervisor_changed:
                    with cron_jobs.use_cron_store(profile_home):
                        rolled_back = cron_jobs.update_job(
                            action["supervisor_job_id"], supervisor_snapshot
                        )
                        if rolled_back is None or any(
                            rolled_back.get(key) != value
                            for key, value in supervisor_snapshot.items()
                        ):
                            raise _WorkflowReconciliationRequired(
                                "source task transition failed and supervisor rollback readback failed"
                            )
                raise

            details = {
                "board": action["board"],
                "tenant": action["tenant"],
                "task_id": action["task_id"],
                "source_event_id": action["source_event_id"],
                "task_before": task_before,
                "task_after": task_after,
                "supervisor_profile": action["supervisor_profile"],
                "supervisor_job_id": action["supervisor_job_id"],
                "supervisor_before": supervisor_before,
                "supervisor_after": str(supervisor.get("state") or ""),
                "supervisor_changed": supervisor_changed,
                "approved_by": card.answered_by,
                "approved_at": card.answered_at,
                **actor_details,
                "worker_claimed": False,
                "external_action_executed": False,
            }
            receipt = (
                f"Exact parked workflow resumed: task {action['task_id']} is {task_after} and supervisor "
                f"{action['supervisor_profile']}/{action['supervisor_job_id']} is scheduled. "
                "No worker was claimed and no client action was executed; those require separate evidence."
            )
            _record(decision_conn, card.id, "workflow_resume_succeeded", details)
            decision_conn.execute("UPDATE decision_cards SET receipt=?, updated_at=? WHERE id=?", (receipt, datetime.now(timezone.utc).isoformat(timespec="seconds"), card.id))
            decision_conn.commit()
            return WorkflowContinuationResult("succeeded", receipt, details)
        except _WorkflowReconciliationRequired as exc:
            message = str(exc).strip() or type(exc).__name__
            return _failure(
                decision_conn,
                card,
                message,
                {"approved_by": card.answered_by, "approved_at": card.answered_at},
                reconciliation_required=True,
            )
        except WorkflowContinuationError as exc:
            message = str(exc).strip() or type(exc).__name__
            return _failure(
                decision_conn,
                card,
                message,
                {"approved_by": card.answered_by, "approved_at": card.answered_at},
            )
        except Exception as exc:  # durable fail-closed receipt for every post-approval fault
            message = str(exc).strip() or type(exc).__name__
            return _failure(
                decision_conn,
                card,
                message,
                {"approved_by": card.answered_by, "approved_at": card.answered_at},
                reconciliation_required=reconciliation_started,
            )
