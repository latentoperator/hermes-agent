"""Read-only profile roster for external clients (``GET /v1/profiles``).

Since the unified gateway runtime, a profile's roster (bot titles, shapes, colours,
avatars) is reachable only through the dashboard's WebSocket, which redeems tickets minted
on an owner-only control socket. A client that holds just the API-server credential (the
Hermes Browser Extension, Open WebUI, scripts) can reach ``/p/<profile>/`` for each profile
but has no way to learn which profiles exist or how to draw them.

This module serves that roster over the credential those clients already hold. It is
read-only and lists a profile only when the presented key is the one that profile's own
``/p/<profile>/`` route accepts. It does not select paths, credentials or sessions into the
response; ``description`` and ``ui_meta`` are free-form user-authored text returned as written.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Avatars are small icons; a hard cap keeps a mistaken multi-megabyte file off the wire.
_AVATAR_MAX_BYTES = 2_000_000
# A roster is a handful of profiles; a bound keeps a pathological profile directory from
# turning one authenticated GET into an unbounded scan, and ``ui_meta`` (free-form, written by
# Desktop and the extension) is dropped from a row when it is larger than a client could use.
_ROSTER_MAX_PROFILES = 1000
_UI_META_MAX_BYTES = 65_536
_AVATAR_TYPES = (("png", "image/png"), ("jpg", "image/jpeg"), ("webp", "image/webp"))


def _http_routes(self) -> list[tuple[str, str, Any]]:
    async def profiles(request: web.Request) -> web.Response:
        return await _handle_profiles(self, request)

    async def profile_avatar(request: web.Request) -> web.Response:
        return await _handle_profile_avatar(self, request)

    return [
        ("GET", "/v1/profiles", profiles),
        ("GET", "/v1/profiles/{name}/avatar", profile_avatar)]


def _json_error(message: str, status: int, code: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}}, status=status)


def _presented_token(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _profile_key(self, name: str, home: Path) -> str:
    """The usable API_SERVER_KEY that ``/p/<name>/`` would accept, or "" (fail closed).

    Built by ``owner_secret_scope`` -- the same builder the ``/p/<name>/`` scope uses after
    hydration -- but only once the profile's external secret sources are settled (no sources,
    or fully hydrated; the gateway hydrates every served profile at startup). A cold or
    last-failed profile is omitted rather than hydrated here: hydration holds a process-wide
    lock across slow fetches that the synchronous prefix middleware also takes, so fanning it
    out from a roster request could stall the event loop. Never fetches; never takes that lock.
    Resolution exceptions omit the row; an outage that the route itself tolerates (a failed
    source with a local fallback) is reflected as "not settled", i.e. omitted."""
    from hermes_cli.auth import has_usable_secret
    if name == "default":
        key = getattr(self, "_api_key", "") or ""
    else:
        try:
            from hermes_cli.env_loader import secret_sources_settled
            if not secret_sources_settled(home):
                return ""
            from tui_gateway.launch_profile_policy import owner_secret_scope
            key = owner_secret_scope(Path(home)).get("API_SERVER_KEY", "") or ""
        except BaseException as exc:  # ConfigBackendUnavailable is a SystemExit: omit, not crash
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
                raise
            logger.warning("Roster: cannot resolve API key for profile %r: %s", name, type(exc).__name__)
            return ""
    return key if has_usable_secret(key, min_length=16) else ""


def _served(self, token: str = "", only: str | None = None) -> dict[str, Path]:
    """``{profile name: home}`` the presented credential is authorized for.

    A profile is listed only when the caller's key is the key its own ``/p/<profile>/`` route
    accepts, so the roster never reveals a profile the same key could not already reach.
    Being served by the multiplexer is not authorization."""
    import hmac
    from hermes_cli.profiles import profiles_to_serve
    runner = getattr(self, "gateway_runner", None)
    multiplex = bool(getattr(getattr(runner, "config", None), "multiplex_profiles", False))
    served = {name: Path(home) for name, home in profiles_to_serve(multiplex=multiplex)}
    if only is not None:  # avatar: resolve just the requested profile, never its siblings
        served = {name: home for name, home in served.items() if name == only}
    from gateway.platforms.api_server import _api_request_profile
    scoped = _api_request_profile.get()
    if scoped and scoped != "default":
        # Already authenticated by _check_auth against this profile's own key.
        return {name: home for name, home in served.items() if name == scoped}
    if not token:
        return {}
    presented = token.encode()
    allowed: dict[str, Path] = {}
    for name, home in served.items():
        key = _profile_key(self, name, home)
        if key and hmac.compare_digest(presented, key.encode()):
            allowed[name] = home
    return allowed


def _ui_meta(home: Path) -> tuple[dict, dict]:
    """``(ui_meta, ui_meta_revisions)`` from ``profile.yaml``; empty when absent or unreadable."""
    import hermes_yaml as yaml
    path = home / "profile.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}, {}
    data = data if isinstance(data, dict) else {}
    ui_meta = data.get("ui_meta") if isinstance(data.get("ui_meta"), dict) else {}
    raw = data.get("_ui_meta_revisions")
    revisions = ({str(k): max(0, v) for k, v in raw.items() if type(v) is int}
                 if isinstance(raw, dict) else {})
    return ui_meta, revisions


def _open_avatar(home: Path) -> tuple[int, str] | None:
    """Open the profile's avatar as ``(fd, mime)``; the caller closes ``fd``.

    Traversal is descriptor-relative so nothing validated can be swapped afterwards:
    ``assets`` is opened with ``O_DIRECTORY|O_NOFOLLOW`` relative to the profile directory,
    and the avatar is opened relative to that descriptor with ``O_NOFOLLOW|O_NONBLOCK``
    (a FIFO opens without blocking), then accepted only if ``fstat`` says regular file."""
    import os
    import stat as _stat
    cloexec = getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | cloexec)
    except OSError:
        return None
    try:
        try:
            assets_fd = os.open("assets", os.O_RDONLY | os.O_DIRECTORY | nofollow | cloexec, dir_fd=home_fd)
        except OSError:
            return None
        try:
            for ext, mime in _AVATAR_TYPES:
                try:
                    fd = os.open(f"avatar.{ext}", os.O_RDONLY | nofollow | os.O_NONBLOCK | cloexec,
                                 dir_fd=assets_fd)
                except OSError:
                    continue
                try:
                    if _stat.S_ISREG(os.fstat(fd).st_mode):
                        result, fd = (fd, mime), -1
                        return result
                finally:
                    if fd != -1:
                        os.close(fd)
            return None
        finally:
            os.close(assets_fd)
    finally:
        os.close(home_fd)


def _has_avatar(home: Path) -> bool:
    import os
    opened = _open_avatar(home)
    if opened is None:
        return False
    os.close(opened[0])
    return True


def _read_avatar(home: Path) -> tuple[bytes, str] | None:
    """The avatar bytes and MIME type, reading at most the cap plus one byte."""
    import os
    opened = _open_avatar(home)
    if opened is None:
        return None
    fd, mime = opened
    try:
        chunks, remaining = [], _AVATAR_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return (data, mime) if len(data) <= _AVATAR_MAX_BYTES else None


def _json_safe(ui_meta: dict) -> dict | None:
    """``ui_meta`` normalized to exactly what will be sent (YAML dates etc. become strings), or
    None when it cannot be represented or exceeds the cap. One bad row never breaks the roster."""
    try:
        encoded = json.dumps(ui_meta, default=str)
    except (TypeError, ValueError, RecursionError):
        return None
    if len(encoded) > _UI_META_MAX_BYTES:
        return None
    return json.loads(encoded)


def _build_roster(self, token: str = "") -> list[dict[str, Any]]:
    from hermes_cli.profiles import list_profiles
    served = _served(self, token)
    if not served:
        return []
    rows: list[dict[str, Any]] = []
    for info in list_profiles(lazy_skill_count=True):
        home = served.get(info.name)
        if home is None:
            continue
        ui_meta, revisions = _ui_meta(home)
        row: dict[str, Any] = {
            "name": info.name,
            "is_default": bool(info.is_default),
            "model": info.model or "",
            "provider": info.provider or "",
            "description": info.description or "",
            "display_name": info.display_name or "",
            "skill_count": int(info.skill_count or 0),
            "has_avatar": _has_avatar(home),
            "ui_meta_revisions": revisions}
        safe_meta = _json_safe(ui_meta) if ui_meta else None
        if safe_meta:
            row["ui_meta"] = safe_meta
        rows.append(row)
        if len(rows) >= _ROSTER_MAX_PROFILES:
            break
    return rows


async def _handle_profiles(self, request: web.Request) -> web.Response:
    """GET /v1/profiles — the served profiles with their bot metadata."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    try:
        rows = await asyncio.to_thread(_build_roster, self, _presented_token(request))
    except Exception:
        logger.exception("GET /v1/profiles failed")
        return _json_error("Failed to list profiles.", 500, "profiles_failed")
    return web.json_response({"object": "list", "profiles": rows})


async def _handle_profile_avatar(self, request: web.Request) -> web.Response:
    """GET /v1/profiles/{name}/avatar — the profile's avatar image, if it has one."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    from hermes_cli.profiles import normalize_profile_name
    try:
        name = normalize_profile_name(request.match_info.get("name", ""))
    except ValueError:
        return _json_error("Unknown profile.", 404, "profile_not_found")
    home = (await asyncio.to_thread(_served, self, _presented_token(request), name)).get(name)
    if home is None:
        return _json_error("Unknown profile.", 404, "profile_not_found")

    try:
        result = await asyncio.to_thread(_read_avatar, home)
    except OSError:
        result = None
    if result is None:
        return _json_error("Profile has no avatar.", 404, "avatar_not_found")
    body, mime = result
    return web.Response(body=body, content_type=mime, headers={"Cache-Control": "private, max-age=60"})
