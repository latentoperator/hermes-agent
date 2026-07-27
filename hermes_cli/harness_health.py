"""Read-only collection for the Harness Health v0.1 diagnostic."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from hermes_constants import get_hermes_home


EVIDENCE_STATES = ("VERIFIED", "INFERRED", "USER_REPORTED", "INACCESSIBLE")
FUNNEL_STAGES = (
    "Available",
    "Eligible",
    "Shown",
    "Consulted",
    "Acted through",
    "Checked",
    "Accepted",
)
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_PORTABLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def _identity(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()


def _text_identity(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _safe_name(value: Any) -> str:
    """Keep useful identifiers while dropping secret- or path-shaped values."""
    from agent.redact import redact_sensitive_text

    raw = str(value)
    if redact_sensitive_text(raw, force=True) != raw:
        return "<redacted>"
    if not _PORTABLE_NAME_RE.fullmatch(raw):
        return "<redacted>"
    if raw.startswith(("/", "\\")) or "\\" in raw or ".." in raw.split("/"):
        return "<redacted>"
    return raw


def _safe_names(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    return sorted({_safe_name(value) for value in values})


@contextmanager
def _isolated_requirement_home():
    """Keep native probes away from the audited profile's writable state."""
    previous = os.environ.get("HERMES_HOME")
    with tempfile.TemporaryDirectory(prefix="hermes-harness-health-") as scratch:
        os.environ["HERMES_HOME"] = scratch
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous


def _portable(
    path: Path,
    *,
    home: Path,
    cwd: Path,
    project_root: Path | None = None,
) -> str:
    path = path.resolve()
    for base, token in (
        (project_root, "$PROJECT_ROOT"),
        (home, "$HERMES_HOME"),
        (cwd, "$CWD"),
    ):
        if base is None:
            continue
        try:
            rel = path.relative_to(base.resolve())
            return token if str(rel) == "." else f"{token}/{rel.as_posix()}"
        except ValueError:
            pass
    return "<outside-scope>"


def _context_inventory(cwd: Path, home: Path) -> dict[str, Any]:
    """Run the native context loaders and report their actual rendered result."""
    from agent.prompt_builder import (
        _find_git_root,
        _find_hermes_md,
        _load_agents_md,
        _load_claude_md,
        _load_cursorrules,
        _load_hermes_md,
    )

    root = _find_git_root(cwd) or cwd
    candidates: list[tuple[str, Path, bool]] = []

    # Native loaders consult config for truncation limits. Run them against a
    # disposable profile: this audit only needs the precedence/renderability
    # decision, and must never let their normal config bootstrap touch target.
    with _isolated_requirement_home():
        hermes = _find_hermes_md(cwd)
        if hermes:
            candidates.append(("hermes", hermes, bool(_load_hermes_md(cwd))))
        for kind, names, loader in (
            ("agents", ("AGENTS.md", "agents.md"), _load_agents_md),
            ("claude", ("CLAUDE.md", "claude.md"), _load_claude_md),
        ):
            match = next((cwd / name for name in names if (cwd / name).is_file()), None)
            if match:
                candidates.append((kind, match, bool(loader(cwd))))
        cursor = cwd / ".cursorrules"
        cursor_dir = cwd / ".cursor" / "rules"
        if cursor.is_file() or (cursor_dir.is_dir() and any(cursor_dir.glob("*.mdc"))):
            path = cursor if cursor.is_file() else cursor_dir
            candidates.append(("cursorrules", path, bool(_load_cursorrules(cwd))))

    usable: list[dict[str, Any]] = []
    inaccessible: list[dict[str, Any]] = []
    for kind, path, rendered in candidates:
        item = {
            "kind": kind,
            "path": _portable(path, home=home, cwd=cwd, project_root=root),
            "state": "VERIFIED" if rendered else "INACCESSIBLE",
        }
        (usable if rendered else inaccessible).append(item)
    return {
        "effective": usable[0] if usable else None,
        "shadowed": usable[1:],
        "inaccessible": inaccessible,
    }


def _installed_skill_names(home: Path) -> set[str]:
    from agent.skill_utils import get_external_skills_dirs, iter_skill_index_files
    from tools.skills_tool import _parse_frontmatter

    names: set[str] = set()
    scan_dirs = [home / "skills", *get_external_skills_dirs()]
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
                frontmatter, _body = _parse_frontmatter(content)
                name = frontmatter.get("name") or skill_md.parent.name
            except Exception:
                continue
            if isinstance(name, str) and _SKILL_NAME_RE.fullmatch(name):
                names.add(name)
    return names


def _skill_inventory(platform: str, home: Path) -> dict[str, Any]:
    from agent.skill_utils import get_disabled_skill_names
    from tools.skills_tool import _find_all_skills

    installed_names = _installed_skill_names(home)
    available = _find_all_skills(skip_disabled=True)
    available_names = sorted({
        str(skill.get("name")) for skill in available if skill.get("name")
    })
    disabled = set(get_disabled_skill_names(platform))
    eligible_names = sorted(set(available_names) - disabled)
    return {
        "state": "VERIFIED",
        "eligibility_state": "VERIFIED",
        "installed_names": _safe_names(installed_names),
        "available_names": _safe_names(available_names),
        "eligible_names": _safe_names(eligible_names),
        "disabled_names": _safe_names(disabled),
        "ineligible_names": _safe_names(installed_names - set(eligible_names)),
    }


def _tool_inventory(config: dict[str, Any], platform: str) -> dict[str, Any]:
    """Resolve effective toolsets, then apply current registry requirement gates."""
    from hermes_cli.tools_config import _get_platform_tools
    from tools.registry import discover_builtin_tools, registry
    from toolsets import TOOLSETS, resolve_toolset

    with _isolated_requirement_home():
        discover_builtin_tools()
        toolsets = sorted(
            _get_platform_tools(
                config,
                platform,
                discover_plugins=False,
                probe_credentials=False,
            )
        )
        requested: set[str] = set()
        for name in toolsets:
            requested.update(resolve_toolset(name))
            requested.update(registry.get_tool_names_for_toolset(name))
        definitions = registry.get_definitions(requested, quiet=True)
        eligible = {
            definition.get("function", {}).get("name")
            for definition in definitions
            if isinstance(definition, dict)
            and isinstance(definition.get("function"), dict)
            and isinstance(definition["function"].get("name"), str)
        }
        registered = set(registry.get_all_tool_names())
        for name in TOOLSETS:
            registered.update(resolve_toolset(name, include_registry=False))
    return {
        "state": "VERIFIED",
        "registered_names": _safe_names(registered),
        "eligible_toolsets": _safe_names(toolsets),
        "eligible_names": _safe_names(eligible & registered),
    }


def _mcp_inventory(config: dict[str, Any]) -> dict[str, Any]:
    from hermes_cli.tools_config import enabled_mcp_server_names

    raw = config.get("mcp_servers") or {}
    servers = raw if isinstance(raw, dict) else {}
    configured = {str(name) for name in servers}
    enabled = enabled_mcp_server_names(config)
    return {
        "state": "VERIFIED",
        "configured_names": _safe_names(configured),
        "enabled_names": _safe_names(configured & enabled),
        "disabled_names": _safe_names(configured - enabled),
    }


def _plugin_inventory(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("plugins") or {}
    plugins = raw if isinstance(raw, dict) else {}
    disabled_raw = plugins.get("disabled")
    enabled_raw = plugins.get("enabled")
    disabled = (
        {str(name) for name in disabled_raw}
        if isinstance(disabled_raw, list)
        else set()
    )
    enabled = (
        {str(name) for name in enabled_raw} if isinstance(enabled_raw, list) else set()
    ) - disabled
    return {
        "state": "VERIFIED",
        "enabled_names": _safe_names(enabled),
        "disabled_names": _safe_names(disabled),
    }


def _memory_inventory(home: Path, config: dict[str, Any]) -> dict[str, Any]:
    from tools.memory_tool import MemoryStore

    cfg = config.get("memory") if isinstance(config.get("memory"), dict) else {}
    limits = {
        "memory": int(cfg.get("memory_char_limit", 2200)),
        "user": int(cfg.get("user_char_limit", 1375)),
    }
    result: dict[str, Any] = {"state": "VERIFIED", "limits": limits}
    memory_dir = home / "memories"
    for key, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
        path = memory_dir / filename
        try:
            data = path.read_bytes() if path.is_file() else b""
            result[key] = {
                "entry_count": len(MemoryStore._read_file(path)),
                "snapshot_identity": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        except OSError:
            result[key] = {"state": "INACCESSIBLE", "entry_count": 0}
    return result


def _config_inaccessible_setup_sections() -> dict[str, dict[str, Any]]:
    return {
        "tools": {
            "state": "INACCESSIBLE",
            "registered_names": [],
            "eligible_toolsets": [],
            "eligible_names": [],
        },
        "plugins": {
            "state": "INACCESSIBLE",
            "enabled_names": [],
            "disabled_names": [],
        },
        "mcp": {
            "state": "INACCESSIBLE",
            "configured_names": [],
            "enabled_names": [],
            "disabled_names": [],
        },
        "safeguards": {
            "state": "INACCESSIBLE",
            "approval_mode": None,
            "configured_approval_mode": None,
            "effective_approval_mode": None,
            "approval_process_yolo_override": None,
            "approval_current_session_yolo_override": None,
            "approval_historical_session_override": {"state": "INACCESSIBLE"},
            "approvals_configured": None,
            "checkpoints_enabled": None,
            "checkpoints_enabled_configured": None,
            "checkpoints_enabled_profile_default": None,
            "checkpoints_enabled_effective": {"state": "INACCESSIBLE"},
            "checkpoint_runtime_override": {"state": "INACCESSIBLE"},
            "checkpoint_configured": None,
        },
    }


def _approval_runtime_overrides() -> tuple[bool | None, bool | None]:
    """Return canonical process/current-session YOLO state without raw env."""
    try:
        from tools import approval

        return (
            bool(approval._YOLO_MODE_FROZEN),
            bool(approval.is_current_session_yolo_enabled()),
        )
    except Exception:
        return None, None


def _normalize_approval_mode(value: Any) -> str:
    try:
        from tools.approval import _normalize_approval_mode as native_normalize

        return native_normalize(value)
    except Exception:
        if isinstance(value, bool):
            return "off" if value is False else "manual"
        if isinstance(value, str) and value.strip().lower() in {
            "manual",
            "smart",
            "off",
        }:
            return value.strip().lower()
        return "manual"


def _safeguards(config: dict[str, Any], home: Path) -> dict[str, Any]:
    approvals_raw = config.get("approvals") or {}
    approvals = approvals_raw if isinstance(approvals_raw, dict) else {}
    configured_mode = approvals.get("mode") if "mode" in approvals else None
    config_mode = _normalize_approval_mode(
        configured_mode if configured_mode is not None else "manual"
    )
    process_yolo, current_session_yolo = _approval_runtime_overrides()
    runtime_bypass = bool(process_yolo) or bool(current_session_yolo)
    effective_mode = "off" if runtime_bypass or config_mode == "off" else config_mode

    checkpoints_raw = config.get("checkpoints")
    if isinstance(checkpoints_raw, bool):
        checkpoints_configured_enabled = checkpoints_raw
    elif isinstance(checkpoints_raw, dict):
        checkpoints_configured_enabled = bool(checkpoints_raw.get("enabled", False))
    else:
        checkpoints_configured_enabled = False

    result: dict[str, Any] = {
        "state": "VERIFIED",
        "approval_mode": effective_mode,
        "configured_approval_mode": config_mode
        if configured_mode is not None
        else None,
        "effective_approval_mode": effective_mode,
        "approval_process_yolo_override": process_yolo,
        "approval_current_session_yolo_override": current_session_yolo,
        "approval_historical_session_override": {"state": "INACCESSIBLE"},
        "approvals_configured": "approvals" in config,
        "checkpoints_enabled": checkpoints_configured_enabled,
        "checkpoints_enabled_configured": checkpoints_configured_enabled,
        "checkpoints_enabled_profile_default": checkpoints_configured_enabled,
        "checkpoints_enabled_effective": {
            "state": "INACCESSIBLE",
            "profile_default": checkpoints_configured_enabled,
        },
        "checkpoint_runtime_override": {"state": "INACCESSIBLE"},
        "checkpoint_configured": "checkpoints" in config,
    }
    try:
        from tools.checkpoint_manager import store_status

        status = store_status(home / "checkpoints")
        result["checkpoint_status"] = {
            "state": "VERIFIED",
            "project_count": int(status.get("project_count", 0)),
            "total_size_bytes": int(status.get("total_size_bytes", 0)),
        }
    except (OSError, ValueError):
        result["checkpoint_status"] = {"state": "INACCESSIBLE"}
    return result


def _runtime_context(prompt: str) -> dict[str, Any]:
    """Describe only the context source proven present in the frozen prompt."""
    marker = "# Project Context\n\nThe following project context files have been loaded"
    start = prompt.find(marker)
    if start < 0:
        return {"state": "VERIFIED", "shown": False, "kind": None}
    rendered = prompt[start:]
    heading = re.search(r"(?m)^##\s+([^\n]+)$", rendered)
    if not heading:
        return {"state": "INACCESSIBLE", "shown": True, "kind": None}
    basename = Path(heading.group(1).strip()).name.lower()
    if basename in {".hermes.md", "hermes.md"}:
        kind = "hermes"
    elif basename == "agents.md":
        kind = "agents"
    elif basename == "claude.md":
        kind = "claude"
    elif basename == ".cursorrules" or basename.endswith(".mdc"):
        kind = "cursorrules"
    else:
        kind = None
    return {
        "state": "VERIFIED" if kind else "INACCESSIBLE",
        "shown": True,
        "kind": kind,
    }


def _shown_skill_names(prompt: str) -> set[str]:
    """Parse the native rendered index, including names-only categories."""
    skills_start = prompt.find("## Skills (mandatory)")
    project_start = prompt.find("# Project Context")
    if skills_start < 0 or (0 <= project_start < skills_start):
        return set()
    open_tag = prompt.find("<available_skills>", skills_start)
    close_tag = prompt.find("</available_skills>", open_tag)
    if open_tag < 0 or close_tag < 0:
        return set()

    shown: set[str] = set()
    rendered = prompt[open_tag + len("<available_skills>") : close_tag]
    for line in rendered.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            candidate = stripped[2:].split(": ", 1)[0].strip()
            if _SKILL_NAME_RE.fullmatch(candidate):
                shown.add(candidate)
        elif "[names only]:" in stripped:
            for candidate in stripped.split("[names only]:", 1)[1].split(","):
                candidate = candidate.strip()
                if _SKILL_NAME_RE.fullmatch(candidate):
                    shown.add(candidate)
    return shown


def _sqlite_signature(
    db: Path,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Return size/mtime signatures for a SQLite database and its WAL."""
    signatures: list[tuple[int, int] | None] = []
    for path in (db, Path(str(db) + "-wal")):
        try:
            stat = path.stat()
            signatures.append((stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            signatures.append(None)
    return signatures[0], signatures[1]


@contextmanager
def _sqlite_read_snapshot(db: Path):
    """Copy SQLite state to scratch without disturbing live-process locks."""
    from hermes_cli.sqlite_safe_read import LiveConnectionError, offline_file_access

    with tempfile.TemporaryDirectory(prefix="hermes-harness-health-db-") as scratch:
        snapshot = Path(scratch) / "state.db"
        try:
            # Raw opens/closes can cancel this process's POSIX advisory locks.
            # Hold the native lifecycle guard across the complete source bundle
            # copy and its stability check; queries happen only on the scratch
            # copy after the guard is released.
            with offline_file_access(db, what="snapshot for Harness Health"):
                before = _sqlite_signature(db)
                shutil.copy2(db, snapshot)
                source_wal = Path(str(db) + "-wal")
                if before[1] is not None:
                    shutil.copy2(source_wal, Path(str(snapshot) + "-wal"))
                if _sqlite_signature(db) != before:
                    raise OSError(
                        "session database changed while creating read-only snapshot"
                    )
        except LiveConnectionError as exc:
            raise OSError("session database has a live in-process connection") from exc
        yield snapshot


def _empty_runtime() -> dict[str, Any]:
    return {
        "state": "INACCESSIBLE",
        "context": {},
        "session_cwd": {"state": "INACCESSIBLE"},
        "skills": {},
        "tools": {},
        "plugins": {"state": "INACCESSIBLE", "used_names": []},
        "mcp": {"state": "INACCESSIBLE", "used_names": []},
    }


def _read_runtime(
    home: Path,
    cwd: Path,
    session_id: str | None,
) -> tuple[dict[str, Any], list[str]]:
    runtime = _empty_runtime()
    blind: list[str] = []
    if not session_id:
        blind.append("No session selected; per-run evidence is inaccessible.")
        return runtime, blind
    db = home / "state.db"
    if not db.is_file():
        blind.append("Session database is absent; runtime evidence is inaccessible.")
        return runtime, blind
    try:
        with _sqlite_read_snapshot(db) as snapshot_db:
            uri = f"file:{quote(str(snapshot_db.resolve()))}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                session_cols = {
                    row[1] for row in conn.execute("PRAGMA table_info(sessions)")
                }
                message_cols = {
                    row[1] for row in conn.execute("PRAGMA table_info(messages)")
                }
                if not {"id", "system_prompt"}.issubset(session_cols) or not {
                    "session_id",
                    "role",
                    "tool_calls",
                    "tool_name",
                    "tool_call_id",
                    "content",
                    "active",
                }.issubset(message_cols):
                    raise sqlite3.DatabaseError("required session columns unavailable")
                select_cols = (
                    "system_prompt, cwd"
                    if "cwd" in session_cols
                    else "system_prompt, NULL"
                )
                row = conn.execute(
                    f"SELECT {select_cols} FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    blind.append("Selected session was not found.")
                    return runtime, blind
                messages = conn.execute(
                    "SELECT role, tool_calls, tool_name, tool_call_id, "
                    "CASE WHEN tool_name = 'skill_view' THEN content ELSE NULL END "
                    "FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                    (session_id,),
                ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        blind.append(f"Session evidence inaccessible ({type(exc).__name__}).")
        return runtime, blind

    prompt = row[0] or ""
    session_cwd_raw = row[1]
    session_cwd: dict[str, Any] = {"state": "INACCESSIBLE"}
    if isinstance(session_cwd_raw, str) and session_cwd_raw.strip():
        session_cwd_path = Path(session_cwd_raw).expanduser()
        session_cwd = {
            "state": "VERIFIED",
            "path": _portable(session_cwd_path, home=home, cwd=cwd),
            "identity": _identity(session_cwd_path),
        }
    shown = _shown_skill_names(prompt)
    preloaded = set(
        re.findall(
            r'with the "([A-Za-z0-9][A-Za-z0-9_.:/-]{0,127})" skill preloaded',
            prompt,
        )
    )
    used: set[str] = set()
    viewed: set[str] = set()
    pending_skill_views: dict[str, str] = {}
    pending_tool_calls: dict[str, str] = {}
    matched_result_ids: set[str] = set()
    checked_tool_names: set[str] = set()
    for role, raw_calls, tool_name, tool_call_id, result_content in messages:
        if role == "tool":
            if (
                isinstance(tool_call_id, str)
                and pending_tool_calls.get(tool_call_id) == tool_name
            ):
                matched_result_ids.add(tool_call_id)
                if isinstance(tool_name, str):
                    checked_tool_names.add(tool_name)
            if tool_name == "skill_view" and tool_call_id in pending_skill_views:
                try:
                    payload = json.loads(result_content or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict) and payload.get("success") is True:
                    viewed.add(pending_skill_views[tool_call_id])
        if not raw_calls:
            continue
        try:
            calls = json.loads(raw_calls)
        except (TypeError, json.JSONDecodeError):
            continue
        for call in calls if isinstance(calls, list) else []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            name = fn.get("name")
            if isinstance(name, str):
                used.add(name)
                call_id = call.get("id") if isinstance(call, dict) else None
                if isinstance(call_id, str):
                    pending_tool_calls[call_id] = name
            if name == "skill_view":
                try:
                    skill = json.loads(fn.get("arguments") or "{}").get("name")
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if (
                        isinstance(skill, str)
                        and _SKILL_NAME_RE.fullmatch(skill)
                        and isinstance(call_id, str)
                    ):
                        pending_skill_views[call_id] = skill
                except (TypeError, json.JSONDecodeError):
                    pass

    plugin_owner_by_tool: dict[str, str] = {}
    try:
        import hermes_cli.plugins as plugin_runtime

        manager = getattr(plugin_runtime, "_plugin_manager", None)
        if manager is not None:
            for key, loaded in manager._plugins.items():
                owner = loaded.manifest.key or key
                for registered_name in loaded.tools_registered:
                    plugin_owner_by_tool[registered_name] = owner
    except Exception:
        pass

    plugin_used: set[str] = set()
    plugin_used_tools: set[str] = set()
    plugin_state = "INACCESSIBLE"
    mcp_used: set[str] = set()
    mcp_used_tools: set[str] = set()
    unknown_ownership: set[str] = set()
    try:
        from tools.registry import registry

        for name in used:
            toolset = registry.get_toolset_for_tool(name)
            if name in plugin_owner_by_tool:
                plugin_used.add(plugin_owner_by_tool[name])
                plugin_used_tools.add(name)
                plugin_state = "VERIFIED"
            elif toolset and toolset.startswith("plugin-"):
                plugin_used.add(toolset.removeprefix("plugin-"))
                plugin_used_tools.add(name)
                if plugin_state != "VERIFIED":
                    plugin_state = "INFERRED"
            elif toolset and toolset.startswith("mcp-"):
                mcp_used.add(toolset.removeprefix("mcp-"))
                mcp_used_tools.add(name)
            elif toolset is None:
                unknown_ownership.add(name)
    except Exception:
        unknown_ownership.update(used)

    matched_result_count = len(matched_result_ids)
    runtime = {
        "state": "VERIFIED",
        "context": _runtime_context(prompt),
        "session_cwd": session_cwd,
        "skills": {
            "shown_names": _safe_names(shown),
            "preloaded_names": _safe_names(preloaded),
            "viewed_names": _safe_names(viewed),
            "loaded_names": _safe_names(preloaded | viewed),
        },
        "tools": {
            "used_names": _safe_names(used),
            "checked_names": _safe_names(checked_tool_names),
            "result_count": matched_result_count,
            "matched_result_count": matched_result_count,
            "unattributed_names": _safe_names(unknown_ownership),
        },
        "plugins": {
            "state": plugin_state if plugin_used else "INACCESSIBLE",
            "used_names": _safe_names(plugin_used),
            "used_tool_names": _safe_names(plugin_used_tools),
        },
        "mcp": {
            "state": "VERIFIED" if mcp_used else "INACCESSIBLE",
            "used_names": _safe_names(mcp_used),
            "used_tool_names": _safe_names(mcp_used_tools),
        },
    }
    blind.extend([
        "Credential-gated toolset auto-enablement is not probed by the read-only audit.",
        "Exact historical tool-schema exposure is not persisted.",
        "Historical session YOLO and checkpoint CLI overrides are not persisted.",
        "Semantic consultation and operator acceptance are not persisted.",
    ])
    if plugin_state == "INFERRED":
        blind.append(
            "Plugin ownership inferred from the plugin-* toolset naming convention."
        )
    elif not plugin_used:
        blind.append(
            "Historical plugin tool ownership is inaccessible unless loaded "
            "registry ownership proves it."
        )
    if not mcp_used:
        blind.append(
            "Historical MCP tool ownership is inaccessible unless registry "
            "ownership proves it."
        )
    if unknown_ownership:
        blind.append(
            "Some historical tool names are no longer registered, so ownership "
            "is inaccessible."
        )
    return runtime, blind


def _funnel(_setup: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """Build one causal selected-run tool trace; setup inventory stays separate."""
    if runtime["state"] == "INACCESSIBLE":
        values = (0, 0, 0, 0, 0, 0, 0)
        states = ("INACCESSIBLE",) * 7
    else:
        acted = len(runtime.get("tools", {}).get("used_names", []))
        checked = len(runtime.get("tools", {}).get("checked_names", []))
        values = (acted, acted, acted, acted, acted, checked, 0)
        states = (
            "INFERRED",
            "INFERRED",
            "INFERRED",
            "INFERRED",
            "VERIFIED",
            "VERIFIED",
            "INACCESSIBLE",
        )
    funnel = {
        "completeness": "PARTIAL",
        "population": "selected-session tool capabilities",
        "stages": [
            {
                "name": name,
                "state": state,
                "count": count,
                "basis": "persisted selected-session tool-call trace",
            }
            for name, state, count in zip(FUNNEL_STAGES, states, values)
        ],
    }
    validate_funnel(funnel)
    return funnel


def validate_funnel(funnel: dict[str, Any]) -> None:
    stages = funnel.get("stages") or []
    names = [stage.get("name") for stage in stages]
    if names != list(FUNNEL_STAGES):
        if funnel.get("completeness") == "COMPLETE":
            raise ValueError("COMPLETE trace must contain every ordered funnel stage")
        raise ValueError("funnel stages must be present in contract order")
    if funnel.get("completeness") == "COMPLETE" and any(
        stage.get("state") == "INACCESSIBLE" for stage in stages
    ):
        raise ValueError("COMPLETE trace cannot contain an INACCESSIBLE stage")
    if funnel.get("completeness") == "COMPLETE":
        counts = [stage.get("count") for stage in stages]
        if any(not isinstance(count, int) or count < 0 for count in counts):
            raise ValueError("COMPLETE trace counts must be non-negative integers")
        if any(later > earlier for earlier, later in zip(counts, counts[1:])):
            raise ValueError("COMPLETE trace counts must follow causal funnel order")


def collect_harness_health(
    *,
    platform: str = "cli",
    cwd: str | Path | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Collect a deterministic report without mutating the target profile."""
    from hermes_cli.config import read_raw_config

    home = get_hermes_home()
    workdir = Path(cwd or Path.cwd()).resolve()
    config_blind: list[str] = []
    try:
        config = read_raw_config(backup_corrupt=False, raise_on_error=True)
        config_state = "VERIFIED"
    except Exception as exc:
        config = {}
        config_state = "INACCESSIBLE"
        config_blind.append(
            f"Profile config is inaccessible ({type(exc).__name__}); "
            "config-derived setup is inaccessible."
        )
    inaccessible_sections = _config_inaccessible_setup_sections()
    if config_state == "VERIFIED":
        config_sections: dict[str, dict[str, Any]] = {}
        collectors = {
            "tools": lambda: _tool_inventory(config, platform),
            "plugins": lambda: _plugin_inventory(config),
            "mcp": lambda: _mcp_inventory(config),
            "safeguards": lambda: _safeguards(config, home),
        }
        for section, collector in collectors.items():
            try:
                config_sections[section] = collector()
            except Exception as exc:
                config_sections[section] = inaccessible_sections[section]
                config_blind.append(
                    f"{section.capitalize()} setup evidence inaccessible "
                    f"({type(exc).__name__})."
                )
    else:
        config_sections = inaccessible_sections
    try:
        memory = _memory_inventory(home, config)
    except Exception as exc:
        memory = {"state": "INACCESSIBLE"}
        config_blind.append(
            f"Memory setup evidence inaccessible ({type(exc).__name__})."
        )
    if config_state != "VERIFIED":
        memory["state"] = "INFERRED"
        memory["config_state"] = "INACCESSIBLE"
    skills = _skill_inventory(platform, home)
    if config_state != "VERIFIED":
        skills.update({
            "state": "INFERRED",
            "eligibility_state": "INACCESSIBLE",
            "eligible_names": [],
            "disabled_names": [],
            "ineligible_names": [],
        })
    setup = {
        "config_state": config_state,
        "context": _context_inventory(workdir, home),
        "skills": skills,
        "tools": config_sections["tools"],
        "plugins": config_sections["plugins"],
        "mcp": config_sections["mcp"],
        "memory": memory,
        "safeguards": config_sections["safeguards"],
    }
    runtime, blind = _read_runtime(home, workdir, session_id)
    blind = config_blind + blind
    return {
        "schema_version": "0.1",
        "evidence_states": list(EVIDENCE_STATES),
        "profile": {"identity": _identity(home)},
        "scope": {
            "platform": _safe_name(platform),
            "cwd": "$CWD",
            "session_identity": _text_identity(session_id) if session_id else None,
        },
        "setup": setup,
        "runtime": runtime,
        "funnel": _funnel(setup, runtime),
        "blind_spots": blind,
    }


def write_report_exclusive(report: dict[str, Any], destination: str | Path) -> None:
    """Write JSON exclusively; an existing destination is never changed."""
    output_path = Path(destination)
    try:
        output_path.resolve().relative_to(get_hermes_home().resolve())
    except ValueError:
        pass
    else:
        raise ValueError("Harness Health output must be outside the target profile")
    with output_path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def render_report(report: dict[str, Any]) -> str:
    runtime = report["runtime"]
    return "\n".join((
        f"Harness health v{report['schema_version']} ({report['scope']['platform']})",
        f"Runtime evidence: {runtime['state']}",
        f"Skills: {len(report['setup']['skills']['eligible_names'])} eligible; "
        f"tools: {len(report['setup']['tools']['eligible_names'])} eligible",
        f"Funnel: {report['funnel']['completeness']} "
        f"({len(report['blind_spots'])} blind spots)",
    ))


def cmd_harness_health(args: Any) -> None:
    report = collect_harness_health(
        platform=args.platform,
        cwd=args.cwd,
        session_id=args.session,
    )
    if args.output:
        write_report_exclusive(report, args.output)
    print(
        json.dumps(report, ensure_ascii=False, indent=2)
        if args.json
        else render_report(report)
    )
