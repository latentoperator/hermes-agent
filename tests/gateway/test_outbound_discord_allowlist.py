import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.outbound_discord_allowlist import check_discord_outbound_allowed


def _write_allowlist(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            gateway:
              outbound_discord_allowlist:
                enabled: true
                disabled_profiles:
                  - rocket
                profiles:
                  wren:
                    channels: ['111']
                    threads: ['222']
                    thread_parent_channels: ['777']
                  wildcard:
                    channels: ['*']
                    threads: ['*']
                    thread_parent_channels: ['*']
                shared_threads:
                  decisions:
                    chat_id: '999'
                    thread_id: '444'
                    allowed_profiles: ['dante']
            """
        ),
        encoding="utf-8",
    )
    return cfg


def test_check_allows_profile_channel_and_thread(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_PROFILE", "wren")

    assert check_discord_outbound_allowed("111").allowed is True
    assert check_discord_outbound_allowed("111", thread_id="222").allowed is True


def test_check_denies_unlisted_thread_even_when_parent_channel_allowed(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_PROFILE", "wren")

    decision = check_discord_outbound_allowed("111", thread_id="555")

    assert decision.allowed is False
    assert decision.reason == "thread not allowlisted"


def test_check_allows_thread_under_allowlisted_parent_channel(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_PROFILE", "wren")

    decision = check_discord_outbound_allowed("555", thread_id="555", parent_channel_id="777")

    assert decision.allowed is True
    assert decision.reason == "thread parent channel allowlisted"


def test_check_derives_profile_from_gateway_hermes_home(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    profile_home = tmp_path / "profiles" / "wren"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_ACTIVE_PROFILE", raising=False)

    decision = check_discord_outbound_allowed("111")

    assert decision.allowed is True
    assert decision.profile == "wren"


def test_check_allows_direct_thread_id_chat_target(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))

    decision = check_discord_outbound_allowed("222", profile="wren")

    assert decision.allowed is True
    assert decision.reason == "thread allowlisted"


def test_shared_thread_is_limited_to_configured_profiles(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))

    assert check_discord_outbound_allowed("999", thread_id="444", profile="dante").allowed is True
    assert check_discord_outbound_allowed("999", thread_id="444", profile="wren").allowed is False


def test_wildcard_profile_allows_any_channel_or_thread(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))

    assert check_discord_outbound_allowed("random-channel", profile="wildcard").allowed is True
    assert check_discord_outbound_allowed("random-channel", thread_id="random-thread", profile="wildcard").allowed is True
    assert check_discord_outbound_allowed("direct-thread", profile="wildcard").allowed is True


def test_thread_parent_wildcard_allows_thread_without_known_parent(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))

    decision = check_discord_outbound_allowed(
        "private-thread",
        thread_id="private-thread",
        parent_channel_id=None,
        profile="wildcard",
    )

    assert decision.allowed is True
    assert decision.reason == "thread allowlisted"


def test_disabled_profile_is_exempt(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))

    decision = check_discord_outbound_allowed("anything", thread_id="anywhere", profile="rocket")

    assert decision.allowed is True
    assert decision.reason == "profile exempt from allowlist"


@pytest.mark.asyncio
async def test_discord_adapter_returns_failed_send_on_allowlist_denial(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_PROFILE", "wren")

    # Import after env is set; the adapter module can be imported with a mocked
    # discord package in this test environment.
    import sys
    from types import ModuleType

    if "discord" not in sys.modules:
        discord_mod = MagicMock()
        discord_mod.Intents.default.return_value = MagicMock()
        discord_mod.DMChannel = type("DMChannel", (), {})
        discord_mod.Thread = type("Thread", (), {})
        discord_mod.ForumChannel = type("ForumChannel", (), {})
        discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
        discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
        discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
        discord_mod.Interaction = object
        discord_mod.Embed = MagicMock
        discord_mod.app_commands = SimpleNamespace(describe=lambda **kwargs: (lambda fn: fn), choices=lambda **kwargs: (lambda fn: fn), Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        commands_mod = MagicMock()
        commands_mod.Bot = MagicMock
        ext_mod = ModuleType("discord.ext")
        setattr(ext_mod, "commands", commands_mod)
        sys.modules["discord"] = discord_mod
        sys.modules["discord.ext"] = ext_mod
        sys.modules["discord.ext.commands"] = commands_mod

    from plugins.platforms.discord.adapter import DiscordAdapter

    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1234)))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(get_channel=lambda _id: channel, fetch_channel=AsyncMock())

    result = await adapter.send("111", "blocked", metadata={"thread_id": "555"})

    assert result.success is False
    assert result.error_kind == "forbidden"
    assert "Outbound Discord post denied" in (result.error or "")
    assert channel.send.await_count == 0


@pytest.mark.asyncio
async def test_discord_adapter_allows_thread_under_allowlisted_parent(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_PROFILE", "wren")

    import sys
    from types import ModuleType

    if "discord" not in sys.modules:
        discord_mod = MagicMock()
        discord_mod.Intents.default.return_value = MagicMock()
        discord_mod.DMChannel = type("DMChannel", (), {})
        discord_mod.Thread = type("Thread", (), {})
        discord_mod.ForumChannel = type("ForumChannel", (), {})
        discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
        discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
        discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
        discord_mod.Interaction = object
        discord_mod.Embed = MagicMock
        discord_mod.app_commands = SimpleNamespace(describe=lambda **kwargs: (lambda fn: fn), choices=lambda **kwargs: (lambda fn: fn), Choice=lambda **kwargs: SimpleNamespace(**kwargs))
        commands_mod = MagicMock()
        commands_mod.Bot = MagicMock
        ext_mod = ModuleType("discord.ext")
        setattr(ext_mod, "commands", commands_mod)
        sys.modules["discord"] = discord_mod
        sys.modules["discord.ext"] = ext_mod
        sys.modules["discord.ext.commands"] = commands_mod

    from plugins.platforms.discord.adapter import DiscordAdapter

    thread = SimpleNamespace(parent_id="777", send=AsyncMock(return_value=SimpleNamespace(id=1234)))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(get_channel=lambda _id: thread, fetch_channel=AsyncMock())

    result = await adapter.send("555", "allowed", metadata={"thread_id": "555"})

    assert result.success is True
    assert thread.send.await_count == 1
