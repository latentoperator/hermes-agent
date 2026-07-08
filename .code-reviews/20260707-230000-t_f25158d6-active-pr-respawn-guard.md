# Code Review: D1 active-PR respawn guard after review unblock

- **Review id:** 20260707-230000-t_f25158d6
- **Repo:** hermes-agent (worktree)
- **Branch:** `wren/t_0037406e-active-pr-guard`
- **Base SHA:** `a07a6b1a759f8860a3c2b257cf5576e70576f6f0`
- **Head SHA:** `d058de18c95c9e04064cdd3a41a447aaffa101a1`
- **Changed files:** `hermes_cli/kanban_db.py`, `tests/hermes_cli/test_kanban_db.py`
- **Reviewer:** code-reviewer (DeepSeek v4 Pro)
- **Verdict:** APPROVE

## Summary

Two changes in `kanban_db.py`:

1. **Active-PR unblock gate** (lines 7908-7917): In `check_respawn_guard`, PR comments at or before the latest `unblocked` event are skipped. Only PR activity after the unblock decision can re-arm the `active_pr` guard. Without an unblock event, the gate short-circuits (0 timestamp), preserving the old behavior.

2. **Guard-age alert** (new functions + dispatch integration): After an unblock, if a `respawn_guarded` event for the same reason persists longer than 15 minutes (`_RESPAWN_GUARD_ALERT_SECONDS`), a `respawn_guard_age_alert` event is emitted. The alert is per-reason, anchored to the latest unblock, and suppressed after first emission per unblock span to prevent spam.

## Evidence Checked

| Check | Result |
|-------|--------|
| `pytest -k respawn_guard` (targeted) | 23 passed (matches worker claim) |
| `pytest tests/hermes_cli/test_kanban_db.py` (full) | 230 passed, 0 regressions |
| `git status --porcelain` | clean |
| HEAD matches reported SHA | `d058de18c` confirmed |
| Regression-tests-bite verification (3/4 new tests fail on base `a07a6b1a`) | Bites confirmed |
| `compileall` syntax check | implicit via test pass |

### Regression-test bite verification

Base commit `a07a6b1a` checked out for `kanban_db.py` only; branch test file kept. Results:

- `test_respawn_guard_active_pr_comment_before_unblock_not_guarded` — **FAILS** (base returns `active_pr`; fix correctly returns `None`)
- `test_respawn_guard_active_pr_comment_after_unblock_still_guarded` — **PASSES** (existing behavior preserved — PR comment after unblock is still guarded)
- `test_dispatch_post_review_unblock_respawns_despite_open_pr` — **FAILS** (base strangles the task; fix lets it spawn)
- `test_dispatch_respawn_guard_emits_age_alert_after_unblock` — **FAILS** (`_RESPAWN_GUARD_ALERT_SECONDS` missing on base)

Three of four new tests genuinely guard the change. The fourth (`...after_unblock_still_guarded`) validates the existing behavior the fix must not break — it passes on both base and branch, which is correct.

## Findings

### Critical

None.

### Warnings

None.

### Suggestions

None.

### Looks Good

- **Minimal diff.** Two new helper functions (`_latest_event_created_at`, `_payload_reason_matches`, `_respawn_guard_age_alert_payload`) and one guarded block (~8 lines) in `check_respawn_guard`. No sweeping refactors or new dependencies.
- **Correct unblock semantics.** The gate uses `MAX(created_at)` for `unblocked` events — multiple unblocks are handled correctly (latest one wins). The condition `if latest_unblocked_at and ...` short-circuits to 0 when no unblock exists, preserving the old active-PR guard for tasks that were never review-unblocked.
- **Alert spam prevention.** `_respawn_guard_age_alert_payload` checks for a prior `respawn_guard_age_alert` event with the same reason in the same post-unblock span. After the first alert fires, subsequent ticks suppress duplicates. A fresh unblock resets the window, which is correct — a re-reviewed task that gets stuck again should page the operator again.
- **Per-reason alert tracking.** The `_payload_reason_matches` filter ensures a `blocker_auth` guard's alert doesn't interfere with an `active_pr` guard's alert tracking. Both are independently evaluated.
- **One-tick delay is correct.** Because `_respawn_guard_age_alert_payload` runs inside the same write transaction before the `respawn_guarded` event is written, the alert naturally fires on the *next* tick after 15 minutes of persistence, not on the first guarded tick. This is the right design — "has been persisting" not "just started."
- **Alert enrichment on `respawn_guarded` event.** When the alert fires, the `respawn_guarded` event payload carries `alert: "guard_persisted_after_unblock"` along with `age_seconds` and `threshold_seconds` — operators running `hermes kanban tail` get the diagnosis inline without cross-referencing a separate alert event.
- **SQL safety.** `_latest_event_created_at` uses `?` placeholders for the parameterized `kinds` values; the placeholder count is derived from the Python-side list length, not from user input. No injection risk.
- **Test coverage.** Four targeted tests cover: (a) PR comment before unblock → not guarded, (b) PR comment after unblock → still guarded, (c) end-to-end dispatch after review unblock spawns, (d) guard-age alert emission after 15 minutes. All pass.
- **No shared-state mutation outside the write transaction.** The alert payload function is a pure reader; writes happen only via `_append_event` inside the existing `with write_txn` block.

## Decisions

- No code changes recommended. The fix is correct, minimal, well-tested, and the regression tests bite.
- The `_RESPAWN_GUARD_ALERT_SECONDS = 15 * 60` threshold is reasonable for operator visibility without noise. If the fleet finds alerts too chatty or too slow, it's a one-line constant change.
