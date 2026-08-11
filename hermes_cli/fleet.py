"""Fleet-level operations for multi-profile Hermes deployments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class FleetResetResult:
    """Result for one profile in a fleet session reset."""

    profile: str
    path: Path
    reset_count: int = 0
    skipped: bool = False
    error: str | None = None


def reset_profile_sessions(profile: str, profile_home: Path) -> FleetResetResult:
    """Rotate every active gateway route for one profile.

    ``gateway_routing`` in ``state.db`` is authoritative on current Hermes.
    Reusing :meth:`gateway.session.SessionStore.reset_session` keeps that table,
    session lifecycle rows, and the downgrade-compatible ``sessions.json``
    mirror synchronized without changing process-global ``HERMES_HOME``.
    Existing transcripts stay attached to their predecessor session IDs.
    """
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    profile_home = Path(profile_home)
    result = FleetResetResult(profile=profile, path=profile_home)
    sessions_dir = profile_home / "sessions"
    state_db_path = profile_home / "state.db"
    sessions_file = sessions_dir / "sessions.json"

    if not state_db_path.exists() and not sessions_file.exists():
        result.skipped = True
        return result

    db = None
    try:
        db = SessionDB(state_db_path)
        config = GatewayConfig(sessions_dir=sessions_dir)
        store = SessionStore(sessions_dir, config, session_db=db)
        store._ensure_loaded()
        session_keys = list(store._entries)

        if not session_keys:
            result.skipped = True
            return result

        for session_key in session_keys:
            if store.reset_session(session_key) is not None:
                result.reset_count += 1
        result.skipped = result.reset_count == 0
    except Exception as exc:
        result.error = f"failed to reset active sessions: {exc}"
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    return result


def resolve_fleet_profiles(target: str) -> list[tuple[str, Path, bool]]:
    """Resolve ``all`` or a comma-separated target to profile status rows."""
    from hermes_cli.profiles import (
        get_profile_dir,
        list_profiles,
        normalize_profile_name,
        profile_exists,
    )

    target = (target or "").strip()
    profile_rows = {row.name: row for row in list_profiles()}
    if target == "all":
        return [
            (row.name, row.path, row.gateway_running)
            for row in profile_rows.values()
        ]

    resolved: list[tuple[str, Path, bool]] = []
    seen: set[str] = set()
    for raw_name in target.split(","):
        raw_name = raw_name.strip()
        if not raw_name:
            continue
        name = normalize_profile_name(raw_name)
        if name in seen:
            continue
        if not profile_exists(name):
            raise ValueError(f"profile '{name}' does not exist")
        row = profile_rows.get(name)
        resolved.append(
            (
                name,
                get_profile_dir(name),
                bool(row and row.gateway_running),
            )
        )
        seen.add(name)

    if not resolved:
        raise ValueError("no profiles were selected")
    return resolved


def reset_fleet_sessions(target: str) -> list[FleetResetResult]:
    """Reset active sessions for ``target`` (``all`` or profile names)."""
    return [
        reset_profile_sessions(name, path)
        for name, path, _gateway_running in resolve_fleet_profiles(target)
    ]
