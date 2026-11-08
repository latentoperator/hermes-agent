"""Cron-aware external memory sync: a scheduled fire mirrors a provenance header + its response.

Before this, ``_sync_external_memory_for_turn`` sent the scheduler-assembled prompt (cron hint,
notepad, script output / ``## Run Context``, ``context_from`` previous output, standing job
instructions) to providers as the user's words, so every fire re-taught memory the job's own
instructions — and ``[SILENT]`` fires were retained too. Primary and subagent turns are unchanged.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider

SENTINEL = "ZEBRA-QUOKKA-7731 rule: always file reports under the teal folder"


def _assembled_cron_prompt() -> str:
    """A prompt shaped like ``cron.scheduler_prompt._build_job_prompt`` output, built from its parts."""
    from cron.scheduler_prompt import _CRON_HINT

    return (
        f"{_CRON_HINT}[CURRENT RUN TIME: Saturday, 2026-11-07 07:00 CST (UTC-06:00)]\n\n"
        "## Script Output\nThe following data was collected by a pre-run script.\n\nqueue=3\n\n"
        "## Your previous run's output\nYesterday: 2 open items.\n\n"
        f"Send Chris the morning pulse. {SENTINEL}.\n\n## Run Context\nmonitor: green\n"
    )


def _bare_agent(platform: str | None, *, meta: dict | None = None):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = MagicMock()
    agent.session_id = "cron_db3dc491566b_20261107_070000"
    if platform is not None:
        agent.platform = platform
    if meta is not None:
        agent._cron_job_meta = meta
    return agent


_META = {"id": "db3dc491566b", "name": "Dante morning pulse", "scheduled_at": "2026-11-07T07:00:00-06:00"}
_HEADER_OK = 'Scheduled job "Dante morning pulse" (db3dc491566b) ran at 2026-11-07T07:00:00-06:00; status: ok.'


def _sync(agent, response, user=None):
    agent._sync_external_memory_for_turn(
        original_user_message=_assembled_cron_prompt() if user is None else user,
        final_response=response, interrupted=False, messages=[{"role": "user", "content": "x"}])


@pytest.mark.parametrize("response", ["[SILENT]", "  [SILENT]\n", "[silent]", "[SILENT]\n[SILENT]"])
def test_cron_silent_response_retains_nothing(response, caplog):
    agent = _bare_agent("cron", meta=_META)
    with caplog.at_level(logging.INFO, logger="agent.cron_memory_sync"):
        _sync(agent, response)
    agent._memory_manager.sync_all.assert_not_called()
    agent._memory_manager.queue_prefetch_all.assert_not_called()
    assert "cron memory sync job=db3dc491566b mode=skipped_silent" in caplog.text


@pytest.mark.parametrize("response", ["", "   \n", None])
def test_cron_empty_response_retains_nothing(response, caplog):
    agent = _bare_agent("cron", meta=_META)
    with caplog.at_level(logging.INFO, logger="agent.cron_memory_sync"):
        _sync(agent, response)
    agent._memory_manager.sync_all.assert_not_called()
    agent._memory_manager.queue_prefetch_all.assert_not_called()
    assert "cron memory sync job=db3dc491566b mode=skipped_empty" in caplog.text


def test_cron_response_retains_header_and_response_only(caplog):
    from cron.scheduler_prompt import _CRON_HINT

    agent = _bare_agent("cron", meta=_META)
    response = "Morning pulse: 3 items in queue, deploy green."
    with caplog.at_level(logging.INFO, logger="agent.cron_memory_sync"):
        _sync(agent, response)

    agent._memory_manager.sync_all.assert_called_once()
    args, kwargs = agent._memory_manager.sync_all.call_args
    assert args == (_HEADER_OK, response)
    assert kwargs == {"session_id": agent.session_id}  # no transcript: its user row is the prompt
    retained = "\n".join(args)
    for leaked in (_CRON_HINT.strip()[:60], "Your previous run's output", "## Script Output", "## Run Context",
                   SENTINEL, "CURRENT RUN TIME"):
        assert leaked not in retained
    agent._memory_manager.queue_prefetch_all.assert_not_called()
    assert caplog.text.count("cron memory sync job=db3dc491566b mode=response") == 1


def test_cron_failure_marker_sets_failure_status():
    agent = _bare_agent("cron", meta=_META)
    _sync(agent, "[CRON_FAILURE]\nchild worker timed out")
    header = agent._memory_manager.sync_all.call_args.args[0]
    assert header.endswith("; status: failure.")
    assert header.startswith('Scheduled job "Dante morning pulse" (db3dc491566b) ran at ')


def test_cron_failure_marker_quoted_mid_report_stays_ok():
    """Same strict first-line rule as ``cron.scheduler._cron_failure_marker_error``."""
    agent = _bare_agent("cron", meta=_META)
    _sync(agent, "All fine. The [CRON_FAILURE] marker was not needed.")
    assert agent._memory_manager.sync_all.call_args.args[0] == _HEADER_OK


def test_cron_without_meta_still_never_sends_prompt():
    agent = _bare_agent("cron")
    _sync(agent, "report")
    header, response = agent._memory_manager.sync_all.call_args.args
    assert header.startswith('Scheduled job "unknown" (unknown) ran at ') and header.endswith("; status: ok.")
    assert response == "report"


def test_cron_interrupted_turn_syncs_nothing():
    agent = _bare_agent("cron", meta=_META)
    agent._sync_external_memory_for_turn(
        original_user_message=_assembled_cron_prompt(), final_response="partial", interrupted=True)
    agent._memory_manager.sync_all.assert_not_called()


def test_cron_sync_failure_never_raises():
    agent = _bare_agent("cron", meta=_META)
    agent._memory_manager.sync_all.side_effect = RuntimeError("backend down")
    _sync(agent, "report")  # best-effort, like the primary path


# --- Golden: primary and subagent paths are byte-identical to the pre-change behaviour ----------

_PRIMARY_USER = "What did we decide about the Hindsight retention policy?"
_PRIMARY_RESPONSE = "We chose a core cron-aware retain; primary turns are unchanged."
_MESSAGES = [{"role": "user", "content": _PRIMARY_USER}, {"role": "assistant", "content": _PRIMARY_RESPONSE}]


@pytest.mark.parametrize("platform", ["cli", "telegram", "desktop", "kanban", None])
def test_primary_sync_payload_is_unchanged(platform):
    agent = _bare_agent(platform)
    agent._sync_external_memory_for_turn(
        original_user_message=_PRIMARY_USER, final_response=_PRIMARY_RESPONSE, interrupted=False,
        messages=_MESSAGES)
    assert agent._memory_manager.sync_all.call_args_list == [
        ((_PRIMARY_USER, _PRIMARY_RESPONSE), {"session_id": agent.session_id, "messages": _MESSAGES})]
    assert agent._memory_manager.queue_prefetch_all.call_args_list == [
        ((_PRIMARY_USER,), {"session_id": agent.session_id})]


def test_primary_silent_text_is_still_retained():
    """``[SILENT]`` handling is cron-only; a chat turn that says it is ordinary content."""
    agent = _bare_agent("telegram")
    agent._sync_external_memory_for_turn(
        original_user_message="reply with [SILENT]", final_response="[SILENT]", interrupted=False)
    agent._memory_manager.sync_all.assert_called_once_with(
        "reply with [SILENT]", "[SILENT]", session_id=agent.session_id)


def test_subagent_sync_payload_is_unchanged():
    agent = _bare_agent("subagent")
    task = "Summarise the cron trace.\n## Run Context\nnot a cron prompt"
    agent._sync_external_memory_for_turn(
        original_user_message=task, final_response="summary", interrupted=False, messages=_MESSAGES)
    assert agent._memory_manager.sync_all.call_args_list == [
        ((task, "summary"), {"session_id": agent.session_id, "messages": _MESSAGES})]
    agent._memory_manager.queue_prefetch_all.assert_called_once_with(task, session_id=agent.session_id)


# --- Through a real MemoryManager to a provider ----------------------------------------------


class _RecordingProvider(MemoryProvider):
    def __init__(self):
        self.turns: list[tuple[str, str, dict]] = []
        self.prefetches: list[str] = []

    @property
    def name(self) -> str:
        return "recorder"

    def is_available(self) -> bool:
        return True

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def initialize(self, session_id: str = "", **kwargs) -> None:  # type: ignore[override]
        return None

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: list | None = None) -> None:  # type: ignore[override]
        self.turns.append((user_content, assistant_content, {"session_id": session_id, "messages": messages}))

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:  # type: ignore[override]
        self.prefetches.append(query)


def test_cron_turn_reaches_provider_as_header_plus_response():
    from run_agent import AIAgent

    provider = _RecordingProvider()
    mm = MemoryManager()
    mm._providers.append(provider)  # bypass add_provider config reads for the stub
    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager, agent.session_id, agent.platform = mm, "cron_x_1", "cron"
    agent._cron_job_meta = _META
    try:
        _sync(agent, "Pulse: deploy green.")
        _sync(agent, "[SILENT]")
        assert mm.flush_pending(timeout=10)
    finally:
        mm.shutdown_all()
    assert provider.turns == [(_HEADER_OK, "Pulse: deploy green.", {"session_id": "cron_x_1", "messages": None})]
    assert provider.prefetches == []
