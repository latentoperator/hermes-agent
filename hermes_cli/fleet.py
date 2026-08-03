"""Fleet-level operational helpers for multi-profile Hermes deployments.

These helpers intentionally live outside ``gateway.run`` so gateway slash
commands, CLI commands, and tests can share the same session-reset semantics.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass
class FleetResetResult:
    """Result for one profile in a fleet session reset."""

    profile: str
    path: Path
    reset_count: int = 0
    skipped: bool = False
    error: str | None = None


_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "last_prompt_tokens",
    "estimated_cost_usd",
)


def _atomic_json_write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            raise


def _new_session_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _reset_entry(entry: dict, now: datetime) -> tuple[dict, str | None, str]:
    """Return ``(new_entry, old_session_id, new_session_id)``.

    Mirrors ``gateway.session.SessionStore.reset_session()`` for the on-disk
    JSON session index while avoiding process-global HERMES_HOME changes.
    """
    old_session_id = entry.get("session_id") if isinstance(entry, dict) else None
    new_session_id = _new_session_id(now)
    new_entry = dict(entry)
    new_entry["session_id"] = new_session_id
    new_entry["created_at"] = now.isoformat()
    new_entry["updated_at"] = now.isoformat()
    new_entry["is_fresh_reset"] = True
    new_entry["was_auto_reset"] = False
    new_entry["auto_reset_reason"] = None
    new_entry["reset_had_activity"] = False
    new_entry["expiry_finalized"] = False
    new_entry["suspended"] = False
    new_entry["resume_pending"] = False
    new_entry["resume_reason"] = None
    new_entry["last_resume_marked_at"] = None
    new_entry["cost_status"] = "unknown"
    for field in _TOKEN_FIELDS:
        new_entry[field] = 0
    return new_entry, old_session_id, new_session_id


def reset_profile_sessions(profile: str, profile_home: Path) -> FleetResetResult:
    """Reset every active gateway session entry for one profile.

    This rotates each session key to a fresh session ID and marks the entry as
    ``is_fresh_reset`` so the next inbound message behaves like manual
    ``/new``/``/reset``. Existing transcripts remain in ``state.db`` under the
    old session IDs; the new sessions start empty.
    """
    profile_home = Path(profile_home)
    result = FleetResetResult(profile=profile, path=profile_home)
    sessions_file = profile_home / "sessions" / "sessions.json"
    if not sessions_file.exists():
        result.skipped = True
        return result

    try:
        data = json.loads(sessions_file.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("sessions.json root is not an object")
    except Exception as exc:
        result.error = f"failed to read sessions.json: {exc}"
        return result

    if not data:
        result.skipped = True
        return result

    now = datetime.now()
    rotated: dict[str, dict] = {}
    db_ops: list[tuple[str | None, str, dict]] = []
    for session_key, entry in data.items():
        if not isinstance(entry, dict):
            rotated[session_key] = entry
            continue
        new_entry, old_session_id, new_session_id = _reset_entry(entry, now)
        rotated[session_key] = new_entry
        db_ops.append((old_session_id, new_session_id, new_entry))

    try:
        _atomic_json_write(sessions_file, rotated)
    except Exception as exc:
        result.error = f"failed to write sessions.json: {exc}"
        return result

    # Keep state.db metadata aligned when available. These operations are best
    # effort: sessions.json is the gateway routing source, and append failures
    # are logged elsewhere if state.db is temporarily locked.
    try:
        from hermes_state import SessionDB

        db = SessionDB(profile_home / "state.db")
        for old_session_id, new_session_id, entry in db_ops:
            if old_session_id:
                try:
                    db.end_session(old_session_id, "fleet_session_reset")
                except Exception:
                    pass
            try:
                raw_origin = entry.get("origin")
                origin = raw_origin if isinstance(raw_origin, dict) else {}
                source = entry.get("platform") or origin.get("platform") or "unknown"
                user_id = origin.get("user_id")
                db.create_session(new_session_id, source=source, user_id=user_id)
            except Exception:
                pass
    except Exception:
        pass

    result.reset_count = len(db_ops)
    result.skipped = result.reset_count == 0
    return result


def resolve_fleet_profiles(target: str) -> list[tuple[str, Path, bool]]:
    """Resolve a fleet target into ``(name, path, gateway_running)`` tuples."""
    from hermes_cli.profiles import get_profile_dir, list_profiles, normalize_profile_name, profile_exists

    target = (target or "").strip()
    if target == "all":
        return [(p.name, p.path, p.gateway_running) for p in list_profiles()]

    names: Iterable[str] = [part.strip() for part in target.split(",") if part.strip()]
    resolved: list[tuple[str, Path, bool]] = []
    for name in names:
        canon = normalize_profile_name(name)
        if not profile_exists(canon):
            raise ValueError(f"profile '{canon}' does not exist")
        resolved.append((canon, get_profile_dir(canon), False))
    return resolved


def reset_fleet_sessions(target: str) -> list[FleetResetResult]:
    """Reset active sessions for ``target`` (``all`` or comma-separated profiles)."""
    return [
        reset_profile_sessions(name, path)
        for name, path, _gateway_running in resolve_fleet_profiles(target)
    ]
