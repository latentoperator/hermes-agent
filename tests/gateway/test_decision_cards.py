"""Decision Cards queue admission and action semantics."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from gateway import decision_cards as dc


def _card(**overrides) -> dict[str, Any]:
    data: dict[str, Any] = {
        "question": "Should Dante send the review branch to Chris?",
        "context": "The branch passed tests.\nIt changes only the review helper.",
        "default_action": "Wait until tomorrow morning",
        "fire_at": "2026-07-03 07:00 CT",
        "requested_by": "Dante morning pulse",
        "source_ref": "kanban:t_demo",
        "originating_profile": "dante",
    }
    data.update(overrides)
    return data


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    path = tmp_path / "decision_cards.db"
    monkeypatch.setenv("HERMES_DECISION_QUEUE_DB", str(path))
    return path


def test_create_card_enforces_plain_english_admission(queue_db):
    card = dc.create_card(**_card())
    assert card.id.startswith("dc_")
    assert card.status == "pending"
    assert card.context.count("\n") == 1
    assert dc.default_db_path() == queue_db

    with pytest.raises(dc.DecisionCardError, match="at most 3"):
        dc.create_card(**_card(context="one\ntwo\nthree\nfour"))

    with pytest.raises(dc.DecisionCardError, match="one line"):
        dc.create_card(**_card(question="Line one\nLine two"))


def test_protected_write_card_requires_exact_visible_payload(queue_db, tmp_path):
    agents = str((tmp_path / "AGENTS.md").resolve())
    claude = str((tmp_path / "CLAUDE.md").resolve())
    yes_action = "write exactly the listed instruction files once"
    payload = {
        "action": dc.PROTECTED_INSTRUCTION_WRITE_ACTION,
        "task_id": "t_exact",
        "paths": [agents, claude],
        "yes_action": yes_action,
    }

    card = dc.create_card(
        **_card(
            context=f"Task t_exact — Yes: {yes_action}\n{agents}\n{claude}",
            action_kind=dc.PROTECTED_INSTRUCTION_WRITE_ACTION_KIND,
            action_payload=payload,
        )
    )
    assert card.action_kind == dc.PROTECTED_INSTRUCTION_WRITE_ACTION_KIND

    with pytest.raises(dc.DecisionCardError, match="exact absolute path"):
        dc.create_card(
            **_card(
                context=f"Task t_exact — Yes: {yes_action}\n{agents}.bak\n{claude}",
                action_kind=dc.PROTECTED_INSTRUCTION_WRITE_ACTION_KIND,
                action_payload=payload,
                source_ref="kanban:t_missing_visible_path",
            )
        )

    with pytest.raises(dc.DecisionCardError, match="task_id and yes_action"):
        dc.create_card(
            **_card(
                context=f"{agents}\n{claude}",
                action_kind=dc.PROTECTED_INSTRUCTION_WRITE_ACTION_KIND,
                action_payload=payload,
                source_ref="kanban:t_hidden_task",
            )
        )

    malformed = dict(payload, paths=["AGENTS.md"])
    with pytest.raises(dc.DecisionCardError, match="absolute"):
        dc.create_card(
            **_card(
                context="AGENTS.md",
                action_kind=dc.PROTECTED_INSTRUCTION_WRITE_ACTION_KIND,
                action_payload=malformed,
                source_ref="kanban:t_relative_path",
            )
        )


def test_claim_rejects_non_allowlisted_button_actor(queue_db, tmp_path, monkeypatch):
    agents = str((tmp_path / "AGENTS.md").resolve())
    yes_action = "write exactly the listed instruction file once"
    card = dc.create_card(
        **_card(
            context=f"Task t_actor — Yes: {yes_action}\n{agents}",
            action_kind=dc.PROTECTED_INSTRUCTION_WRITE_ACTION_KIND,
            action_payload={
                "action": dc.PROTECTED_INSTRUCTION_WRITE_ACTION,
                "task_id": "t_actor",
                "paths": [agents],
                "yes_action": yes_action,
            },
        )
    )
    dc.handle_action(
        card.id,
        "yes",
        actor="Not Chris",
        actor_id="99",
        actor_platform="discord",
    )
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")

    assert dc.claim_protected_instruction_write(
        task_id="t_actor",
        profile="dante",
        paths=[agents],
    ) is None


def test_claim_accepts_legacy_button_event_from_delivered_platform(
    queue_db, tmp_path, monkeypatch
):
    agents = str((tmp_path / "AGENTS.md").resolve())
    yes_action = "write exactly the listed instruction file once"
    card = dc.create_card(
        **_card(
            context=f"Task t_legacy — Yes: {yes_action}\n{agents}",
            action_kind=dc.PROTECTED_INSTRUCTION_WRITE_ACTION_KIND,
            action_payload={
                "action": dc.PROTECTED_INSTRUCTION_WRITE_ACTION,
                "task_id": "t_legacy",
                "paths": [agents],
                "yes_action": yes_action,
            },
        )
    )
    dc.record_delivery(card.id, platform="discord", message_id="m1")
    dc.handle_action(card.id, "yes", actor="Chris", actor_id="42")
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")

    claimed = dc.claim_protected_instruction_write(
        task_id="t_legacy",
        profile="dante",
        paths=[agents],
    )
    assert claimed is not None and claimed.id == card.id


def test_format_card_text_contains_contract_fields(queue_db):
    card = dc.create_card(**_card())
    text = dc.format_card_text(card)
    assert "Decision needed: Should Dante send" in text
    assert "Default if you do nothing: Wait until tomorrow morning" in text
    assert "Fire time: 2026-07-03 07:00 CT" in text
    assert "Asked by: Dante morning pulse (kanban:t_demo)" in text


def test_handle_action_updates_audit_trail(queue_db):
    card = dc.create_card(**_card())
    answered, receipt = dc.handle_action(card.id, "yes", actor="Chris", actor_id="u1")

    assert answered.status == "answered_yes"
    assert answered.answer == "yes"
    assert answered.answered_by == "Chris"
    assert "Yes recorded" in receipt

    with sqlite3.connect(queue_db) as conn:
        events = conn.execute(
            "SELECT event_type, actor FROM decision_card_events WHERE card_id = ? ORDER BY id",
            (card.id,),
        ).fetchall()
    assert events == [("created", "Dante morning pulse"), ("button_yes", "Chris")]


def test_info_action_keeps_card_explainable(queue_db):
    card = dc.create_card(**_card(explanation="This is the friendly explanation."))
    updated, receipt = dc.handle_action(card.id, "info", actor="Chris")

    assert updated.status == "info_requested"
    assert updated.answer == "info"
    assert receipt == "This is the friendly explanation."


def test_parse_custom_id_rejects_non_decision():
    assert dc.parse_custom_id("decision:dc_123:wait") == ("dc_123", "wait")
    with pytest.raises(dc.DecisionCardError):
        dc.parse_custom_id("clarify:abc:0")
