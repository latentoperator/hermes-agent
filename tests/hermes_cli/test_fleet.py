import json
from pathlib import Path

from hermes_cli.fleet import reset_profile_sessions


def test_reset_profile_sessions_rotates_active_session_entries(tmp_path: Path):
    profile_home = tmp_path / "profile"
    sessions_dir = profile_home / "sessions"
    sessions_dir.mkdir(parents=True)
    sessions_file = sessions_dir / "sessions.json"
    sessions_file.write_text(
        json.dumps(
            {
                "agent:main:telegram:dm:123": {
                    "session_key": "agent:main:telegram:dm:123",
                    "session_id": "old_session",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "display_name": "Chris",
                    "platform": "telegram",
                    "chat_type": "dm",
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_tokens": 30,
                    "cache_write_tokens": 40,
                    "total_tokens": 100,
                    "last_prompt_tokens": 50,
                    "estimated_cost_usd": 1.25,
                    "cost_status": "estimated",
                    "suspended": True,
                    "resume_pending": True,
                    "resume_reason": "restart_timeout",
                    "is_fresh_reset": False,
                }
            }
        ),
        encoding="utf-8",
    )

    result = reset_profile_sessions("test", profile_home)

    assert result.error is None
    assert result.reset_count == 1
    assert not result.skipped

    data = json.loads(sessions_file.read_text(encoding="utf-8"))
    entry = data["agent:main:telegram:dm:123"]
    assert entry["session_id"] != "old_session"
    assert entry["is_fresh_reset"] is True
    assert entry["suspended"] is False
    assert entry["resume_pending"] is False
    assert entry["resume_reason"] is None
    assert entry["input_tokens"] == 0
    assert entry["output_tokens"] == 0
    assert entry["total_tokens"] == 0
    assert entry["estimated_cost_usd"] == 0
    assert entry["cost_status"] == "unknown"


def test_reset_profile_sessions_skips_missing_store(tmp_path: Path):
    result = reset_profile_sessions("empty", tmp_path / "missing")

    assert result.error is None
    assert result.skipped is True
    assert result.reset_count == 0
