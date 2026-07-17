# Harness Health v0.1

Status: v0.1 vertical slice

## Decision

Add a read-only `hermes harness-health` CLI diagnostic. It answers two separate questions without adding a model tool, background service, telemetry stream, approval system, or dashboard:

1. What is configured and eligible to shape this profile/session?
2. What does persisted Hermes evidence prove shaped one run?

Collection is deterministic and local. The command never calls an LLM or a provider, never connects to configured MCP servers, and never imports target memory contents into its output. A later dashboard can consume the same JSON contract.

## Sources of truth and evidence boundaries

| Surface | Setup source | Runtime source | Boundary |
|---|---|---|---|
| Profile | `get_hermes_home()` | selected `state.db` | Report a stable profile fingerprint, never the private absolute home path. |
| Config | read-only parse of `config.yaml` | n/a | Malformed or unreadable config makes config-derived setup sections `INACCESSIBLE`; the audit never creates recovery backups or rewrites target config. |
| Project context | `agent.prompt_builder` native filename/precedence rules | persisted `sessions.system_prompt` and `sessions.cwd` | Setup reports the candidate native loading actually uses; empty/unreadable higher-priority files do not shadow loadable fallbacks. Runtime reports only portable cwd identity/path tokens and the context kind proven present in the frozen prompt. |
| Skills | native installed `SKILL.md` discovery across profile/external skill roots plus `tools.skills_tool._find_all_skills()` and explicit disabled-skill resolution | persisted system-prompt snapshot and successful matched `skill_view` result rows | Installed inventory and current eligible skills are separate. Failed or unmatched `skill_view` calls do not count as loaded/viewed. Curator counters are cumulative profile telemetry, not per-run proof. |
| Tools | native platform resolver plus registry schema/check-fn filtering, with plugin startup against the target profile and credential-store probes disabled | persisted assistant `tool_calls` + unique matched tool result rows | Eligible means schema-capable under deterministic checks. Configuration does not prove a schema was shown. A persisted call proves acted-through and infers its upstream run-time stages; orphan, mismatched, or duplicate tool-result rows do not inflate checked capability counts. Credential-gated auto-enablement remains an explicit blind spot. |
| Plugins | `plugins.enabled`/`plugins.disabled` config, disabled deny-list wins | tool calls mapped to registered plugin toolsets when provable | Enabled means configured, not successfully loaded. v0.1 does not execute plugin discovery against the target profile. Unattributable historical plugin use remains `INACCESSIBLE`. |
| MCP | `mcp_servers` config interpreted through `enabled_mcp_server_names()` | persisted tool calls mapped to registered `mcp-*` toolsets when provable | The command does not connect to MCP servers; configured tools remain unproven until runtime evidence exists. Unattributable historical MCP use remains `INACCESSIBLE`. |
| Memory | `MemoryStore` parser/limits and the two profile files | persisted system-prompt identity only | Report usage counts and SHA-256 snapshot identities; never report entries or hashes of individual entries. |
| Approvals/checkpoints | effective config defaults, canonical audit-process/current-session YOLO state, and read-only checkpoint store status | persisted tool effects where available | Configured and effective approval states are separate. Historical session YOLO and checkpoint CLI overrides are not persisted, so historical effective checkpoint state is `INACCESSIBLE` rather than copied from the profile default. |
| Session trace | scratch copy of `state.db` plus a stable WAL snapshot | `sessions` and active `messages` rows | Opening the target database directly can create `state.db-shm` even with SQLite `mode=ro`, so the audit copies stable DB/WAL bytes to a disposable directory, rechecks source size/mtime, and queries only the copy. Concurrent source changes fail closed as `INACCESSIBLE`. |

Current Hermes docs confirm profile selection, the offline CLI diagnostic pattern (`prompt-size`), bounded frozen memory snapshots, progressive skill disclosure, MCP startup discovery/filtering, and SQLite session persistence. Source code remains authoritative for exact precedence and persisted columns.

The external design reference is `NateBJones-Projects/clean-my-ai-harness` at commit `03a5cb582035b772ae6a37fae86c11476a8947cc`. v0.1 borrows only the read-only-first, visible-blind-spots, and setup-shaping questions described in its README. No source, report format, approval flow, rollback subsystem, or branded wording is copied; the repository has no standard LICENSE file and its README reserves competing derivatives.

References:

- https://hermes-agent.nousresearch.com/docs/user-guide/configuration
- https://hermes-agent.nousresearch.com/docs/reference/cli-commands
- https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
- https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
- https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- https://github.com/NateBJones-Projects/clean-my-ai-harness/tree/03a5cb582035b772ae6a37fae86c11476a8947cc

## Evidence states

Every material claim has one state:

- `VERIFIED`: directly observed in the selected source of truth.
- `INFERRED`: required by a verified downstream event, but the upstream event was not persisted.
- `USER_REPORTED`: accepted only from an explicit future input field; v0.1 does not manufacture these claims.
- `INACCESSIBLE`: the source is absent, unreadable, unsupported, or not persisted.

File existence is inventory only. It never means activation.

## Funnel contract

The ordered stages are always present:

`Available -> Eligible -> Shown -> Consulted -> Acted through -> Checked -> Accepted`

Each stage is an object with `state`, `count`, and a short `basis`. The funnel is one coherent population—unique tool capabilities acted through in the selected session—not a mixture of setup inventory, skill counts, and tool calls. Setup inventory remains under `setup`. A trace has `completeness: COMPLETE` only when all seven stages are present, none is `INACCESSIBLE`, and counts do not increase downstream; validation fails closed otherwise. v0.1 normally reports `PARTIAL` because Hermes does not persist exact historical availability/schema exposure, semantic consultation, or operator acceptance.

Stage meanings:

- Available: capabilities proven by a downstream selected-session call to have existed at run time; state is inferred because the historical registry snapshot is not persisted.
- Eligible: capabilities proven by that call to have survived run-time gates; state is inferred because the historical eligible-schema snapshot is not persisted.
- Shown: tool schemas exposed to the model; v0.1 infers at least one shown schema from a persisted tool call when no exact schema snapshot exists.
- Consulted: tool schema consulted; v0.1 infers this from acted-through calls because exact schema consultation is not persisted.
- Acted through: unique tool capabilities named by persisted assistant tool-call events.
- Checked: unique acted-through tool capabilities with at least one persisted result whose tool-call ID and tool name both match the originating call.
- Accepted: a persisted human/system acceptance signal.

## JSON contract

Top-level keys are stable for v0.1:

```json
{
  "schema_version": "0.1",
  "evidence_states": ["VERIFIED", "INFERRED", "USER_REPORTED", "INACCESSIBLE"],
  "profile": {"identity": "sha256:..."},
  "scope": {"platform": "cli", "cwd": "$CWD", "session_identity": null},
  "setup": {
    "context": {},
    "skills": {},
    "tools": {},
    "plugins": {},
    "mcp": {},
    "memory": {},
    "safeguards": {}
  },
  "runtime": {
    "state": "INACCESSIBLE",
    "context": {},
    "session_cwd": {},
    "skills": {},
    "tools": {},
    "plugins": {},
    "mcp": {}
  },
  "funnel": {"completeness": "PARTIAL", "stages": []},
  "blind_spots": []
}
```

Portable paths use `$HERMES_HOME`, `$CWD`, or `<outside-scope>` tokens. The report never includes config secret values, memory text, message text, tool arguments other than validated skill identifiers, environment values, or private absolute paths.

## CLI contract

```text
hermes harness-health [--platform cli] [--cwd PATH] [--session ID] [--json]
                      [--output PATH]
```

Plain text is the concise operator view. `--json` prints the contract. `--output` writes the JSON contract with exclusive-create semantics and fails if the destination already exists. Standard output never silently redirects or overwrites.

The audit target is read-only. Output creation is the only write and occurs outside target-state collection after the report is complete. The CLI entrypoint suppresses its normal target-profile `agent.log`/`errors.log` bootstrap for this command, built-in tool discovery and requirement checks run under a disposable `HERMES_HOME`, and session DB/WAL bytes are queried only from a stable scratch snapshot.

## Operating model

Daily: collect quietly and locally; emit nothing when the comparable contract has not changed and no source becomes inaccessible.

Weekly: digest changes and anomalies only—newly shadowed context, setup/runtime divergence, previously used capabilities becoming ineligible, incomplete traces, and safeguard drift.

Monthly: review behaviour only when comparable task cohorts exist. Compare like-for-like runs before discussing consultation, verification, or acceptance patterns. Do not claim productivity improvement from inventory counts or one-off sessions.

## Blind spots and rollout boundary

v0.1 does not prove semantic reading, capture the exact tool-schema snapshot for historical sessions, persist per-call approval decisions, infer successful plugin/MCP startup from config, probe credential stores for auto-enabled toolsets, expose message content/tool arguments, or define acceptance on the operator's behalf. Those remain explicit `INACCESSIBLE`/`INFERRED` stages.

Rollout stops at source, tests, docs, and an isolated profile/session exercise. No gateway restart, live profile activation, scheduler, dashboard route, or fleet config change is included. Rollback is removal of the new CLI module/parser wiring, tests, and this document; the diagnostic creates no target state to migrate or clean up.
