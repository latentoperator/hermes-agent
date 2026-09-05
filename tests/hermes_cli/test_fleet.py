import json
from datetime import datetime
from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionStore
from hermes_cli.fleet import reset_profile_sessions
from hermes_state import SessionDB


SESSION_KEY = "agent:main:telegram:dm:123"


def _legacy_entry() -> dict:
    return {
        "session_key": SESSION_KEY,
        "session_id": "old_session",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "display_name": "Chris",
        "platform": "telegram",
        "chat_type": "dm",
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "estimated_cost_usd": 1.25,
        "cost_status": "estimated",
        "suspended": True,
        "resume_pending": True,
        "resume_reason": "restart_timeout",
        "is_fresh_reset": False,
    }


def test_reset_profile_sessions_rotates_legacy_and_primary_routes(tmp_path: Path):
    profile_home = tmp_path / "profile"
    sessions_dir = profile_home / "sessions"
    sessions_dir.mkdir(parents=True)
    sessions_file = sessions_dir / "sessions.json"
    sessions_file.write_text(json.dumps({SESSION_KEY: _legacy_entry()}), encoding="utf-8")

    result = reset_profile_sessions("test", profile_home)

    assert result.error is None
    assert result.reset_count == 1
    assert not result.skipped

    mirror = json.loads(sessions_file.read_text(encoding="utf-8"))[SESSION_KEY]
    assert mirror["session_id"] != "old_session"
    assert mirror["is_fresh_reset"] is True
    assert mirror["suspended"] is False
    assert mirror["resume_pending"] is False
    assert mirror["input_tokens"] == 0
    assert mirror["estimated_cost_usd"] == 0

    db = SessionDB(profile_home / "state.db", read_only=True)
    try:
        routing = db.load_gateway_routing_entries(scope=str(sessions_dir.resolve()))
    finally:
        db.close()
    primary = json.loads(routing[SESSION_KEY])
    assert primary["session_id"] == mirror["session_id"]


def test_reset_profile_sessions_reads_state_db_without_json_mirror(tmp_path: Path):
    profile_home = tmp_path / "profile"
    sessions_dir = profile_home / "sessions"
    db = SessionDB(profile_home / "state.db")
    config = GatewayConfig(sessions_dir=sessions_dir, write_sessions_json=False)
    store = SessionStore(sessions_dir, config, session_db=db)
    now = datetime.now()
    store._entries[SESSION_KEY] = SessionEntry(
        session_key=SESSION_KEY,
        session_id="db_only_session",
        created_at=now,
        updated_at=now,
        platform=Platform.TELEGRAM,
    )
    store._loaded = True
    with store._lock:
        store._save()
    db.close()
    assert not (sessions_dir / "sessions.json").exists()

    result = reset_profile_sessions("test", profile_home)

    assert result.error is None
    assert result.reset_count == 1
    db = SessionDB(profile_home / "state.db", read_only=True)
    try:
        routing = db.load_gateway_routing_entries(scope=str(sessions_dir.resolve()))
    finally:
        db.close()
    assert json.loads(routing[SESSION_KEY])["session_id"] != "db_only_session"


def test_reset_profile_sessions_skips_missing_store(tmp_path: Path):
    result = reset_profile_sessions("empty", tmp_path / "missing")

    assert result.error is None
    assert result.skipped is True
    assert result.reset_count == 0
