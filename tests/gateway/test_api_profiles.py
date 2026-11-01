"""``GET /v1/profiles`` and ``/v1/profiles/{name}/avatar``: the roster external clients draw from.

Exercises the real route table behind the real profile-prefix middleware with distinct
default and named-profile keys, because the boundary that matters is *who may learn which
profiles exist*: the listener key sees ``default`` plus the operator's explicit
``gateway.api_server.roster_profiles`` allowlist; a profile's own key sees only itself.
Being served by the multiplexer is not authorization.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope as ss
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

DEFAULT_KEY = "d" * 32
WORKER_KEY = "w" * 32
OTHER_KEY = "o" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"avatar"


def _profile(root, name, *, key=None, ui_meta=None, avatar=False):
    home = root / name
    home.mkdir(parents=True)
    if key:
        (home / ".env").write_text(f"API_SERVER_KEY={key}\n", encoding="utf-8")
    if ui_meta is not None:
        import hermes_yaml as yaml

        (home / "profile.yaml").write_text(yaml.safe_dump({"ui_meta": ui_meta}), encoding="utf-8")
    if avatar:
        (home / "assets").mkdir()
        (home / "assets" / "avatar.png").write_bytes(PNG)
    return home


def _info(name, is_default):
    return SimpleNamespace(
        name=name, is_default=is_default, model="m", provider="p", description=f"{name} bot",
        display_name=name.title(), skill_count=3, path=f"/secret/{name}",
    )


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Served profiles: ``shared`` reuses the listener key, ``worker``/``other`` have their own keys,
    ``nokey`` has none. ``default``, ``worker`` carry a bot title; several carry an avatar."""
    homes = {
        "default": _profile(tmp_path, "default", ui_meta={"title": "Hermes"}),
        "shared": _profile(tmp_path, "shared", key=DEFAULT_KEY, ui_meta={"title": "Shared"}, avatar=True),
        "worker": _profile(tmp_path, "worker", key=WORKER_KEY, ui_meta={"title": "Worker"}, avatar=True),
        "other": _profile(tmp_path, "other", key=OTHER_KEY, avatar=True),
        "nokey": _profile(tmp_path, "nokey", avatar=True),
    }
    monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve", lambda multiplex=False: list(homes.items()))
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: homes[name])
    monkeypatch.setattr(
        "hermes_cli.profiles.list_profiles",
        lambda lazy_skill_count=False: [_info(n, n == "default") for n in homes],
    )
    _allow(monkeypatch, ["shared"])
    return homes


def _allow(monkeypatch, value):
    """Set gateway.api_server.roster_profiles as the listener config would."""
    cfg = {"gateway": {"api_server": {"roster_profiles": value}}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: cfg)


@pytest_asyncio.fixture
async def client(served):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._api_key = DEFAULT_KEY
    adapter.gateway_runner = type("_Runner", (), {"config": GatewayConfig(multiplex_profiles=True)})()
    ss.set_multiplex_active(True)
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    for method, path, handler in adapter._http_route_table():
        if "/v1/profiles" in path:
            app.router.add_route(method, path, handler)
            app.router.add_route(method, "/p/{profile}" + path, handler)
    async with TestClient(TestServer(app)) as c:
        yield c


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_listener_key_sees_default_plus_allowlist_only(client):
    response = await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))
    assert response.status == 200
    payload = await response.json()
    assert payload["object"] == "list"
    rows = {row["name"]: row for row in payload["profiles"]}
    # Profiles outside the allowlist stay invisible, whatever their keys.
    assert set(rows) == {"default", "shared"}
    assert rows["default"]["is_default"] is True
    assert rows["shared"]["has_avatar"] is True and rows["default"]["has_avatar"] is False
    assert rows["shared"]["ui_meta"] == {"title": "Shared"}


@pytest.mark.asyncio
async def test_shared_key_reentering_root_or_default_gains_nothing(client):
    expected = {
        "/v1/profiles": {"default", "shared"},
        "/p/default/v1/profiles": {"default", "shared"},
        "/p/shared/v1/profiles": {"shared"},
    }
    for path, names in expected.items():
        response = await client.get(path, headers=_auth(DEFAULT_KEY))
        assert response.status == 200
        assert {row["name"] for row in (await response.json())["profiles"]} == names


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["nokey", "worker", "other"])
async def test_listener_key_cannot_read_unauthorized_avatars(client, name):
    for path in (f"/v1/profiles/{name}/avatar", f"/p/default/v1/profiles/{name}/avatar"):
        assert (await client.get(path, headers=_auth(DEFAULT_KEY))).status == 404


@pytest.mark.asyncio
async def test_avatar_symlink_is_not_followed(served, client, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("not an avatar")
    avatar = served["shared"] / "assets" / "avatar.png"
    avatar.unlink()
    avatar.symlink_to(secret)
    response = await client.get("/v1/profiles/shared/avatar", headers=_auth(DEFAULT_KEY))
    assert response.status == 404
    assert b"not an avatar" not in await response.read()
    rows = {r["name"]: r for r in (await (await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))).json())["profiles"]}
    assert rows["shared"]["has_avatar"] is False


@pytest.mark.asyncio
async def test_symlinked_assets_dir_is_not_followed(served, client, tmp_path):
    import shutil

    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(served["shared"] / "assets"), str(elsewhere))
    (served["shared"] / "assets").symlink_to(elsewhere, target_is_directory=True)
    assert (await client.get("/v1/profiles/shared/avatar", headers=_auth(DEFAULT_KEY))).status == 404


@pytest.mark.asyncio
async def test_oversized_avatar_is_refused(served, client):
    (served["shared"] / "assets" / "avatar.png").write_bytes(b"x" * 2_000_001)
    assert (await client.get("/v1/profiles/shared/avatar", headers=_auth(DEFAULT_KEY))).status == 404


@pytest.mark.asyncio
async def test_yaml_dates_in_ui_meta_do_not_break_the_roster(served, client):
    (served["shared"] / "profile.yaml").write_text("ui_meta:\n  anniversary: 2026-01-01\n", encoding="utf-8")
    response = await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))
    assert response.status == 200
    rows = {row["name"]: row for row in (await response.json())["profiles"]}
    assert rows["shared"]["ui_meta"] == {"anniversary": "2026-01-01"}
    assert "default" in rows


@pytest.mark.asyncio
async def test_roster_never_exposes_paths_or_secrets(client):
    body = await (await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))).text()
    for leaked in ("/secret/", WORKER_KEY, OTHER_KEY, DEFAULT_KEY, "API_SERVER_KEY"):
        assert leaked not in body


@pytest.mark.asyncio
async def test_profile_key_sees_only_its_own_profile(client):
    response = await client.get("/p/worker/v1/profiles", headers=_auth(WORKER_KEY))
    assert response.status == 200
    assert [row["name"] for row in (await response.json())["profiles"]] == ["worker"]


@pytest.mark.asyncio
async def test_profile_key_cannot_fetch_another_profiles_avatar(client):
    own = await client.get("/p/worker/v1/profiles/worker/avatar", headers=_auth(WORKER_KEY))
    assert own.status == 200 and await own.read() == PNG
    other = await client.get("/p/worker/v1/profiles/other/avatar", headers=_auth(WORKER_KEY))
    assert other.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path,key", [
    ("/v1/profiles", "x" * 32),
    ("/v1/profiles", WORKER_KEY),            # a profile key is not the listener key
    ("/p/worker/v1/profiles", DEFAULT_KEY),  # and the listener key is not a profile key
    ("/p/worker/v1/profiles", OTHER_KEY),
    ("/v1/profiles/worker/avatar", "x" * 32),
])
async def test_wrong_key_is_refused(client, path, key):
    assert (await client.get(path, headers=_auth(key))).status == 401


@pytest.mark.asyncio
async def test_avatar_errors_are_404_not_500(client):
    for name in ("default", "nope", "nokey", "..%2f..%2fetc"):
        response = await client.get(f"/v1/profiles/{name}/avatar", headers=_auth(DEFAULT_KEY))
        assert response.status == 404


@pytest.mark.asyncio
async def test_oversized_ui_meta_is_dropped_not_served(served, client):
    import hermes_yaml as yaml

    (served["shared"] / "profile.yaml").write_text(
        yaml.safe_dump({"ui_meta": {"blob": "x" * 70_000}}), encoding="utf-8")
    response = await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))
    rows = {row["name"]: row for row in (await response.json())["profiles"]}
    assert "ui_meta" not in rows["shared"]


def test_capabilities_advertise_the_roster():
    from gateway.platforms import api_server as mod

    assert mod._STATIC_FEATURE_FLAGS["profile_roster"] is True
    names = {name for name, _ in mod._CAPABILITY_ENDPOINTS}
    assert {"profiles", "profile_avatar"} <= names


def _swap_at_avatar_open(monkeypatch, action):
    """Run *action* immediately before the avatar file itself is opened -- whether the code opens
    it by full path or relative to a directory descriptor -- and report whether it fired."""
    import os

    real_open = os.open
    fired = []

    def spy(path, flags, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == "avatar.png" and not fired:
            fired.append(True)
            action()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    return fired


@pytest.mark.asyncio
async def test_assets_dir_swapped_before_avatar_open_cannot_redirect_the_read(served, client, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "avatar.png").write_bytes(b"OUTSIDE BYTES")
    assets = served["shared"] / "assets"

    def swap():
        assets.rename(served["shared"] / "assets-old")
        assets.symlink_to(outside, target_is_directory=True)

    fired = _swap_at_avatar_open(monkeypatch, swap)
    response = await client.get("/v1/profiles/shared/avatar", headers=_auth(DEFAULT_KEY))
    body = await response.read()
    assert fired, "the swap must actually run at the avatar open"
    assert b"OUTSIDE BYTES" not in body
    assert response.status == 200 and body == PNG  # read through the already-anchored assets fd


@pytest.mark.asyncio
async def test_avatar_swapped_for_fifo_does_not_block(served, client, monkeypatch):
    import asyncio
    import os

    avatar = served["shared"] / "assets" / "avatar.png"

    def swap():
        avatar.unlink()
        os.mkfifo(avatar)

    fired = _swap_at_avatar_open(monkeypatch, swap)
    try:
        response = await asyncio.wait_for(
            client.get("/v1/profiles/shared/avatar", headers=_auth(DEFAULT_KEY)), timeout=5)
    finally:
        # If a regression ever drops O_NONBLOCK, release the blocked reader so teardown can't hang.
        try:
            wfd = os.open(avatar, os.O_WRONLY | os.O_NONBLOCK)
            os.close(wfd)
        except OSError:
            pass
    assert fired
    assert response.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, [], "shared", {"shared": True}, [1, None], ["default"]])
async def test_missing_or_malformed_allowlist_lists_default_only(served, client, monkeypatch, value):
    _allow(monkeypatch, value)
    response = await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))
    assert response.status == 200
    assert {r["name"] for r in (await response.json())["profiles"]} == {"default"}
    assert (await client.get("/v1/profiles/shared/avatar", headers=_auth(DEFAULT_KEY))).status == 404


@pytest.mark.asyncio
async def test_unreadable_config_fails_closed(served, client, monkeypatch):
    def boom(*a, **k):
        raise SystemExit("config backend unavailable")

    monkeypatch.setattr("hermes_cli.config.load_config", boom)
    response = await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))
    assert response.status == 200
    assert {r["name"] for r in (await response.json())["profiles"]} == {"default"}


@pytest.mark.asyncio
async def test_allowlist_names_only_served_profiles(served, client, monkeypatch):
    _allow(monkeypatch, ["shared", "ghost", "WORKER"])
    rows = {r["name"] for r in (await (await client.get(
        "/v1/profiles", headers=_auth(DEFAULT_KEY))).json())["profiles"]}
    assert rows == {"default", "shared", "worker"}  # names are normalized; unknown names ignored
    assert (await client.get("/v1/profiles/ghost/avatar", headers=_auth(DEFAULT_KEY))).status == 404


@pytest.mark.asyncio
async def test_roster_never_resolves_secrets(served, client, monkeypatch):
    """Visibility is configuration, not secret resolution: no hydration, no scope building."""
    from hermes_cli import env_loader
    from tui_gateway import launch_profile_policy

    calls = []
    monkeypatch.setattr(env_loader, "_hydrate_profile_secret_sources",
                        lambda home: calls.append(("hydrate", Path(home).name)) or {})
    real_owner = launch_profile_policy.owner_secret_scope

    def owner(home):
        calls.append(("owner", Path(home).name))
        return real_owner(home)

    monkeypatch.setattr(launch_profile_policy, "owner_secret_scope", owner)
    assert (await client.get("/v1/profiles", headers=_auth(DEFAULT_KEY))).status == 200
    assert (await client.get("/v1/profiles/shared/avatar", headers=_auth(DEFAULT_KEY))).status == 200
    named = {"shared", "worker", "other", "nokey"}
    assert not [c for c in calls if c[1] in named]


@pytest.mark.asyncio
async def test_named_prefix_still_sees_only_itself_even_if_allowlisted(served, client, monkeypatch):
    _allow(monkeypatch, ["shared", "worker", "other", "nokey"])
    response = await client.get("/p/worker/v1/profiles", headers=_auth(WORKER_KEY))
    assert [r["name"] for r in (await response.json())["profiles"]] == ["worker"]
    assert (await client.get("/p/worker/v1/profiles/other/avatar", headers=_auth(WORKER_KEY))).status == 404
