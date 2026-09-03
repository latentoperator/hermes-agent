"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


@pytest.mark.parametrize(
    ("active_profile", "dispatcher_profile", "expected"),
    [
        ("wren", "WREN", True),
        ("", "default", True),
        ("DEFAULT", "default", True),
        ("code-reviewer", "wren", False),
        ("code-reviewer", "", True),
        ("code-reviewer", None, True),
    ],
)
def test_dispatcher_profile_pin_matching(
    active_profile,
    dispatcher_profile,
    expected,
):
    """Pins are case-insensitive; empty pins preserve legacy ownership."""
    from gateway.kanban_watchers import _profile_matches_dispatcher_pin

    assert (
        _profile_matches_dispatcher_pin(active_profile, dispatcher_profile) is expected
    )


def test_dispatcher_watcher_skips_nonmatching_profile_before_lock_attempt():
    """Only the pinned profile may compete for the machine-global lock."""
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "code-reviewer"  # type: ignore[attr-defined]
    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatcher_profile": "wren",
        }
    }

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("gateway.kanban_watchers._acquire_singleton_lock") as acquire,
    ):
        asyncio.run(runner._kanban_dispatcher_watcher())

    acquire.assert_not_called()


def test_unset_dispatcher_pin_preserves_legacy_contention_behavior():
    """Without an owner pin, a contended gateway opts out as it always did."""
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "code-reviewer"  # type: ignore[attr-defined]
    sleep_calls = []
    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 600,
        }
    }

    async def record_sleep(delay):
        sleep_calls.append(delay)
        runner._running = False

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(None, "contended"),
        ) as acquire,
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=record_sleep),
    ):
        asyncio.run(runner._kanban_dispatcher_watcher())

    acquire.assert_called_once()
    assert sleep_calls == []


def test_dispatcher_watcher_retries_boundedly_then_acquires_and_releases():
    """The pinned gateway recovers after an incumbent releases the lock."""
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "wren"  # type: ignore[attr-defined]
    handle = object()
    attempts = 0
    sleep_calls = []

    def fake_acquire(_lock_path):
        nonlocal attempts
        attempts += 1
        return (None, "contended") if attempts == 1 else (handle, "held")

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        if attempts >= 2:
            # Stop at the normal post-lock startup delay, before a dispatch tick.
            runner._running = False

    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatcher_profile": "WREN",
            # Lock recovery must not inherit an arbitrarily long dispatch tick.
            "dispatch_interval_seconds": 600,
        }
    }
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            side_effect=fake_acquire,
        ) as acquire,
        patch("gateway.kanban_watchers._release_singleton_lock") as release,
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=fake_sleep),
    ):
        asyncio.run(runner._kanban_dispatcher_watcher())

    assert acquire.call_count == 2
    # Five interruptible one-second slices cap lock recovery at five seconds;
    # the final 5 is the existing post-acquisition startup delay.
    assert sleep_calls == [1.0, 1.0, 1.0, 1.0, 1.0, 5]
    release.assert_called_once_with(handle)
    assert runner._kanban_dispatcher_lock_handle is None


def test_dispatcher_watcher_shutdown_while_waiting_stops_retrying():
    """Gateway shutdown interrupts lock contention without dispatching."""
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "wren"  # type: ignore[attr-defined]
    sleep_calls = []

    async def stop_gateway(delay):
        sleep_calls.append(delay)
        runner._running = False

    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatcher_profile": "wren",
            "dispatch_interval_seconds": 600,
        }
    }
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(None, "contended"),
        ) as acquire,
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=stop_gateway),
    ):
        asyncio.run(runner._kanban_dispatcher_watcher())

    acquire.assert_called_once()
    assert sleep_calls == [1.0]
    assert runner._kanban_dispatcher_lock_handle is None


def test_dispatcher_watcher_cancellation_after_acquire_releases_lock():
    """Cancellation during startup cannot strand the singleton lock."""
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "wren"  # type: ignore[attr-defined]
    handle = object()
    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatcher_profile": "wren",
        }
    }

    async def cancel_startup(_delay):
        raise asyncio.CancelledError

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(handle, "held"),
        ),
        patch("gateway.kanban_watchers._release_singleton_lock") as release,
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=cancel_startup),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(runner._kanban_dispatcher_watcher())

    release.assert_called_once_with(handle)
    assert runner._kanban_dispatcher_lock_handle is None


def test_dispatcher_watcher_cancellation_during_interval_releases_lock():
    """Cancellation between steady-state ticks cannot strand the lock."""
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "wren"  # type: ignore[attr-defined]
    handle = object()
    sleep_calls = []
    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatcher_profile": "wren",
            "dispatch_interval_seconds": 600,
            "auto_decompose": False,
        }
    }

    async def fake_thread_call(*_args, **_kwargs):
        return []

    async def cancel_interval(delay):
        sleep_calls.append(delay)
        if delay != 5:
            raise asyncio.CancelledError

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(handle, "held"),
        ),
        patch(
            "gateway.kanban_watchers._to_thread_process_service",
            side_effect=fake_thread_call,
        ),
        patch("gateway.kanban_watchers._release_singleton_lock") as release,
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=cancel_interval),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(runner._kanban_dispatcher_watcher())

    assert sleep_calls == [5, 1.0]
    release.assert_called_once_with(handle)
    assert runner._kanban_dispatcher_lock_handle is None
