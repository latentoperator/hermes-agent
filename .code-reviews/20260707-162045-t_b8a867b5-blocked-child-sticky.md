# Code Review: t_b8a867b5 — Blocked child approval-card promotion fix

- **Review task:** t_935af98d (code-reviewer)
- **Implementation task:** t_b8a867b5 (wren)
- **Repo:** /home/hopewell/.hermes/hermes-agent
- **Branch:** hopebox/prod-upstream-main-sync-20260609
- **Base SHA:** 77a11a236f8dac46a4e100dd13d7d8a28f38a624 (HEAD; changes are uncommitted in the working tree)
- **Reviewer model:** glm-5.2 (zai) — code-reviewer profile
- **Session:** 20260707_161703_1b5e27

## Verdict: APPROVE (the blocked-child fix)

The targeted fix — appending a sticky `blocked` event in `create_task` when `initial_status='blocked'` — is correct, minimal, and reuses the existing sticky-block semantics rather than introducing new status/promotion rules. Independently re-verified against the repo and tests.

## Root cause (verified)

`create_task(..., initial_status='blocked')` set `tasks.status='blocked'` but wrote no `task_events.kind='blocked'` row. `recompute_ready()` skips a blocked task only when `_has_sticky_block()` is true, and that predicate is true iff the most recent `blocked`/`unblocked` event for the task is `blocked` (`kanban_db.py:3673-3679`). With no event row, the predicate returned False, so once all parents reached `done`/`archived`, `recompute_ready` auto-promoted the task to `ready` (`kanban_db.py:3736-3758`) — exactly the bug that let an approval card run before Chris approved it. This matches Wren's root-cause writeup.

## The fix (verified correct)

`create_task` now appends a `blocked` event immediately after the `created` event when `initial_status == 'blocked'` (`kanban_db.py:3029-3043`):

```python
if initial_status == "blocked":
    _append_event(conn, task_id, "blocked",
        {"reason": "initial_status=blocked", "kind": None, "source": "initial_status"})
```

This makes `_has_sticky_block` return True from creation, so `recompute_ready` skips the task until an explicit `unblock_task` writes the later `unblocked` event that flips the predicate back (`kanban_db.py:5304-5307`, verified). The fix is minimal (15 lines, one new branch) and adds no new status value, no new promotion rule, and no schema change.

## Behavior-preservation checks (verified against source)

- **Dependency promotion (`todo` children):** unaffected. The patch only fires for `initial_status == 'blocked'`; `todo` children still take the `cur_status == 'blocked'` → False branch and promote normally. Confirmed by `test_create_task_with_parent_is_todo_until_parent_done` and `test_recompute_ready_fan_in_waits_for_all_parents` (both pass in the 280-test sweep).
- **Circuit-breaker auto-recovery:** unaffected. The breaker emits `gave_up` (not `blocked`), so `_has_sticky_block` still returns False for it and the failure-limit guard at `kanban_db.py:3746-3753` still governs. The new event does not interact with `consecutive_failures` or `max_retries`.
- **`unblock_task`:** still the single release edge. It writes `unblocked`, which becomes the most recent event, flipping `_has_sticky_block` back to False. An intentionally blocked approval card therefore resumes only after an explicit operator action — the intended semantics.
- **Auto-review-gate children:** unaffected. The recursive `create_task` call for the review-gate child (`kanban_db.py:3056-3083`) does not pass `initial_status`, so it defaults to `"running"` and the new branch is not entered. No risk of the review-gate child itself being sticky-blocked.
- **`block_task` worker path:** unaffected. `block_task` writes its own `blocked` event via a different code path; the new creation-time event does not collide with it.
- **Event semantics:** the new event payload `{"reason": ..., "kind": None, "source": "initial_status"}` is consistent with the existing event vocabulary. `kind: None` matches the "un-typed block" convention used elsewhere (e.g. legacy `block_task` with no kind).

## Test verification (independently re-run)

Targeted suite (worker claimed 7 passed — I reproduced exactly):
```
./venv/bin/python -m pytest tests/hermes_cli/test_kanban_blocked_sticky.py -q -o 'addopts='
→ 7 passed in 1.05s
```

Broader kanban sweep (worker claimed 11 passed across 3 selections — I ran the full files for a stronger guarantee):
```
./venv/bin/python -m pytest tests/hermes_cli/test_kanban_blocked_sticky.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_cli.py -q -o 'addopts='
→ 280 passed in 30.14s
```

**Test-bites verification (the important one):** reverted `hermes_cli/kanban_db.py` to base via `git stash`, ran the new regression test against the unpatched source:
```
tests/hermes_cli/test_kanban_blocked_sticky.py::test_initial_status_blocked_child_is_not_auto_promoted_after_parent_completion
→ FAILED: AssertionError: assert 'ready' == 'blocked'
```
The child became `ready` after parent completion on the base source — proving the test genuinely exercises the fix and is not vacuous. Restored the patch; test passes again.

## Warnings (non-blocking, process/hygiene)

### W1 — Working tree contains two unrelated, undisclosed change sets

Wren's handoff discloses only the blocked-child fix (`hermes_cli/kanban_db.py` + `tests/hermes_cli/test_kanban_blocked_sticky.py`). The actual working tree has **four** modified files:

```
 M cron/lifecycle_guard.py                         (8 lines, undisclosed)
 M hermes_cli/kanban_db.py                         (15 lines, disclosed)
 M tests/hermes_cli/test_gateway_restart_loop.py   (24 lines, undisclosed)
 M tests/hermes_cli/test_kanban_blocked_sticky.py  (33 lines, disclosed)
```

The two undisclosed files (`cron/lifecycle_guard.py` + its test) are a separate, coherent change: they add `\b` word boundaries to the gateway-lifecycle kill-pattern regex so benign prose words like "skill"/"SKILL.md" don't trip the `p?kill` branch before later "Hermes"/"gateway" mentions. This is clearly related to the weekly-skill-drift cron work (Wren's t_621756ef), not to t_b8a867b5.

This is not a defect in the blocked-child fix, but it is a handoff-hygiene issue: when this task is committed/pushed, the two unrelated changes will ride along unless explicitly separated. **Recommendation:** stage/commit the blocked-child fix as its own isolated commit (the two `kanban` files only), and let the lifecycle-guard change be committed under its own task. Whoever commits should use scoped `git add <file>`, not `git add -A`.

### W2 — Patch is uncommitted in the live Hermes checkout

As with all code changes in `/home/hopewell/.hermes/hermes-agent`, this fix is sitting as an uncommitted working-tree modification on a live branch. This is flagged per fleet norm — it needs a commit (scoped per W1) and is not "done" until committed and treated as merged. The task was correctly blocked as `review-required` for this reason.

## Suggestions (non-blocking)

- **S1 — Consider asserting the sticky event exists in the regression test.** The current test asserts the externally observable behavior (status stays `blocked` after parent completion). That's the right primary assertion. Optionally also asserting that a `blocked` event row exists in `task_events` for the child would pin the mechanism, not just the symptom — making the test less likely to pass for the wrong reason if the promotion logic is ever refactored. Low value; current test is acceptable.
- **S2 — Docstring on `create_task` could note the sticky behavior.** The `initial_status` parameter docstring (`kanban_db.py:2709` area, and the class docstring at 2715-2746) does not mention that `blocked` is now sticky until explicit unblock. A one-line note would help future callers understand the semantics difference between `initial_status='blocked'` (sticky) and `initial_status='running'` (not).

## Looks good

- Root cause correctly identified and the fix targets it precisely.
- Minimal, surgical change reusing existing sticky-block semantics — no new status, rule, or schema.
- The regression test genuinely bites (verified by reverting the source).
- All 280 kanban tests pass; no regressions in dependency promotion, circuit-breaker, or review-gate paths.
- Correct operator guidance in the durable note: `initial_status='blocked'` for approval gates, `kanban_unblock` as the explicit release edge.

## Evidence checked

- `hermes_cli/kanban_db.py`: `create_task` (2686-3090), patch site (3029-3043), `_has_sticky_block` (3644-3679), `recompute_ready` (3682-3766), `block_task` (4951-5065), `unblock_task` (5245-5308), review-gate child creation (3044-3083), `VALID_INITIAL_STATUSES` (103).
- `tests/hermes_cli/test_kanban_blocked_sticky.py`: full new test (51-83).
- `git status --porcelain`, `git diff --stat`, `git rev-parse HEAD` on `hopebox/prod-upstream-main-sync-20260609`.
- Test re-runs: 7 passed (targeted), 280 passed (kanban sweep).
- Test-bites check: base-source revert → test fails with `assert 'ready' == 'blocked'`; patch restored → test passes.
- Durable note: `/home/hopewell/obsidian/Hopewell MSP/101-Hermes/Kanban/2026-07-07 blocked child approval-card promotion fix.md`.
- Diff artifact: `/home/hopewell/obsidian/Hopewell MSP/101-Hermes/Kanban/artifacts/t_b8a867b5-blocked-child-promotion.diff`.
