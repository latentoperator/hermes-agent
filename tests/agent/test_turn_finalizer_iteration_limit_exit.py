"""Regression tests for iteration-limit exit normalization (#61631)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_finalizer import finalize_turn
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


class _LimitAgent:
    def __init__(
        self,
        *,
        max_iterations=60,
        budget_remaining=0,
        completion_explainer=False,
    ):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=budget_remaining, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        self._handle_max_iterations_called = False
        self._completion_explainer = completion_explainer

    def _handle_max_iterations(self, messages, api_call_count):
        self._handle_max_iterations_called = True
        return "summary from extra call"

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return self._completion_explainer

    def _format_turn_completion_explanation(self, _reason):
        return "iteration-limit explanation"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def _finalize(
    agent,
    *,
    final_response,
    exit_reason,
    api_call_count=60,
    pending_verification_response=None,
):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
        _pending_verification_response=pending_verification_response,
    )
















@pytest.mark.parametrize(
    ("exit_reason", "interrupted", "failed"),
    [
        ("interrupted_by_user", True, False),
        ("all_retries_exhausted_no_response", False, False),
        ("provider_failure", False, True),
    ],
)
def test_pending_response_does_not_mask_later_terminal_exit(
    monkeypatch, exit_reason, interrupted, failed
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=interrupted,
        failed=failed,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
        _pending_verification_response="stale premature report",
    )

    assert result["final_response"] is None
    assert result["turn_exit_reason"] == exit_reason
    assert result["completed"] is False
    assert agent._handle_max_iterations_called is False


def test_pending_response_stops_kanban_task_with_durable_handoff(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    block = MagicMock(name="block_task", return_value=True)
    comment = MagicMock(name="add_comment")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db_connect.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db.block_task", block)
    monkeypatch.setattr("hermes_cli.kanban_db.add_comment", comment)
    agent = _LimitAgent()

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="composed report",
    )

    assert result["turn_exit_reason"] == "max_iterations_reached(60/60)"
    block.assert_called_once_with(
        conn,
        "task-123",
        reason=(
            "Iteration budget exhausted (60/60). Review the saved handoff and "
            "decompose or narrow the remaining scope before re-dispatch."
        ),
        kind="needs_input",
        expected_run_id=42,
        run_summary="composed report",
        run_metadata={
            "worker_session_id": "sess-test",
            "budget_used": 60,
            "budget_max": 60,
            "stop_reason": "iteration_budget_exhausted",
            "resume_action": "review_and_decompose",
        },
    )
    comment.assert_called_once()


def test_first_kanban_budget_exhaustion_closes_claim_and_preserves_handoff(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="oversized", assignee="wren")
        claimed = kb.claim_task(conn, task_id, claimer="test-worker")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    agent = _LimitAgent(max_iterations=3)
    agent.session_id = "sess-budget-handoff"

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        api_call_count=3,
    )

    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        comments = kb.list_comments(conn, task_id)

    assert task is not None and task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.current_run_id is None
    assert run is not None and run.outcome == "blocked"
    assert run.summary == "summary from extra call"
    assert run.metadata == {
        "worker_session_id": "sess-budget-handoff",
        "budget_used": 3,
        "budget_max": 3,
        "stop_reason": "iteration_budget_exhausted",
        "resume_action": "review_and_decompose",
    }
    assert comments and "summary from extra call" in comments[-1].body
    assert result["completed"] is False


def test_published_pending_candidate_is_not_duplicated_by_finalizer(monkeypatch):
    """When budget exhaustion preserves a verification candidate that is
    already the tail assistant message, the finalizer must NOT append a
    duplicate. The content-comparison guard prevents this. (#65919 §7)
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()
    report = "the composed report"

    result = finalize_turn(
        agent,
        final_response=report,
        api_call_count=60,
        interrupted=False,
        failed=False,
        # The candidate is already in messages as the tail assistant.
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": report},
        ],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="unknown",
        _pending_verification_response=report,
    )

    # The tail assistant already matches final_response — no duplicate appended.
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant"]
    # Persisted messages should also have no duplicate.
    assert agent.persisted_messages is not None
    persisted_roles = [m["role"] for m in agent.persisted_messages]
    assert persisted_roles == ["user", "assistant"]

def test_bounded_fallback_stops_kanban_task_when_interrupted(monkeypatch):
    """When budget is exhausted and the turn was interrupted,
    ``finalize_turn`` must still close the run with a durable handoff.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-456")
    block = MagicMock(name="block_task", return_value=True)
    comment = MagicMock(name="add_comment")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db_connect.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db.block_task", block)
    monkeypatch.setattr("hermes_cli.kanban_db.add_comment", comment)
    agent = _LimitAgent()

    # Budget exhausted (60/60), interrupted, no fallback-eligible exit_reason
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
    )

    block.assert_called_once()
    args, kwargs = block.call_args
    assert args[1] == "task-456"
    assert kwargs["kind"] == "needs_input"
    assert kwargs["run_metadata"]["budget_used"] == 60
    assert kwargs["run_metadata"]["budget_max"] == 60
    comment.assert_called_once()


def test_bounded_fallback_stops_kanban_task_when_failed(monkeypatch):
    """When budget is exhausted and the turn failed,
    the bounded fallback must still preserve a terminal handoff.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-789")
    block = MagicMock(name="block_task", return_value=True)
    comment = MagicMock(name="add_comment")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db_connect.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db.block_task", block)
    monkeypatch.setattr("hermes_cli.kanban_db.add_comment", comment)
    agent = _LimitAgent()

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=False,
        failed=True,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="provider_failure",
    )

    block.assert_called_once()
    args, kwargs = block.call_args
    assert args[1] == "task-789"
    assert kwargs["kind"] == "needs_input"
    comment.assert_called_once()


def test_bounded_fallback_does_not_fire_without_kanban_task(monkeypatch):
    """When budget is exhausted and interrupted but no kanban task is
    active, the bounded fallback must NOT fire (#87096).
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    block = MagicMock(name="block_task", return_value=True)
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db_connect.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db.block_task", block)
    agent = _LimitAgent()

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
    )

    block.assert_not_called()


def test_bounded_fallback_does_not_fire_when_budget_not_exhausted(monkeypatch):
    """When budget is NOT exhausted but turn is interrupted and a kanban
    task is active, the bounded fallback must NOT fire (#87096).
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-999")
    block = MagicMock(name="block_task", return_value=True)
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db_connect.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db.block_task", block)
    agent = _LimitAgent(budget_remaining=60)

    # api_call_count=10, max_iterations=60 — budget NOT exhausted
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=10,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
    )

    block.assert_not_called()
