"""Outbound Discord posting allowlist enforcement.

This is intentionally profile-scoped and config-backed: gateway/notifier code can
ask this module whether the active Hermes profile may post to a Discord channel
or thread before hitting Discord's API.  Denials are loud (logged and returned
as send failures) rather than silent drops.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_CONFIG_ENV = "HERMES_OUTBOUND_DISCORD_ALLOWLIST_CONFIG"


@dataclass(frozen=True)
class DiscordOutboundDecision:
    allowed: bool
    reason: str
    profile: str
    chat_id: str
    thread_id: str | None = None
    parent_channel_id: str | None = None


def _profile_name() -> str:
    explicit = (os.getenv("HERMES_PROFILE") or os.getenv("HERMES_ACTIVE_PROFILE") or "").strip()
    if explicit:
        return explicit

    home = Path(os.getenv("HERMES_HOME", "")).expanduser()
    parts = home.parts
    # Gateway services for named profiles are launched with only
    # HERMES_HOME=~/.hermes/profiles/<profile>.  Derive the acting profile
    # from that profile home instead of falling back to the default profile.
    if len(parts) >= 2 and parts[-2] == "profiles" and parts[-1]:
        return parts[-1]

    return "default"


def _shared_hermes_home() -> Path:
    override = os.getenv(_CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve().parent

    home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    parts = home.parts
    # Profile gateways run with HERMES_HOME=~/.hermes/profiles/<profile>.  The
    # allowlist is fleet-wide shared state, so walk back to ~/.hermes.
    if len(parts) >= 2 and parts[-2] == "profiles":
        return home.parent.parent
    return home


def _config_path() -> Path:
    override = os.getenv(_CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _shared_hermes_home() / "config.yaml"


def _load_allowlist_config() -> Mapping[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Discord outbound allowlist: failed to load %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    gateway = data.get("gateway")
    if not isinstance(gateway, dict):
        return {}
    cfg = gateway.get("outbound_discord_allowlist")
    return cfg if isinstance(cfg, dict) else {}


def _normalize_id(value: Any) -> str:
    return str(value or "").strip()


def _id_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, (str, int)):
        return {_normalize_id(values)} if _normalize_id(values) else set()
    if isinstance(values, (list, tuple, set)):
        return {_normalize_id(v) for v in values if _normalize_id(v)}
    return set()


def _profile_block(cfg: Mapping[str, Any], profile: str) -> Mapping[str, Any]:
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    block = profiles.get(profile)
    return block if isinstance(block, dict) else {}


def _shared_thread_allowed(cfg: Mapping[str, Any], profile: str, thread_id: str | None) -> bool:
    if not thread_id:
        return False
    shared = cfg.get("shared_threads")
    if not isinstance(shared, dict):
        return False
    for block in shared.values():
        if not isinstance(block, dict):
            continue
        if _normalize_id(block.get("thread_id")) != thread_id:
            continue
        allowed_profiles = _id_set(block.get("allowed_profiles"))
        if not allowed_profiles or profile in allowed_profiles or "*" in allowed_profiles:
            return True
    return False


def check_discord_outbound_allowed(
    chat_id: str,
    *,
    thread_id: str | None = None,
    parent_channel_id: str | None = None,
    profile: str | None = None,
) -> DiscordOutboundDecision:
    """Return whether the active profile may post to this Discord target.

    Config schema (in shared ~/.hermes/config.yaml):

        gateway:
          outbound_discord_allowlist:
            enabled: true
            disabled_profiles: [rocket]
            profiles:
              wren:
                channels: ["150..."]
                threads: ["150..."]
                thread_parent_channels: ["150..."]
            shared_threads:
              decisions:
                chat_id: "150..."
                thread_id: "152..."
                allowed_profiles: [dante]
    """
    active_profile = (profile or _profile_name()).strip() or "default"
    target_chat_id = _normalize_id(chat_id)
    target_thread_id = _normalize_id(thread_id) or None
    target_parent_channel_id = _normalize_id(parent_channel_id) or None

    cfg = _load_allowlist_config()
    if not cfg or not bool(cfg.get("enabled", False)):
        return DiscordOutboundDecision(True, "allowlist disabled", active_profile, target_chat_id, target_thread_id)

    disabled_profiles = _id_set(cfg.get("disabled_profiles"))
    if active_profile in disabled_profiles:
        return DiscordOutboundDecision(True, "profile exempt from allowlist", active_profile, target_chat_id, target_thread_id)

    block = _profile_block(cfg, active_profile)
    channels = _id_set(block.get("channels"))
    threads = _id_set(block.get("threads"))
    thread_parent_channels = _id_set(block.get("thread_parent_channels"))

    if target_thread_id:
        if (
            "*" in threads
            or target_thread_id in threads
            or _shared_thread_allowed(cfg, active_profile, target_thread_id)
        ):
            return DiscordOutboundDecision(True, "thread allowlisted", active_profile, target_chat_id, target_thread_id, target_parent_channel_id)
        if "*" in thread_parent_channels or (
            target_parent_channel_id and target_parent_channel_id in thread_parent_channels
        ):
            return DiscordOutboundDecision(True, "thread parent channel allowlisted", active_profile, target_chat_id, target_thread_id, target_parent_channel_id)
        return DiscordOutboundDecision(False, "thread not allowlisted", active_profile, target_chat_id, target_thread_id, target_parent_channel_id)

    if "*" in channels or target_chat_id in channels:
        return DiscordOutboundDecision(True, "channel allowlisted", active_profile, target_chat_id, None)

    # Discord threads are channels at the API level.  Some send paths address a
    # thread directly as chat_id without separate metadata.thread_id, so honor
    # thread allowlist entries for the direct target ID as well.
    if "*" in threads or target_chat_id in threads or _shared_thread_allowed(cfg, active_profile, target_chat_id):
        return DiscordOutboundDecision(True, "thread allowlisted", active_profile, target_chat_id, None)

    if "*" in thread_parent_channels or (
        target_parent_channel_id and target_parent_channel_id in thread_parent_channels
    ):
        return DiscordOutboundDecision(True, "thread parent channel allowlisted", active_profile, target_chat_id, None, target_parent_channel_id)

    return DiscordOutboundDecision(False, "channel not allowlisted", active_profile, target_chat_id, None)


def deny_message(decision: DiscordOutboundDecision) -> str:
    target = decision.chat_id
    if decision.thread_id:
        target = f"{decision.chat_id}:{decision.thread_id}"
    return (
        "Outbound Discord post denied by profile allowlist: "
        f"profile={decision.profile!r}, target={target!r}, reason={decision.reason}"
    )


def log_denial(decision: DiscordOutboundDecision) -> None:
    logger.error(deny_message(decision))
