"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

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


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0.
    """
    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


def test_dispatcher_profile_pin_matching_is_case_insensitive():
    from gateway.kanban_watchers import _profile_matches_dispatcher_pin

    assert _profile_matches_dispatcher_pin("wren", "WREN") is True
    assert _profile_matches_dispatcher_pin("rocket", "wren") is False
    assert _profile_matches_dispatcher_pin("rocket", "") is True


def test_dispatcher_watcher_skips_unpinned_profile():
    """A gateway whose profile does not match kanban.dispatcher_profile must
    not even attempt the singleton lock.
    """
    import asyncio
    from unittest.mock import patch

    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "rocket"  # type: ignore[attr-defined]

    cfg = {"kanban": {"dispatch_in_gateway": True, "dispatcher_profile": "wren"}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        with patch("gateway.kanban_watchers._acquire_singleton_lock") as acquire:
            asyncio.run(runner._kanban_dispatcher_watcher())

    acquire.assert_not_called()


def test_dispatcher_lock_contention_retries_until_released():
    """The pinned gateway does not opt out forever on a startup-time lock loss;
    it retries and takes the singleton once it becomes available.
    """
    import asyncio
    from unittest.mock import patch

    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    runner._active_profile_name = lambda: "wren"  # type: ignore[attr-defined]
    held_handles: list[object] = []
    sleep_calls = 0

    async def fake_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        # Let the retry loop sleep once after the contended result, then stop
        # after the post-acquire initial delay so the long-running watcher exits.
        if sleep_calls >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_acquire(_lock_path):
        if not held_handles:
            held_handles.append(object())
            return None, "contended"
        return held_handles[-1], "held"

    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatcher_profile": "wren",
            "dispatch_interval_seconds": 1,
            "no_dispatcher_alarm_after_seconds": 0,
        }
    }
    with patch("hermes_cli.config.load_config", return_value=cfg):
        with patch("gateway.kanban_watchers._acquire_singleton_lock", side_effect=fake_acquire) as acquire:
            with patch("gateway.kanban_watchers._release_singleton_lock"):
                with patch("asyncio.sleep", side_effect=fake_sleep):
                    with patch("asyncio.to_thread", side_effect=fake_to_thread):
                        asyncio.run(runner._kanban_dispatcher_watcher())

    assert acquire.call_count >= 2
    assert runner._kanban_dispatcher_lock_handle is None
