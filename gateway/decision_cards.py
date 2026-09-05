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
PROTECTED_INSTRUCTION_WRITE_ACTION_KIND = "protected_instruction_write"
PROTECTED_INSTRUCTION_WRITE_ACTION = "write_protected_instruction_files"


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
    return " ".join(
        part.capitalize()
        for part in raw.replace("_", " ").replace("-", " ").split()
    )


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


def _canonical_approved_paths(value: Any) -> list[str] | None:
    """Return a sorted exact path set for a protected-write approval payload."""
    if not isinstance(value, list) or not value:
        return None
    canonical: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        path = Path(item).expanduser()
        if not path.is_absolute():
            return None
        canonical.append(str(path.resolve(strict=False)))
    if len(canonical) != len(set(canonical)):
        return None
    return sorted(canonical)


def _parse_protected_instruction_write_payload(
    raw_payload: str,
    question: str,
    context: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DecisionCardError(
            "protected instruction write action_payload must be valid JSON"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "action",
        "task_id",
        "paths",
        "yes_action",
    }:
        raise DecisionCardError(
            "protected instruction write payload must contain exactly "
            "action, task_id, paths, and yes_action"
        )
    if payload.get("action") != PROTECTED_INSTRUCTION_WRITE_ACTION:
        raise DecisionCardError("protected instruction write action is invalid")
    if not str(payload.get("task_id") or "").strip():
        raise DecisionCardError("protected instruction write task_id is required")
    if not str(payload.get("yes_action") or "").strip():
        raise DecisionCardError("protected instruction write yes_action is required")
    paths = _canonical_approved_paths(payload.get("paths"))
    if paths is None:
        raise DecisionCardError(
            "protected instruction write paths must be unique absolute paths"
        )
    visible_lines = {
        line.strip() for line in f"{question}\n{context}".splitlines() if line.strip()
    }
    if any(path not in visible_lines for path in paths):
        raise DecisionCardError(
            "each protected instruction write exact absolute path must be a "
            "standalone Decision Card question/context line"
        )
    task_id = str(payload["task_id"]).strip()
    yes_action = str(payload["yes_action"]).strip()
    if f"Task {task_id} — Yes: {yes_action}" not in visible_lines:
        raise DecisionCardError(
            "protected instruction write task_id and yes_action must be visible "
            "as an exact 'Task <id> — Yes: <action>' line"
        )
    return {
        "task_id": task_id,
        "paths": paths,
        "yes_action": yes_action,
    }


def _allowed_decision_card_actor_ids(platform: str) -> set[str]:
    """Return explicit user ids allowed to answer cards on *platform*.

    This intentionally ignores ``*``/allow-all and role-only authorization:
    an executable approval must bind to an explicit user identity that the
    gateway already trusts on that platform.
    """
    platform_key = str(platform or "").strip().lower()
    platform_env = {
        "discord": "DISCORD_ALLOWED_USERS",
        "telegram": "TELEGRAM_ALLOWED_USERS",
        "slack": "SLACK_ALLOWED_USERS",
    }.get(platform_key)
    if platform_env is None:
        return set()
    allowed: set[str] = set()
    for key in (platform_env, "GATEWAY_ALLOWED_USERS"):
        allowed.update(
            value.strip()
            for value in str(os.environ.get(key) or "").split(",")
            if value.strip() and value.strip() != "*"
        )
    return allowed


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
        if action_kind.strip() == PROTECTED_INSTRUCTION_WRITE_ACTION_KIND:
            _parse_protected_instruction_write_payload(payload, question, context)
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


def claim_protected_instruction_write(
    *,
    task_id: str,
    profile: str,
    paths: Iterable[str],
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[DecisionCard]:
    """Consume one exact answered-Yes grant for a headless Kanban file write.

    The guard deliberately accepts no free text. A matching card must use the
    dedicated action kind and an exact JSON payload containing only
    ``action``, ``task_id``, ``paths``, and ``yes_action``. The card body must
    show every absolute path Chris approved, and its Yes must have arrived via
    a recorded button event with a non-empty actor id. The first matching call
    records ``protected_write_claimed`` under ``BEGIN IMMEDIATE``; later calls
    cannot reuse that card.

    ``None`` means there is no valid, unused grant. Callers then keep their
    normal interactive approval/fail-closed behaviour.
    """
    task = str(task_id or "").strip()
    owner = str(profile or "").strip()
    requested = _canonical_approved_paths(list(paths))
    if not task or not owner or requested is None:
        return None

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT * FROM decision_cards
             WHERE status='answered_yes' AND answer='yes'
               AND action_kind=? AND originating_profile=?
             ORDER BY answered_at DESC, created_at DESC
            """,
            (PROTECTED_INSTRUCTION_WRITE_ACTION_KIND, owner),
        ).fetchall()
        for row in rows:
            try:
                payload = _parse_protected_instruction_write_payload(
                    str(row["action_payload"] or ""),
                    str(row["question"] or ""),
                    str(row["context"] or ""),
                )
            except DecisionCardError:
                continue
            if payload["task_id"] != task or payload["paths"] != requested:
                continue

            answered_by = str(row["answered_by"] or "").strip()
            button = conn.execute(
                """
                SELECT actor, details_json FROM decision_card_events
                 WHERE card_id=? AND event_type='button_yes'
                 ORDER BY id DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            if (
                not answered_by
                or button is None
                or str(button["actor"] or "").strip() != answered_by
            ):
                continue
            try:
                button_details = json.loads(str(button["details_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if not isinstance(button_details, dict):
                continue
            actor_id = str(button_details.get("actor_id") or "").strip()
            actor_platform = (
                str(button_details.get("actor_platform") or "").strip().lower()
            )
            if not actor_platform:
                delivery = conn.execute(
                    """
                    SELECT details_json FROM decision_card_events
                     WHERE card_id=? AND event_type='delivered'
                     ORDER BY id DESC LIMIT 1
                    """,
                    (row["id"],),
                ).fetchone()
                try:
                    delivery_details = (
                        json.loads(str(delivery["details_json"] or "{}"))
                        if delivery is not None
                        else {}
                    )
                except json.JSONDecodeError:
                    delivery_details = {}
                if isinstance(delivery_details, dict):
                    actor_platform = (
                        str(delivery_details.get("platform") or "").strip().lower()
                    )
            if actor_id not in _allowed_decision_card_actor_ids(actor_platform):
                continue

            already_claimed = conn.execute(
                """
                SELECT 1 FROM decision_card_events
                 WHERE card_id=? AND event_type='protected_write_claimed'
                 LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            if already_claimed:
                continue

            record_event(
                conn,
                str(row["id"]),
                "protected_write_claimed",
                actor="file-tools",
                details={
                    "task_id": task,
                    "profile": owner,
                    "paths": requested,
                },
            )
            conn.commit()
            return _row_to_card(row)
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
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
    actor_platform: str = "",
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
                "actor_platform": str(actor_platform or "").strip().lower(),
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
    "PROTECTED_INSTRUCTION_WRITE_ACTION",
    "PROTECTED_INSTRUCTION_WRITE_ACTION_KIND",
    "claim_protected_instruction_write",
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
