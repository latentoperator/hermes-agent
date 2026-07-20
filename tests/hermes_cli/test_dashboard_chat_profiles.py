"""Tests for profile-aware dashboard embedded Chat PTY launches."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import web_server

pytestmark = pytest.mark.xdist_group("dashboard_auth_app_state")


def test_resolve_chat_argv_sets_profile_home(monkeypatch, tmp_path):
    """Selecting a profile for dashboard Chat must isolate the PTY child.

    The dashboard server itself may run from the default Hermes home, but the
    embedded TUI child has to inherit the selected profile's HERMES_HOME so
    config, memory, tools, sessions, and skills stay profile-scoped.
    """

    profile_home = tmp_path / "profiles" / "wren"
    profile_home.mkdir(parents=True)

    monkeypatch.setattr(
        "hermes_cli.main._make_tui_argv",
        lambda tui_dir, tui_dev: (["node", "entry.js"], Path("/tmp/tui")),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda value: value.lower(),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.validate_profile_name",
        lambda value: None,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda value: value == "wren",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda value: profile_home,
    )

    argv, cwd, env = web_server._resolve_chat_argv(profile="wren")

    assert argv == ["node", "entry.js"]
    assert cwd == "/tmp/tui"
    assert env["HERMES_HOME"] == str(profile_home)
    assert env["HERMES_PROFILE"] == "wren"


def test_resolve_chat_argv_rejects_missing_profile(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.main._make_tui_argv",
        lambda tui_dir, tui_dev: (["node", "entry.js"], Path("/tmp/tui")),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.normalize_profile_name",
        lambda value: value.lower(),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.validate_profile_name",
        lambda value: None,
    )
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda value: False)

    with pytest.raises(web_server.HTTPException) as exc_info:
        web_server._resolve_chat_argv(profile="missing")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Profile 'missing' does not exist."
