"""Shared Decision Cards queue and Discord-facing helpers.

Decision Cards are durable, plain-English approval/decision prompts. The
storage path intentionally defaults to the *root* Hermes home
(``~/.hermes/decision_cards.db``), not a profile home, so multiple profiles can
write one shared queue while still keeping profile-local memories isolated.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ACTIONS = {"yes", "no", "wait", "info"}
ACTIVE_STATUSES = {"pending", "waiting", "info_requested"}


class DecisionCardError(ValueError):
    """Raised when a card fails admission or an action is invalid."""


@dataclass(frozen=True)
class DecisionCard:
    id: str
    status: str
    question: str
    context: str
    default_action: str
    fire_at: str
    requested_by: str
    source_ref: str = ""
    originating_profile: str = ""
    target_platform: str = "discord"
    target_chat_id: str = ""
    target_thread_id: str = ""
    message_id: str = ""
    action_kind: str = ""
    action_payload: str = ""
    explanation: str = ""
    created_at: str = ""
    updated_at: str = ""
    answered_at: str = ""
    answered_by: str = ""
    answer: str = ""
    receipt: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root_hermes_home() -> Path:
    raw = os.getenv("HERMES_DECISION_QUEUE_DB", "").strip()
    if raw:
        return Path(raw).expanduser().resolve().parent
    return Path.home() / ".hermes"


def default_db_path() -> Path:
    raw = os.getenv("HERMES_DECISION_QUEUE_DB", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _root_hermes_home() / "decision_cards.db"


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    db_path = Path(path).expanduser().resolve() if path else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS decision_cards (
          id TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'pending',
          question TEXT NOT NULL,
          context TEXT NOT NULL,
          default_action TEXT NOT NULL,
          fire_at TEXT NOT NULL,
          requested_by TEXT NOT NULL,
          source_ref TEXT NOT NULL DEFAULT '',
          originating_profile TEXT NOT NULL DEFAULT '',
          target_platform TEXT NOT NULL DEFAULT 'discord',
          target_chat_id TEXT NOT NULL DEFAULT '',
          target_thread_id TEXT NOT NULL DEFAULT '',
          message_id TEXT NOT NULL DEFAULT '',
          action_kind TEXT NOT NULL DEFAULT '',
          action_payload TEXT NOT NULL DEFAULT '',
          explanation TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          answered_at TEXT NOT NULL DEFAULT '',
          answered_by TEXT NOT NULL DEFAULT '',
          answer TEXT NOT NULL DEFAULT '',
          receipt TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS decision_card_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          card_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT '',
          details_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          FOREIGN KEY(card_id) REFERENCES decision_cards(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_decision_cards_status_created
          ON decision_cards(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_decision_card_events_card_created
          ON decision_card_events(card_id, created_at);
        """
    )
    conn.commit()


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _display_profile_name(profile: str) -> str:
    raw = str(profile or "").strip()
    if not raw:
        raise DecisionCardError("originating_profile is required for decision-card display")
    return " ".join(part.capitalize() for part in raw.replace("_", " ").replace("-", " ").split())


def validate_card(
    question: str,
    context: str,
    default_action: str,
    fire_at: str,
    requested_by: str,
    originating_profile: str = "",
) -> None:
    q = str(question or "").strip()
    if not q:
        raise DecisionCardError("question is required")
    if "\n" in q or "\r" in q:
        raise DecisionCardError("question must be one line")
    if len(q) > 220:
        raise DecisionCardError("question must stay plain and short (<=220 chars)")

    context_lines = _clean_lines(context)
    if not context_lines:
        raise DecisionCardError("context is required")
    if len(context_lines) > 3:
        raise DecisionCardError("context must be at most 3 non-empty lines")

    if not str(default_action or "").strip():
        raise DecisionCardError("default_action is required")
    if not str(fire_at or "").strip():
        raise DecisionCardError("fire_at is required")
    if not str(requested_by or "").strip():
        raise DecisionCardError("requested_by is required")
    _display_profile_name(originating_profile)


def _row_to_card(row: sqlite3.Row | dict[str, Any]) -> DecisionCard:
    data = dict(row)
    fields = DecisionCard.__dataclass_fields__.keys()
    return DecisionCard(**{key: str(data.get(key) or "") for key in fields})


def record_event(
    conn: sqlite3.Connection,
    card_id: str,
    event_type: str,
    *,
    actor: str = "",
    details: Optional[dict[str, Any]] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO decision_card_events(card_id, event_type, actor, details_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (card_id, event_type, actor, json.dumps(details or {}, sort_keys=True), _now()),
    )


def create_card(
    *,
    question: str,
    context: str,
    default_action: str,
    fire_at: str,
    requested_by: str,
    source_ref: str = "",
    originating_profile: str = "",
    action_kind: str = "",
    action_payload: str | dict[str, Any] = "",
    explanation: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> DecisionCard:
    validate_card(
        question,
        context,
        default_action,
        fire_at,
        requested_by,
        originating_profile,
    )
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        card_id = "dc_" + secrets.token_hex(8)
        now = _now()
        payload = (
            action_payload
            if isinstance(action_payload, str)
            else json.dumps(action_payload, sort_keys=True)
        )
        conn.execute(
            """
            INSERT INTO decision_cards(
              id, status, question, context, default_action, fire_at,
              requested_by, source_ref, originating_profile, action_kind,
              action_payload, explanation, created_at, updated_at
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                question.strip(),
                "\n".join(_clean_lines(context)),
                default_action.strip(),
                fire_at.strip(),
                requested_by.strip(),
                source_ref.strip(),
                originating_profile.strip(),
                action_kind.strip(),
                payload,
                explanation.strip(),
                now,
                now,
            ),
        )
        record_event(
            conn,
            card_id,
            "created",
            actor=requested_by.strip(),
            details={
                "source_ref": source_ref.strip(),
                "originating_profile": originating_profile.strip(),
            },
        )
        conn.commit()
        return get_card(card_id, conn=conn)
    finally:
        if owns_conn:
            conn.close()


def get_card(
    card_id: str, *, conn: Optional[sqlite3.Connection] = None
) -> DecisionCard:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM decision_cards WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise DecisionCardError(f"decision card not found: {card_id}")
        return _row_to_card(row)
    finally:
        if owns_conn:
            conn.close()


def list_cards(
    statuses: Optional[Iterable[str]] = None,
    *,
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> list[DecisionCard]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if statuses:
            status_list = [str(s) for s in statuses]
            placeholders = ",".join("?" for _ in status_list)
            rows = conn.execute(
                f"SELECT * FROM decision_cards WHERE status IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
                (*status_list, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM decision_cards ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_card(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def record_delivery(
    card_id: str,
    *,
    platform: str = "discord",
    chat_id: str = "",
    thread_id: str = "",
    message_id: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> DecisionCard:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.execute(
            """
            UPDATE decision_cards
               SET target_platform = ?, target_chat_id = ?, target_thread_id = ?,
                   message_id = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                platform,
                str(chat_id or ""),
                str(thread_id or ""),
                str(message_id or ""),
                _now(),
                card_id,
            ),
        )
        record_event(
            conn,
            card_id,
            "delivered",
            actor="system",
            details={
                "platform": platform,
                "chat_id": str(chat_id or ""),
                "thread_id": str(thread_id or ""),
                "message_id": str(message_id or ""),
            },
        )
        conn.commit()
        return get_card(card_id, conn=conn)
    finally:
        if owns_conn:
            conn.close()


def format_card_text(card: DecisionCard) -> str:
    asker = _display_profile_name(card.originating_profile)
    context_lines = _clean_lines(card.context)
    context = "\n".join(f"• {line}" for line in context_lines)
    source = f" ({card.source_ref})" if card.source_ref else ""
    return (
        f"**{asker} asks:**\n"
        f"Decision needed: {card.question}\n\n"
        f"Context:\n{context}\n\n"
        f"Default if you do nothing: {card.default_action}\n"
        f"Fire time: {card.fire_at}\n"
        f"Asked by: {card.requested_by}{source}"
    )


def info_text(card: DecisionCard) -> str:
    if card.explanation.strip():
        return card.explanation.strip()
    asker = _display_profile_name(card.originating_profile)
    return (
        f"Plain-English version:\n\n"
        f"{asker} is asking: {card.question}\n\n"
        f"Why it matters: {card.context}\n\n"
        f"If you pick Yes, {asker} records approval so the asking agent can run the next step and post a receipt. "
        f"No means stand down. Wait snoozes it back to the next morning brief. "
        f"If you do nothing, the stated default applies at {card.fire_at}: {card.default_action}"
    )


def handle_action(
    card_id: str,
    action: str,
    *,
    actor: str = "",
    actor_id: str = "",
    message_id: str = "",
    channel_id: str = "",
    thread_id: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[DecisionCard, str]:
    action = str(action or "").strip().lower()
    if action not in ACTIONS:
        raise DecisionCardError(f"unknown decision action: {action}")

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM decision_cards WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise DecisionCardError(f"decision card not found: {card_id}")
        card = _row_to_card(row)
        display_actor = actor or actor_id or "user"

        if card.status not in ACTIVE_STATUSES:
            return (
                card,
                f"This decision is already closed as `{card.answer or card.status}`. No new action was taken.",
            )

        now = _now()
        if action == "yes":
            status = "answered_yes"
            receipt = (
                "Yes recorded. The approval is in the shared decision queue; "
                "the asking agent should now run the approved next step and post the outcome receipt here."
            )
        elif action == "no":
            status = "answered_no"
            receipt = (
                "No recorded. The asking agent will stand down and log the decision."
            )
        elif action == "wait":
            status = "waiting"
            receipt = "Wait recorded. This card is snoozed and should resurface in the next morning brief."
        else:
            status = "info_requested"
            receipt = info_text(card)

        conn.execute(
            """
            UPDATE decision_cards
               SET status = ?, updated_at = ?, answered_at = ?, answered_by = ?,
                   answer = ?, receipt = ?, message_id = COALESCE(NULLIF(?, ''), message_id),
                   target_chat_id = COALESCE(NULLIF(?, ''), target_chat_id),
                   target_thread_id = COALESCE(NULLIF(?, ''), target_thread_id)
             WHERE id = ?
            """,
            (
                status,
                now,
                now,
                display_actor,
                action,
                receipt,
                message_id,
                channel_id,
                thread_id,
                card_id,
            ),
        )
        record_event(
            conn,
            card_id,
            f"button_{action}",
            actor=display_actor,
            details={
                "actor_id": actor_id,
                "message_id": message_id,
                "channel_id": channel_id,
                "thread_id": thread_id,
            },
        )
        conn.commit()
        return get_card(card_id, conn=conn), receipt
    finally:
        if owns_conn:
            conn.close()


def parse_custom_id(custom_id: str) -> tuple[str, str]:
    parts = str(custom_id or "").split(":")
    if len(parts) != 3 or parts[0] != "decision" or parts[2] not in ACTIONS:
        raise DecisionCardError("not a decision-card custom_id")
    return parts[1], parts[2]


__all__ = [
    "DecisionCard",
    "DecisionCardError",
    "ACTIONS",
    "ACTIVE_STATUSES",
    "connect",
    "create_card",
    "default_db_path",
    "format_card_text",
    "get_card",
    "handle_action",
    "info_text",
    "init_db",
    "list_cards",
    "parse_custom_id",
    "record_delivery",
    "validate_card",
]
