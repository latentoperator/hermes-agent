import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.MessageType = SimpleNamespace(reply="reply")
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *a, **k: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1,
        primary=2,
        secondary=2,
        danger=3,
        green=1,
        grey=2,
        blurple=2,
        red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1,
        green=lambda: 2,
        blue=lambda: 3,
        red=lambda: 4,
        purple=lambda: 5,
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_adapter  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _write_allowlist(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
gateway:
  outbound_discord_allowlist:
    enabled: true
    profiles:
      zelda:
        channels:
          - 'allowed-parent'
        thread_parent_channels:
          - 'allowed-parent'
        threads:
          - 'allowed-thread'
""",
        encoding="utf-8",
    )
    return cfg


class FakeThread:
    id = 123
    parent_id = None
    name = "private-thread"
    guild = SimpleNamespace(name="Hermes @ Hopebox")
    parent = None

    async def history(self, *args, **kwargs):  # pragma: no cover - not reached in deny test
        return []


@pytest.mark.asyncio
async def test_inbound_discord_message_is_dropped_before_agent_when_outbound_denied(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_PROFILE", "zelda")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setattr(discord_adapter.discord, "Thread", FakeThread)

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._text_batch_delay_seconds = 0
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter.handle_message = AsyncMock()

    message = SimpleNamespace(
        content="<@999> inspect this private thread",
        channel=FakeThread(),
        author=SimpleNamespace(
            id=42,
            bot=False,
            display_name="Chris",
            name="latentoperator",
        ),
        mentions=[SimpleNamespace(bot=True, id=999)],
        attachments=[],
        message_snapshots=[],
        reference=None,
        created_at=None,
        id=555,
        guild=SimpleNamespace(id=777),
    )

    await adapter._handle_message(message)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_discord_message_runs_when_parent_thread_allowed(tmp_path, monkeypatch):
    cfg = _write_allowlist(tmp_path)
    monkeypatch.setenv("HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG", str(cfg))
    monkeypatch.setenv("HERMES_PROFILE", "zelda")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_HISTORY_BACKFILL", "false")

    class AllowedThread(FakeThread):
        id = 456
        parent_id = "allowed-parent"
        name = "allowed-thread-under-parent"
        guild = SimpleNamespace(name="Hermes @ Hopebox")
        parent = SimpleNamespace(id="allowed-parent")

    monkeypatch.setattr(discord_adapter.discord, "Thread", FakeThread)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._text_batch_delay_seconds = 0
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter.handle_message = AsyncMock()

    message = SimpleNamespace(
        content="<@999> inspect this allowed thread",
        channel=AllowedThread(),
        author=SimpleNamespace(
            id=42,
            bot=False,
            display_name="Chris",
            name="latentoperator",
        ),
        mentions=[SimpleNamespace(bot=True, id=999)],
        attachments=[],
        message_snapshots=[],
        reference=None,
        created_at=None,
        id=556,
        guild=SimpleNamespace(id=777),
    )

    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
