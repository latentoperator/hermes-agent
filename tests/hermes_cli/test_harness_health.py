"""Harness Health v0.1 read-only diagnostic tests."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.harness_health import (
    FUNNEL_STAGES,
    collect_harness_health,
    validate_funnel,
    write_report_exclusive,
)


def _seed_session(
    home: Path,
    *,
    session_id: str = "session-1",
    cwd: Path,
    system_prompt: str = "",
    messages: list[dict] | None = None,
) -> Path:
    db_path = home / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            system_prompt TEXT,
            cwd TEXT,
            model TEXT,
            started_at REAL,
            ended_at REAL,
            tool_call_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            effect_disposition TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions(id, source, system_prompt, cwd, model, started_at) "
        "VALUES (?, 'cli', ?, ?, 'test/model', 1)",
        (session_id, system_prompt, str(cwd)),
    )
    for message in messages or []:
        conn.execute(
            """INSERT INTO messages(
                   session_id, role, content, tool_call_id, tool_calls,
                   tool_name, effect_disposition, active
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                session_id,
                message["role"],
                message.get("content"),
                message.get("tool_call_id"),
                json.dumps(message["tool_calls"])
                if message.get("tool_calls")
                else None,
                message.get("tool_name"),
                message.get("effect_disposition"),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def _seed_skill(
    home: Path,
    name: str,
    *,
    platforms: str = "linux",
) -> None:
    skill_dir = home / "skills" / "test" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: test skill {name}\n"
        f"platforms: [{platforms}]\n"
        "---\n"
        f"# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = tmp_path / "profile"
    cwd = tmp_path / "project"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_BRANCH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(cwd)
    return home, cwd


def _profile_files(home: Path) -> dict[Path, tuple[str, int, int]]:
    """Snapshot every target node so empty-directory writes are visible too."""
    return {
        path.relative_to(home): (
            "dir",
            0,
            path.stat().st_mtime_ns,
        )
        if path.is_dir()
        else (
            "file",
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in home.rglob("*")
    }


def test_malformed_config_is_not_backed_up_or_changed_by_audit(isolated_profile):
    home, cwd = isolated_profile
    config = home / "config.yaml"
    config.write_text("plugins: [unterminated", encoding="utf-8")
    before = config.read_bytes()

    report = collect_harness_health(cwd=cwd)

    assert config.read_bytes() == before
    assert not list(home.glob("config.yaml.corrupt.*.bak"))
    assert report["setup"]["config_state"] == "INACCESSIBLE"
    assert report["setup"]["tools"]["state"] == "INACCESSIBLE"
    assert report["setup"]["plugins"]["state"] == "INACCESSIBLE"
    assert report["setup"]["mcp"]["state"] == "INACCESSIBLE"
    assert report["setup"]["safeguards"]["state"] == "INACCESSIBLE"
    assert report["setup"]["skills"]["eligibility_state"] == "INACCESSIBLE"


def test_non_mapping_config_stays_inaccessible_after_permissive_cache_warm(
    isolated_profile,
):
    home, cwd = isolated_profile
    from hermes_cli.config import read_raw_config

    config = home / "config.yaml"
    config.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    assert read_raw_config() == {}

    report = collect_harness_health(cwd=cwd)

    assert report["setup"]["config_state"] == "INACCESSIBLE"
    assert not list(home.glob("config.yaml.corrupt.*.bak"))


def test_empty_non_mapping_config_fails_closed(isolated_profile):
    home, cwd = isolated_profile
    (home / "config.yaml").write_text("[]\n", encoding="utf-8")

    report = collect_harness_health(cwd=cwd)

    assert report["setup"]["config_state"] == "INACCESSIBLE"
    assert report["setup"]["tools"]["state"] == "INACCESSIBLE"


def test_malformed_config_sections_fail_closed_independently(isolated_profile):
    home, cwd = isolated_profile
    (home / "config.yaml").write_text(
        "platform_toolsets: not-a-mapping\n"
        "mcp_servers: not-a-mapping\n"
        "memory:\n  memory_char_limit: not-a-number\n",
        encoding="utf-8",
    )

    report = collect_harness_health(cwd=cwd)

    assert report["setup"]["config_state"] == "VERIFIED"
    assert report["setup"]["tools"]["state"] == "INACCESSIBLE"
    assert report["setup"]["mcp"]["state"] == "INACCESSIBLE"
    assert report["setup"]["memory"]["state"] == "INACCESSIBLE"
    assert report["setup"]["plugins"]["state"] == "VERIFIED"
    assert any("setup evidence inaccessible" in item for item in report["blind_spots"])


def test_read_raw_config_strict_mode_raises_stat_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    from hermes_cli import config as config_module

    class InaccessibleConfig:
        def stat(self):
            raise PermissionError("denied")

    monkeypatch.setattr(config_module, "get_config_path", lambda: InaccessibleConfig())

    with pytest.raises(PermissionError, match="denied"):
        config_module.read_raw_config(backup_corrupt=False, raise_on_error=True)


def test_runtime_read_does_not_create_target_wal_sidecars(
    isolated_profile, tmp_path: Path
):
    home, cwd = isolated_profile
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, system_prompt TEXT, cwd TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO sessions(id, system_prompt, cwd)
            VALUES ('wal-session', '', '/tmp');
            """
        )
        conn.commit()
        shutil.copy2(source, home / "state.db")
        shutil.copy2(Path(str(source) + "-wal"), home / "state.db-wal")
        before = _profile_files(home)

        report = collect_harness_health(cwd=cwd, session_id="wal-session")

        after = _profile_files(home)
    finally:
        conn.close()

    assert report["runtime"]["state"] == "VERIFIED"
    assert after == before


def test_runtime_read_fails_closed_while_target_db_connection_is_live(
    isolated_profile,
):
    home, cwd = isolated_profile
    from hermes_cli.sqlite_safe_read import connect_tracked

    db_path = _seed_session(home, cwd=cwd)
    before = _profile_files(home)
    connection = connect_tracked(db_path)
    try:
        report = collect_harness_health(cwd=cwd, session_id="session-1")
    finally:
        connection.close()

    assert report["runtime"]["state"] == "INACCESSIBLE"
    assert any(
        "Session evidence inaccessible" in item for item in report["blind_spots"]
    )
    assert _profile_files(home) == before


def test_fresh_cli_collection_does_not_seed_target_profile(isolated_profile):
    home, cwd = isolated_profile
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - terminal\n",
        encoding="utf-8",
    )
    before = _profile_files(home)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "--reasoning",
            "medium",
            "harness-health",
            "--cwd",
            str(cwd),
            "--json",
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["schema_version"] == "0.1"
    assert _profile_files(home) == before


def test_output_uses_exclusive_create(isolated_profile, tmp_path: Path):
    _home, cwd = isolated_profile
    report = collect_harness_health(cwd=cwd)
    destination = tmp_path / "report.json"

    write_report_exclusive(report, destination)
    first = destination.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_report_exclusive(report, destination)
    assert destination.read_text(encoding="utf-8") == first


def test_output_refuses_target_profile_destination(isolated_profile):
    home, cwd = isolated_profile
    report = collect_harness_health(cwd=cwd)
    destination = home / "reports" / "health.json"

    with pytest.raises(ValueError, match="target profile"):
        write_report_exclusive(report, destination)
    assert not destination.exists()


def test_complete_trace_cannot_omit_a_funnel_stage():
    stages = [
        {"name": name, "state": "VERIFIED", "count": 1, "basis": "test"}
        for name in FUNNEL_STAGES[:-1]
    ]

    with pytest.raises(ValueError, match="COMPLETE trace"):
        validate_funnel({"completeness": "COMPLETE", "stages": stages})


def test_profile_isolation_uses_live_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    cwd = tmp_path / "project"
    home_a.mkdir()
    home_b.mkdir()
    cwd.mkdir()
    _seed_skill(home_a, "only-a")
    _seed_skill(home_b, "only-b")

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    report_a = collect_harness_health(cwd=cwd)
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    report_b = collect_harness_health(cwd=cwd)

    assert report_a["profile"]["identity"] != report_b["profile"]["identity"]
    assert report_a["setup"]["skills"]["available_names"] == ["only-a"]
    assert report_b["setup"]["skills"]["available_names"] == ["only-b"]
    assert str(home_a) not in json.dumps(report_a)
    assert str(home_b) not in json.dumps(report_b)


def test_context_precedence_uses_native_loaders_without_profile_writes(
    isolated_profile,
):
    home, cwd = isolated_profile
    root = cwd / "repo"
    nested = root / "src"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".hermes.md").write_text("effective", encoding="utf-8")
    (nested / "AGENTS.md").write_text("shadowed agents", encoding="utf-8")
    (nested / "CLAUDE.md").write_text("shadowed claude", encoding="utf-8")
    (nested / ".cursorrules").write_text("shadowed cursor", encoding="utf-8")
    before = _profile_files(home)

    context = collect_harness_health(cwd=nested)["setup"]["context"]

    assert context["effective"]["kind"] == "hermes"
    assert context["effective"]["path"] == "$PROJECT_ROOT/.hermes.md"
    assert [item["kind"] for item in context["shadowed"]] == [
        "agents",
        "claude",
        "cursorrules",
    ]
    assert _profile_files(home) == before


def test_empty_higher_priority_context_does_not_shadow_loadable_context(
    isolated_profile,
):
    _home, cwd = isolated_profile
    (cwd / ".hermes.md").write_text("", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("active agents", encoding="utf-8")

    context = collect_harness_health(cwd=cwd)["setup"]["context"]

    assert context["effective"]["kind"] == "agents"
    assert context["shadowed"] == []
    assert context["inaccessible"][0]["kind"] == "hermes"


def test_runtime_reports_context_and_portable_selected_session_cwd(
    isolated_profile,
):
    home, cwd = isolated_profile
    session_cwd = cwd / "nested"
    session_cwd.mkdir()
    _seed_session(
        home,
        cwd=session_cwd,
        system_prompt=(
            "# Project Context\n\n"
            "The following project context files have been loaded and should "
            "be followed:\n\n"
            "## AGENTS.md\n\nsession context"
        ),
    )

    runtime = collect_harness_health(cwd=cwd, session_id="session-1")["runtime"]

    assert runtime["session_cwd"]["path"] == "$CWD/nested"
    assert runtime["session_cwd"]["identity"].startswith("sha256:")
    assert runtime["context"] == {
        "state": "VERIFIED",
        "shown": True,
        "kind": "agents",
    }
    assert str(session_cwd) not in json.dumps(runtime)


def test_skills_distinguish_inventory_rendered_index_and_successful_load(
    isolated_profile,
):
    home, cwd = isolated_profile
    _seed_skill(home, "alpha")
    _seed_skill(home, "beta")
    _seed_session(
        home,
        cwd=cwd,
        system_prompt="""
## Skills (mandatory)
<available_skills>
  test:
    - alpha: first skill
  plugin [names only]: fixture:beta
</available_skills>
[IMPORTANT: The user launched this CLI session with the "alpha" skill preloaded.]
""",
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-skill",
                        "type": "function",
                        "function": {
                            "name": "skill_view",
                            "arguments": '{"name":"fixture:beta"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-skill",
                "tool_name": "skill_view",
                "content": '{"success":true}',
            },
        ],
    )

    report = collect_harness_health(cwd=cwd, session_id="session-1")
    setup = report["setup"]["skills"]
    runtime = report["runtime"]["skills"]

    assert setup["installed_names"] == ["alpha", "beta"]
    assert setup["eligible_names"] == ["alpha", "beta"]
    assert runtime["shown_names"] == ["alpha", "fixture:beta"]
    assert runtime["preloaded_names"] == ["alpha"]
    assert runtime["viewed_names"] == ["fixture:beta"]
    assert runtime["loaded_names"] == ["alpha", "fixture:beta"]


def test_failed_skill_view_is_not_reported_as_loaded(isolated_profile):
    home, cwd = isolated_profile
    _seed_session(
        home,
        cwd=cwd,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-skill",
                        "type": "function",
                        "function": {
                            "name": "skill_view",
                            "arguments": '{"name":"alpha"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-skill",
                "tool_name": "skill_view",
                "content": '{"success":false,"error":"missing"}',
            },
        ],
    )

    runtime = collect_harness_health(cwd=cwd, session_id="session-1")["runtime"][
        "skills"
    ]

    assert runtime["viewed_names"] == []
    assert runtime["loaded_names"] == []


def test_skill_inventory_separates_installed_from_platform_eligible(
    isolated_profile,
):
    home, cwd = isolated_profile
    _seed_skill(home, "mac-only", platforms="darwin")

    skills = collect_harness_health(cwd=cwd, platform="cli")["setup"]["skills"]

    assert "mac-only" in skills["installed_names"]
    assert "mac-only" not in skills["eligible_names"]
    assert "mac-only" in skills["ineligible_names"]


def test_tool_inventory_applies_effective_resolution_and_check_fn(
    isolated_profile, monkeypatch: pytest.MonkeyPatch
):
    home, cwd = isolated_profile
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    (home / "config.yaml").write_text(
        """platform_toolsets:
  cli:
    - terminal
    - file
    - homeassistant
agent:
  disabled_toolsets:
    - file
mcp_servers:
  enabled-server:
    command: test
  disabled-server:
    command: test
    enabled: false
""",
        encoding="utf-8",
    )

    setup = collect_harness_health(cwd=cwd)["setup"]
    tools = setup["tools"]

    assert "terminal" in tools["eligible_toolsets"]
    assert "file" not in tools["eligible_toolsets"]
    assert "homeassistant" in tools["eligible_toolsets"]
    assert not [name for name in tools["eligible_names"] if name.startswith("ha_")]
    assert "enabled-server" in tools["eligible_toolsets"]
    assert setup["mcp"]["enabled_names"] == ["enabled-server"]
    assert setup["mcp"]["disabled_names"] == ["disabled-server"]


def test_plugin_disabled_precedence_and_runtime_ownership(isolated_profile):
    home, cwd = isolated_profile
    from tools.registry import registry

    registry.register(
        name="test_plugin_tool",
        toolset="plugin-alpha",
        schema={
            "name": "test_plugin_tool",
            "description": "test plugin tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args: "ok",
    )
    try:
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: [alpha, beta]\n  disabled: [alpha]\n",
            encoding="utf-8",
        )
        _seed_session(
            home,
            cwd=cwd,
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-plugin",
                            "type": "function",
                            "function": {
                                "name": "test_plugin_tool",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-plugin",
                    "tool_name": "test_plugin_tool",
                    "content": "ok",
                },
            ],
        )

        report = collect_harness_health(cwd=cwd, session_id="session-1")
    finally:
        registry.deregister("test_plugin_tool")

    assert report["setup"]["plugins"]["enabled_names"] == ["beta"]
    assert report["setup"]["plugins"]["disabled_names"] == ["alpha"]
    assert report["runtime"]["plugins"]["state"] == "INFERRED"
    assert report["runtime"]["plugins"]["used_names"] == ["alpha"]
    assert report["runtime"]["plugins"]["used_tool_names"] == ["test_plugin_tool"]


def test_mcp_runtime_ownership_uses_registry_mapping(isolated_profile):
    home, cwd = isolated_profile
    from tools.registry import registry

    registry.register(
        name="mcp__fixture__lookup",
        toolset="mcp-fixture",
        schema={
            "name": "mcp__fixture__lookup",
            "description": "test MCP tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args: "ok",
    )
    try:
        _seed_session(
            home,
            cwd=cwd,
            messages=[
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-mcp",
                            "type": "function",
                            "function": {
                                "name": "mcp__fixture__lookup",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-mcp",
                    "tool_name": "mcp__fixture__lookup",
                    "content": "ok",
                },
            ],
        )

        runtime = collect_harness_health(cwd=cwd, session_id="session-1")["runtime"][
            "mcp"
        ]
    finally:
        registry.deregister("mcp__fixture__lookup")

    assert runtime["state"] == "VERIFIED"
    assert runtime["used_names"] == ["fixture"]
    assert runtime["used_tool_names"] == ["mcp__fixture__lookup"]


def test_memory_reports_only_capacity_count_and_snapshot_identity(
    isolated_profile,
):
    home, cwd = isolated_profile
    memory_dir = home / "memories"
    memory_dir.mkdir()
    secret_memory = "private memory text that must not appear"
    (memory_dir / "MEMORY.md").write_text(
        f"first\n§\n{secret_memory}",
        encoding="utf-8",
    )

    report = collect_harness_health(cwd=cwd)
    memory = report["setup"]["memory"]
    encoded = json.dumps(report)

    assert memory["memory"]["entry_count"] == 2
    assert memory["memory"]["snapshot_identity"].startswith("sha256:")
    assert secret_memory not in encoded
    assert set(memory["memory"]) == {"entry_count", "snapshot_identity"}


def test_report_redacts_secrets_private_paths_and_untrusted_identifiers(
    isolated_profile,
):
    home, cwd = isolated_profile
    # Construct a realistic-looking test token without checking a static token
    # literal into the repository.
    secret = "sk-" + ("t" * 40)
    private = "/home/alice/private/client"
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled: [{secret}]\n"
        f"mcp_servers:\n  '{private}':\n    command: test\n",
        encoding="utf-8",
    )
    _seed_session(
        home,
        session_id=secret,
        cwd=cwd,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-secret",
                        "type": "function",
                        "function": {"name": secret, "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-secret",
                "tool_name": secret,
                "content": private,
            },
        ],
    )

    encoded = json.dumps(collect_harness_health(cwd=cwd, session_id=secret))

    assert secret not in encoded
    assert private not in encoded
    assert str(home) not in encoded
    assert str(cwd) not in encoded


def test_safeguards_report_current_effective_state_and_historical_blind_spot(
    isolated_profile, monkeypatch: pytest.MonkeyPatch
):
    home, cwd = isolated_profile
    from tools import approval

    (home / "config.yaml").write_text(
        "approvals:\n  mode: smart\ncheckpoints:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)

    safeguards = collect_harness_health(cwd=cwd)["setup"]["safeguards"]

    assert safeguards["configured_approval_mode"] == "smart"
    assert safeguards["effective_approval_mode"] == "off"
    assert safeguards["approval_process_yolo_override"] is True
    assert safeguards["approval_current_session_yolo_override"] is False
    assert safeguards["checkpoints_enabled_profile_default"] is True
    assert safeguards["checkpoints_enabled_effective"]["state"] == "INACCESSIBLE"
    assert safeguards["approval_historical_session_override"]["state"] == (
        "INACCESSIBLE"
    )


def test_funnel_is_one_causal_capability_population(isolated_profile):
    home, cwd = isolated_profile
    _seed_session(
        home,
        cwd=cwd,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "terminal-1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command":"pwd"}',
                        },
                    },
                    {
                        "id": "terminal-2",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command":"date"}',
                        },
                    },
                    {
                        "id": "file-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "terminal-1",
                "tool_name": "terminal",
                "content": "ok",
            },
            {
                "role": "tool",
                "tool_call_id": "terminal-2",
                "tool_name": "terminal",
                "content": "ok",
            },
            {
                "role": "tool",
                "tool_call_id": "file-1",
                "tool_name": "terminal",
                "content": "mismatched name",
            },
        ],
    )

    report = collect_harness_health(cwd=cwd, session_id="session-1")
    stages = {stage["name"]: stage for stage in report["funnel"]["stages"]}

    assert [stage["name"] for stage in report["funnel"]["stages"]] == list(
        FUNNEL_STAGES
    )
    assert report["funnel"]["population"] == "selected-session tool capabilities"
    assert report["funnel"]["completeness"] == "PARTIAL"
    assert stages["Available"]["count"] == 2
    assert stages["Eligible"]["count"] == 2
    assert stages["Shown"]["count"] == 2
    assert stages["Consulted"]["count"] == 2
    assert stages["Acted through"]["count"] == 2
    assert stages["Checked"]["count"] == 1
    assert stages["Accepted"]["state"] == "INACCESSIBLE"
    assert report["runtime"]["tools"]["matched_result_count"] == 2


def test_complete_trace_counts_must_follow_causal_order():
    counts = [1, 1, 2, 1, 1, 1, 0]
    stages = [
        {"name": name, "state": "VERIFIED", "count": count, "basis": "test"}
        for name, count in zip(FUNNEL_STAGES, counts)
    ]

    with pytest.raises(ValueError, match="causal funnel order"):
        validate_funnel({"completeness": "COMPLETE", "stages": stages})
