"""Cron-aware external-memory sync: what a scheduled fire mirrors into memory providers.

A cron turn's "user message" is the scheduler-assembled prompt (cron hint, notepad, script output /
``## Run Context``, ``context_from`` previous output, the job's standing instructions). Mirroring it as
the user's words makes providers re-learn the job's own instructions on every fire. For
``platform == "cron"`` the turn is reduced to a compact provenance header plus the final response;
a silent or empty fire writes nothing, and no next-turn prefetch is queued (a cron session has no
next human turn). Primary and subagent turns never reach this module.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_SILENT_TOKEN = re.compile(r"\[SILENT\]", re.IGNORECASE)
_CRON_FAILURE_MARKER = "[CRON_FAILURE]"
_MAX_NAME_CHARS = 120


def cron_turn_mode(response_text: str) -> str:
    """``skipped_empty`` | ``skipped_silent`` | ``response`` for a cron final response."""
    stripped = (response_text or "").strip()
    if not stripped:
        return "skipped_empty"
    if not _SILENT_TOKEN.sub("", stripped).strip():
        return "skipped_silent"
    return "response"


def cron_provenance_header(meta: dict[str, Any] | None, response_text: str) -> str:
    """``Scheduled job "<name>" (<id>) ran at <iso>; status: ok|failure.``"""
    meta = meta or {}
    job_id = str(meta.get("id") or "unknown")
    name = " ".join(str(meta.get("name") or job_id).split())[:_MAX_NAME_CHARS]
    ran_at = meta.get("scheduled_at") or datetime.now().astimezone().isoformat(timespec="seconds")
    first_line = (response_text or "").strip().splitlines()[0].strip() if (response_text or "").strip() else ""
    status = "failure" if first_line == _CRON_FAILURE_MARKER else "ok"
    return f'Scheduled job "{name}" ({job_id}) ran at {ran_at}; status: {status}.'


def sync_cron_turn(agent: Any, response_text: str) -> str:
    """Mirror one cron turn as header + response (or skip it); returns the mode logged."""
    meta = getattr(agent, "_cron_job_meta", None)
    job_id = (meta or {}).get("id") or "unknown"
    mode = cron_turn_mode(response_text)
    logger.info("cron memory sync job=%s mode=%s", job_id, mode)
    if mode != "response":
        return mode
    # No ``messages``: the transcript's user row is the assembled prompt this path exists to keep out.
    agent._memory_manager.sync_all(
        cron_provenance_header(meta, response_text), response_text, session_id=agent.session_id or "")
    return mode
