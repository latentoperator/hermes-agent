"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """A fresh cache for this exact checkout skips the network update probe."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps({
            "ts": time.time(),
            "behind": 3,
            "rev": None,
            "ver": __version__,
            "source_rev": "new-head",
            "source_path": str(repo_dir),
        })
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with (
        patch("hermes_cli.banner._resolve_repo_dir", return_value=repo_dir),
        patch("hermes_cli.banner._git_stdout", return_value="new-head"),
        patch("hermes_cli.banner._check_via_local_git") as mock_check,
    ):
        result = check_for_updates()

    assert result == 3
    mock_check.assert_not_called()


def test_check_for_updates_invalidates_cache_after_same_version_rebase(
    tmp_path, monkeypatch
):
    """A changed checkout HEAD cannot reuse a same-version behind count."""
    from hermes_cli import __version__
    from hermes_cli.banner import check_for_updates

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    (tmp_path / ".update_check").write_text(
        json.dumps({
            "ts": time.time(),
            "behind": 7,
            "rev": None,
            "ver": __version__,
            "source_rev": "old-head",
            "source_path": str(repo_dir),
        })
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with (
        patch("hermes_cli.banner._resolve_repo_dir", return_value=repo_dir),
        patch("hermes_cli.banner._git_stdout", return_value="new-head"),
        patch("hermes_cli.banner._check_via_local_git", return_value=0) as mock_check,
    ):
        result = check_for_updates()

    assert result == 0
    mock_check.assert_called_once_with(repo_dir)


def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5
