"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_module():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return the module."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    return _load_plugin_module().router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------


def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == kb.VALID_STATUSES - {"archived"}
    for expected in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == ["default"]
    assert data["latest_event_id"] == 0


# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------


def test_board_assignees_include_profiles_on_disk(client, kanban_home):
    """The dashboard create form needs installed profiles, not only used assignees."""
    profiles = kanban_home / "profiles"
    (profiles / "dante").mkdir(parents=True)
    (profiles / "dante" / "config.yaml").write_text("model:\n  default: test\n")
    (profiles / "wren").mkdir(parents=True)
    (profiles / "wren" / "config.yaml").write_text("model:\n  default: test\n")

    response = client.get("/api/plugins/kanban/board")

    assert response.status_code == 200
    data = response.json()
    assert "dante" in data["assignees"]
    assert "wren" in data["assignees"]


def test_board_assignees_honor_hidden_profiles(client, kanban_home):
    profiles = kanban_home / "profiles"
    for name in ("dante", "triss", "syren"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text("model:\n  default: test\n")
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  hidden_assignees:\n    - triss\n    - syren\n"
    )

    data = client.get("/api/plugins/kanban/board").json()

    assert "dante" in data["assignees"]
    assert "triss" not in data["assignees"]
    assert "syren" not in data["assignees"]


def test_dashboard_create_auto_subscribes_home_channels(kanban_home, monkeypatch):
    mod = _load_plugin_module()
    monkeypatch.setattr(
        mod,
        "_configured_home_channels",
        lambda: [
            {
                "platform": "telegram",
                "chat_id": "7593705216",
                "thread_id": "",
                "name": "Home",
            }
        ],
    )
    monkeypatch.setattr(mod, "_active_profile_name", lambda: "default")
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    local_client = TestClient(app)

    response = local_client.post(
        "/api/plugins/kanban/tasks", json={"title": "from dashboard"}
    )

    assert response.status_code == 200, response.text
    task_id = response.json()["task"]["id"]
    with kb.connect() as conn:
        subs = kb.list_notify_subs(conn, task_id)
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "7593705216"
    assert subs[0]["notifier_profile"] == "default"


def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "researcher",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "researcher"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "researcher" in data["assignees"]


def test_dashboard_create_with_supersedes_closes_original(client):
    original = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "overscoped original", "assignee": "wren"},
    ).json()["task"]
    client.patch(
        f"/api/plugins/kanban/tasks/{original['id']}",
        json={"status": "blocked", "block_reason": "split into continuation"},
    )

    response = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "safe continuation",
            "assignee": "wren",
            "parents": [original["id"]],
            "supersedes": [original["id"]],
        },
    )

    assert response.status_code == 200, response.text
    continuation = response.json()["task"]
    assert continuation["status"] == "ready"

    original_detail = client.get(
        f"/api/plugins/kanban/tasks/{original['id']}"
    ).json()
    assert original_detail["task"]["status"] == "done"
    assert original_detail["task"]["result"] == (
        f"Superseded by continuation card {continuation['id']}"
    )
    superseded = [
        event
        for event in original_detail["events"]
        if event["kind"] == "superseded"
    ]
    assert superseded
    assert superseded[-1]["payload"] == {
        "continuation_task_id": continuation["id"],
        "actor": "dashboard",
    }


def test_board_list_recommends_persistent_workspace_for_configured_workdir(
    client, tmp_path
):
    """Board metadata should tell the UI which safe task default to use."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    kb.write_board_metadata("default", default_workdir=str(repo))

    plain_dir = tmp_path / "notes"
    plain_dir.mkdir()
    kb.create_board("notes", default_workdir=str(plain_dir))
    kb.create_board("disposable")

    response = client.get("/api/plugins/kanban/boards")

    assert response.status_code == 200
    boards = {board["slug"]: board for board in response.json()["boards"]}
    assert boards["default"]["default_workspace_kind"] == "worktree"
    assert boards["notes"]["default_workspace_kind"] == "dir"
    assert boards["disposable"]["default_workspace_kind"] == "scratch"


def test_create_board_persists_project_directory(client, tmp_path):
    """The dashboard board form should anchor future tasks to its project."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    response = client.post(
        "/api/plugins/kanban/boards",
        json={
            "slug": "project-board",
            "name": "Project Board",
            "default_workdir": str(project_dir),
        },
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("project-board")["default_workdir"] == str(
        project_dir.resolve()
    )


@pytest.mark.parametrize("path", ["relative/project", "~/missing-project"])
def test_create_board_rejects_invalid_project_directory(client, path):
    """A board must not persist a path that cannot anchor worker output."""
    response = client.post(
        "/api/plugins/kanban/boards",
        json={"slug": "invalid-project", "default_workdir": path},
    )

    assert response.status_code == 400
    assert "project directory" in response.json()["detail"].lower()


def test_patch_board_sets_project_directory(client, tmp_path):
    """Board-level default_workdir must be editable after creation."""
    kb.create_board("late-config")
    project_dir = tmp_path / "late-project"
    project_dir.mkdir()

    response = client.patch(
        "/api/plugins/kanban/boards/late-config",
        json={"default_workdir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    # The recommendation flips from scratch to a persistent kind so the
    # create-task dialog's workspace default follows the board setting.
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("late-config")["default_workdir"] == str(
        project_dir.resolve()
    )


def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])


def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1


def test_dashboard_markdown_html_is_sanitized_before_render():
    """Markdown rendering must sanitize HTML before dangerouslySetInnerHTML."""

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "function sanitizeMarkdownHtml(html)" in js
    assert "MARKDOWN_ALLOWED_TAGS" in js
    assert "sanitizeMarkdownHtml(renderMarkdown(props.source || \"\"))" in js
    assert "dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || \"\") }" not in js


# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------


def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]

    # Events exist from creation.
    assert len(data["events"]) >= 1


# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------


def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_reopen["status"] == "todo"


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------


def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"


# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------


def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket auth (query-param token)
# ---------------------------------------------------------------------------


def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    # No token → policy violation close.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    # Wrong token → policy violation close.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    # Correct token → accepted (connect then close cleanly from our side).
    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz"
    ) as ws:
        assert ws is not None  # handshake succeeded


    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(f"/api/plugins/kanban/tasks/{tid}",
                     json={"status": "blocked", "block_reason": "wait"})

    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert all(r["ok"] for r in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {t["id"] for t in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False


# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------


def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------


_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}


def test_board_endpoint_survives_task_age_exception(client, monkeypatch):
    """If task_age raises for any reason, GET /board must NOT 500.

    Pre-fix behavior (without the try/except in _task_dict): a single corrupt
    row turned the entire board response into a 500. The fallback dict lets
    the dashboard render every other card normally.
    """
    create = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "doomed", "assignee": "alice"},
    )
    assert create.status_code == 200, create.text

    # Force task_age to raise an exception type _safe_int does NOT handle —
    # simulates a future regression where someone re-introduces an unguarded
    # operation in task_age. ValueError on '%s' would be absorbed by _safe_int
    # and never reach the outer try/except, so it would not exercise the
    # contract this test pins.
    def _boom(_task):
        raise RuntimeError("simulated future task_age bug")
    monkeypatch.setattr("hermes_cli.kanban_db.task_age", _boom)

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200, r.text

    payload = r.json()
    # /board returns columns as a list of {name, tasks} — not a dict — so
    # flatten across all columns to find our seeded task.
    tasks = [t for col in payload["columns"] for t in col["tasks"]]
    assert len(tasks) == 1, f"expected exactly the seeded task, got {tasks!r}"
    # Strict equality: the literal fallback dict from plugin_api._task_dict
    # is the published contract the dashboard UI relies on. Key renames or
    # silent additions should fail this test on purpose.
    assert tasks[0]["age"] == _FALLBACK_AGE


def test_single_task_endpoint_survives_task_age_exception(client, monkeypatch):
    """GET /tasks/:id also calls _task_dict — same fallback should kick in.

    This is the "drawer view" path: the user clicks one card and we serialize
    just that task. A corrupt timestamp on a single task should not block the
    user from opening its drawer.
    """
    create = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "drawer-target", "assignee": "bob"},
    )
    task_id = create.json()["task"]["id"]

    def _boom(_task):
        raise RuntimeError("simulated future task_age bug")
    monkeypatch.setattr("hermes_cli.kanban_db.task_age", _boom)

    r = client.get(f"/api/plugins/kanban/tasks/{task_id}")
    assert r.status_code == 200, r.text
    assert r.json()["task"]["age"] == _FALLBACK_AGE


def test_create_task_probe_error_does_not_break_create(client, monkeypatch):
    """Probe failure must never break task creation."""
    def _raise(**kw):
        raise RuntimeError("probe crashed")
    monkeypatch.setattr(
        "hermes_cli.kanban._check_dispatcher_presence", _raise,
    )
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "resilient", "assignee": "worker"},
    )
    assert r.status_code == 200
    assert r.json()["task"]["title"] == "resilient"



# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


def test_home_channels_no_task_id_all_unsubscribed(client, with_home_channels):
    """Without task_id, every entry's subscribed=false (UI "no task" state)."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    assert all(not h["subscribed"] for h in r.json()["home_channels"])


def test_home_subscribe_creates_notify_sub_row(client, with_home_channels):
    """POST .../home-subscribe/telegram writes a kanban_notify_subs row
    keyed to the telegram home's (chat_id, thread_id)."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="x")

    r = client.post(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task_id)
    finally:
        conn.close()
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "1234567"
    assert subs[0]["thread_id"] == "42"
    assert subs[0]["notifier_profile"] == "default"


def test_home_subscribe_flips_subscribed_flag_in_subsequent_get(client, with_home_channels):
    """After subscribe, the GET endpoint reports subscribed=true for that
    platform and false for the others."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="x")
    client.post(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")

    r = client.get(f"/api/plugins/kanban/home-channels?task_id={task_id}")
    flags = {h["platform"]: h["subscribed"] for h in r.json()["home_channels"]}
    assert flags == {"telegram": True, "discord": False}


def test_home_subscribe_is_idempotent(client, with_home_channels):
    """Re-subscribing keeps a single row at the DB layer."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="x")
    client.post(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")
    client.post(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, task_id)) == 1
    finally:
        conn.close()


def test_home_subscribe_backfills_owner_on_legacy_row(client, with_home_channels):
    """Re-subscribing should backfill notifier ownership on ownerless rows."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="x")

    conn = kb.connect()
    try:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="1234567",
            thread_id="42",
        )
    finally:
        conn.close()

    r = client.post(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")
    assert r.status_code == 200

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task_id)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["notifier_profile"] == "default"


def test_home_subscribe_unknown_platform_returns_404(client, with_home_channels):
    """Platforms without a home configured (slack in the fixture) return 404."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(f"/api/plugins/kanban/tasks/{t['id']}/home-subscribe/slack")
    assert r.status_code == 404
    assert "slack" in r.json()["detail"]


def test_home_subscribe_unknown_task_returns_404(client, with_home_channels):
    r = client.post("/api/plugins/kanban/tasks/t_nonexistent/home-subscribe/telegram")
    assert r.status_code == 404


def test_home_unsubscribe_removes_notify_sub_row(client, with_home_channels):
    """DELETE .../home-subscribe/telegram removes the matching row."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="x")
    client.post(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")
    r = client.delete(f"/api/plugins/kanban/tasks/{task_id}/home-subscribe/telegram")
    assert r.status_code == 200

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, task_id) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------


def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()


def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------


def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent, summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]


# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------


def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    # specify_task routes through call_llm now (#35566) — mock it directly.
    fake_call = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    return fake_call


def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps(
            {"title": "Polished", "body": "**Goal**\nDo the thing."}
        ),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------


