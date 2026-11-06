"""The shared owner renders images for its authenticated interactive TUI viewer."""
import base64
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gateway.config import Platform
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.fixture
def image_owner(tmp_path, monkeypatch):
    ambient, home = tmp_path / 'ambient', tmp_path / 'owner'
    for directory, mode in ((ambient, 'off'), (home, 'unicode')):
        directory.mkdir()
        (directory / 'config.yaml').write_text(json.dumps({'display': {'inline_images': mode}}))
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('s', source='test')
        authority = SessionAuthority(SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=True)), profile_id=str(home),
            instance_id='current', db=db, epoch=begin_runtime_epoch(db, instance_id='current'))
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
        live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=Platform.LOCAL,
            user_id=connection.actor.subject), 'route')
        connection.subscriptions['s'] = 'viewer'
        live.subscribers['viewer'] = connection.actor
        yield connection, home, db


async def preview(connection, path, **extra):
    return await connection.dispatch({'id': 1, 'method': 'image.render', 'params': {
        'session_id': 's', 'path': str(path), 'cols': 10000, 'rows': 10000,
        'graphics': True, 'terminal': 'kitty', **extra}})


@pytest.mark.asyncio
async def test_uploaded_image_renders_with_owner_config_and_bounded_terminal_modes(image_owner):
    connection, home, db = image_owner
    data = BytesIO()
    Image.new('RGB', (320, 160), (20, 180, 70)).save(data, format='PNG')
    uploaded = await connection.dispatch({'id': 0, 'method': 'image.attach_bytes', 'params': {
        'session_id': 's', 'filename': 'shot.png', 'content_base64': base64.b64encode(data.getvalue()).decode()}})
    path = uploaded['result']['path']
    for mode in ('unicode', 'kitty', 'off'):
        (home / 'config.yaml').write_text(json.dumps({'display': {'inline_images': mode}}))
        response = await preview(connection, path)
        assert 'result' in response, response
        result = response['result']
        assert result['mode'] == mode
        if mode != 'off':
            assert 0 < result['cols'] <= 64 and 0 < result['rows'] <= 16
            assert result['cells'] if mode == 'unicode' else result['placeholder'] and result['transmit']
    assert not db.get_messages('s')
    assert connection.authority.sessions['s'].task is None
    with db._read_ctx() as conn:
        assert conn.execute('SELECT count(*) FROM session_admissions').fetchone()[0] == 0
    await connection.close()


def test_authenticated_daemon_renders_uploaded_image_over_public_websocket(tmp_path):
    import asyncio
    from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket

    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False}, 'display': {'inline_images': 'auto'},
        'model': {'provider': 'custom', 'default': 'fixture', 'base_url': 'http://127.0.0.1:1/v1'},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []}}))
    data = BytesIO()
    Image.new('RGB', (64, 32), (20, 180, 70)).save(data, format='PNG')
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', PYTHONUNBUFFERED='1')

    async def exercise(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='image-preview', source='tui', cwd=str(user), toolsets=[])
            assert 'result' in created, created
            sid = created['result']['session_id']
            uploaded = await rpc(ws, 'image.attach_bytes', session_id=sid, filename='client.png',
                                 content_base64=base64.b64encode(data.getvalue()).decode())
            path = uploaded['result']['path']
            params = dict(session_id=sid, path=path, cols=20, rows=8, graphics=True, terminal='unicode')
            result = await rpc(ws, 'image.render', **params)
            assert 'result' in result, result
            assert result['result']['mode'] == 'unicode'
            assert result['result']['cells'][0][0] == [20, 180, 70, 255, 20, 180, 70, 255]
            kitty = await rpc(ws, 'image.render', **{**params, 'terminal': 'kitty'})
            assert kitty['result']['mode'] == 'kitty' and kitty['result']['transmit'].startswith('\x1b_G')
            async with websocket(home, desc) as detached:
                denied = await rpc(detached, 'image.render', **params)
                assert denied.get('error', {}).get('message') == 'permission_denied', denied

    with daemon(root, home, env, barrier=False) as (_, desc):
        asyncio.run(exercise(desc))


@pytest.mark.asyncio
async def test_preview_refuses_unattached_foreign_and_read_only_callers(image_owner):
    connection, home, _ = image_owner
    path = home / 'photo.png'
    Image.new('RGB', (16, 8), 'red').save(path)
    for identity, subscribed, reason in (
        ({'user_id': 'owner'}, False, 'permission_denied'),
        ({'user_id': 'other'}, True, 'permission_denied'),
        ({'user_id': 'owner', 'capabilities': ['session:read']}, True, 'permission_denied'),
        ({'user_id': 'owner', 'profile_id': 'foreign'}, True, 'profile_mismatch'),
    ):
        peer = AuthorityConnection(connection.authority, object(), identity)
        if subscribed:
            peer.subscriptions['s'] = 'untrusted-viewer'
        response = await preview(peer, path)
        assert response.get('error', {}).get('message') == reason, response
    for changes in ({'session_id': ''}, {'session_id': ['s']}, {'cols': 'bad'}, {'profile_home': str(home)}):
        response = await preview(connection, path, **changes)
        assert response.get('error', {}).get('message') == 'invalid_params', response
    assert (await preview(connection, home / 'missing.png'))['result'] == {'mode': 'off'}
    await connection.close()
