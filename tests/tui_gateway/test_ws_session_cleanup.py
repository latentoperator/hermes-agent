"""Regression tests for dashboard WebSocket session cleanup."""

from __future__ import annotations

import pytest

from tui_gateway import server
from tui_gateway.ws import handle_ws


class _FakeDisconnect(Exception):
    pass


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[str] = []
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive_text(self) -> str:
        raise _FakeDisconnect()

    async def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self) -> None:
        self.closed = False

    async def write_async(self, _obj: dict) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _FakeWorker:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_ws_disconnect_can_close_owned_sessions(monkeypatch):
    """Dashboard sidecar WS sessions must not leave slash_worker children behind.

    A plain TUI stdio process exits wholesale, so atexit cleanup catches its
    slash workers. The dashboard /api/ws sidecar is different: closing the
    browser-side WebSocket does not exit the dashboard server process. Any
    session created by that WS must therefore be explicitly finalized and have
    its slash_worker closed when the socket disconnects.
    """

    monkeypatch.setattr("tui_gateway.ws._WebSocketDisconnect", _FakeDisconnect)
    monkeypatch.setattr(server, "resolve_skin", lambda: {"name": "test"})
    monkeypatch.setattr(server, "_stdio_transport", object())
    torn_down: list[tuple[dict, str]] = []

    def fake_teardown(session, *, end_reason="tui_close"):
        torn_down.append((session, end_reason))
        session["slash_worker"].close()

    monkeypatch.setattr(server, "_teardown_session", fake_teardown)

    ws = _FakeWebSocket()
    worker = _FakeWorker()
    transport_marker = _FakeTransport()
    owned_session = {"transport": transport_marker, "slash_worker": worker}
    other_session = {"transport": object(), "slash_worker": _FakeWorker()}
    monkeypatch.setattr(
        server,
        "_sessions",
        {"owned": owned_session, "other": other_session},
    )

    await handle_ws(ws, close_sessions_on_disconnect=True, transport_factory=lambda _ws, _loop: transport_marker)

    assert ws.accepted is True
    assert ws.closed is True
    assert worker.closed is True
    assert torn_down == [(owned_session, "ws_disconnect")]
    assert "owned" not in server._sessions
    assert "other" in server._sessions
