from types import SimpleNamespace

from agent.turn_finalizer import finalize_turn
from hermes_cli import kanban_db as kb


class FakeAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
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
        self._response_was_previewed = True
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None

    def _handle_max_iterations(self, messages, api_call_count):
        return "checkpoint summary"

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
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def test_final_response_closes_tool_tail_before_persistence(monkeypatch):
    """A recovered/previewed final response must be durable in session history.

    Regression for turns where the caller receives a non-empty final_response,
    but the message transcript still ends at a tool result. If persisted that
    way, the next turn reloads a stale/malformed history and can appear to loop
    because the assistant's visible final answer is missing from durable state.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "I'll check.",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert result["messages"][-1] == {"role": "assistant", "content": "Done."}
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == {"role": "assistant", "content": "Done."}



def _run_budget_exhaustion_for_kanban_task(monkeypatch, tmp_path, *, assignee, workspace_kind):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="long task",
            assignee=assignee,
            workspace_kind=workspace_kind,
            workspace_path=str(tmp_path / "worktree"),
        )
        claimed = kb.claim_task(conn, task_id, claimer="test-worker")
        assert claimed is not None
        assert claimed.current_run_id is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    agent = FakeAgent()
    agent.max_iterations = 3
    agent.iteration_budget = SimpleNamespace(remaining=0, used=3, max_total=3)
    agent.session_id = "sess-budget"

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "keep working"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="keep working",
        original_user_message="keep working",
        _should_review_memory=False,
        _turn_exit_reason="loop_exhausted",
    )
    return task_id, result


def test_worktree_budget_exhaustion_blocks_with_checkpoint(monkeypatch, tmp_path):
    task_id, result = _run_budget_exhaustion_for_kanban_task(
        monkeypatch, tmp_path, assignee="wren", workspace_kind="worktree"
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert task.status == "blocked"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert task.block_kind == "transient"
    assert runs[-1].outcome == "blocked"
    assert "checkpoint: iteration budget exhausted (3/3)" in (runs[-1].summary or "")
    assert comments
    assert "Iteration-budget checkpoint created automatically" in comments[-1].body
    assert "checkpoint summary" in comments[-1].body
    assert result["turn_exit_reason"] == "max_iterations_reached(3/3)"
    assert result["completed"] is False


def test_dante_budget_exhaustion_blocks_with_checkpoint(monkeypatch, tmp_path):
    task_id, _result = _run_budget_exhaustion_for_kanban_task(
        monkeypatch, tmp_path, assignee="dante", workspace_kind="scratch"
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)

    assert task.status == "blocked"
    assert task.consecutive_failures == 0
    assert comments
    assert "Session: sess-budget" in comments[-1].body


def test_non_code_budget_exhaustion_keeps_timed_out_failure_path(monkeypatch, tmp_path):
    task_id, _result = _run_budget_exhaustion_for_kanban_task(
        monkeypatch, tmp_path, assignee="wren", workspace_kind="scratch"
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert task.status == "ready"
    assert task.consecutive_failures == 1
    assert "Iteration budget exhausted (3/3)" in (task.last_failure_error or "")
    assert runs[-1].outcome == "timed_out"
    assert comments == []
