# Harness Health v0.1

Status: v0.1 vertical slice

## Decision

Add a read-only `hermes harness-health` CLI diagnostic. It answers two
separate questions without adding a model tool, background service, telemetry
stream, approval system, or dashboard:

1. What is configured and eligible to shape this profile/session?
2. What does persisted Hermes evidence prove shaped one run?

Collection is deterministic and local. The command never calls an LLM or
provider, never connects to configured MCP servers, and never includes target
memory contents in its output. A later operator surface can consume the same
JSON contract.

## Sources of truth and evidence boundaries

| Surface | Setup source | Runtime source | Boundary |
|---|---|---|---|
| Profile | `get_hermes_home()` | selected `state.db` | Report a stable profile fingerprint, never its private absolute path. |
| Config | strict read-only parse of `config.yaml` | n/a | Malformed or unreadable config makes config-derived setup sections `INACCESSIBLE`; the audit never creates recovery backups or rewrites target config. |
| Project context | `agent.prompt_builder` native discovery, precedence, loaders, and install-tree policy | persisted `sessions.system_prompt` and `sessions.cwd` | Setup executes native renderability checks in a disposable profile. Empty, blocked, or unreadable higher-priority inputs do not shadow a loadable fallback. Runtime reports only portable cwd identity/path tokens and context proven present in the frozen prompt. |
| Skills | native installed `SKILL.md` discovery, external roots, platform/environment eligibility, and disabled-skill resolution | persisted system-prompt index and successful matched `skill_view` result rows | Installed inventory and current eligibility are separate. Rendered names include current names-only/demoted and qualified entries. Failed or unmatched `skill_view` calls do not count as viewed/loaded. Curator counters are cumulative profile telemetry, not selected-run proof. |
| Tools | current platform resolver, registry definitions, `check_fn` filtering, global disabled precedence, and already-loaded ownership mappings | persisted assistant `tool_calls` plus uniquely matched tool-result rows | Eligible means schema-capable under deterministic checks. Configuration does not prove a schema was shown. Calls distinguish used from enabled; orphan, mismatched, and duplicate result rows do not inflate checked capability counts. Credential-store auto-enablement is not probed. |
| Plugins | `plugins.enabled` and `plugins.disabled`; disabled wins | tool calls mapped to loaded plugin ownership or registered `plugin-*` toolsets | Enabled means configured, not successfully loaded. The audit does not execute target-profile plugin discovery. Naming-convention attribution is `INFERRED`; otherwise unavailable historical ownership is `INACCESSIBLE`. |
| MCP | `mcp_servers` interpreted through `enabled_mcp_server_names()` and effective platform resolution | tool calls mapped to registered `mcp-*` toolsets | The audit does not connect to servers. Configured/enabled is not used. Historical ownership is `INACCESSIBLE` unless registry mapping proves it. |
| Memory | `MemoryStore` parser/limits and profile memory files | persisted system-prompt identity only | Report capacity, entry counts, and whole-file SHA-256 snapshot identity; never memory entries, individual-entry hashes, or content. |
| Approvals/checkpoints | effective config defaults, canonical process/current-session YOLO state, and read-only checkpoint store status | persisted tool effects where available | Configured and effective approval states are separate. Historical session YOLO and checkpoint CLI overrides are not persisted, so historical effective state is `INACCESSIBLE`, not copied from profile defaults. |
| Session trace | stable scratch copy of `state.db` plus WAL | active `sessions` and `messages` rows | Direct SQLite read-only opens can create `state.db-shm`; the audit uses `hermes_cli.sqlite_safe_read.offline_file_access()` while copying stable DB/WAL bytes to a disposable directory, rechecks source size/mtime, and queries only the copy. Concurrent source changes or a live tracked in-process connection fail closed. |

Current source code is authoritative for exact precedence and persisted
columns. Relevant sources of truth are `hermes_constants.py`,
`agent/prompt_builder.py`, `agent/skill_utils.py`, `tools/skills_tool.py`,
`hermes_cli/tools_config.py`, `tools/registry.py`, `tools/approval.py`,
`tools/checkpoint_manager.py`, `tools/memory_tool.py`,
`hermes_cli/sqlite_safe_read.py`, and `hermes_state.py`.

The external design reference is
`NateBJones-Projects/clean-my-ai-harness` at commit
`03a5cb582035b772ae6a37fae86c11476a8947cc`. v0.1 borrows only its
read-only-first, visible-blind-spots, and setup-shaping questions. No source,
report format, approval flow, rollback subsystem, or branded wording is
copied.

## Evidence states

Every material claim has one state:

- `VERIFIED`: directly observed in the selected source of truth.
- `INFERRED`: required by a verified downstream event, but the upstream event
  was not persisted.
- `USER_REPORTED`: accepted only from an explicit future input field; v0.1
  does not manufacture these claims.
- `INACCESSIBLE`: the source is absent, unreadable, unsupported, unstable, or
  not persisted.

File existence is inventory only. It never means activation.

## Funnel contract

The ordered stages are always present:

`Available -> Eligible -> Shown -> Consulted -> Acted through -> Checked -> Accepted`

Each stage is an object with `state`, `count`, and a short `basis`. The funnel
uses one coherent population: unique tool capabilities acted through in the
selected session. Setup inventory, skill counts, calls, and result rows are
not mixed.

A trace may be `COMPLETE` only when all seven stages are present, none is
`INACCESSIBLE`, and counts do not increase downstream. Validation fails closed
otherwise. v0.1 normally reports `PARTIAL` because Hermes does not persist
exact historical availability/schema exposure, semantic consultation, or
operator acceptance.

- Available: a selected-session call proves the capability existed at runtime;
  the stage is inferred because the historical registry snapshot is absent.
- Eligible: the call proves the capability survived runtime gates; exact
  historical eligibility is not persisted.
- Shown: the call proves its tool schema was exposed, but the complete schema
  snapshot is not persisted.
- Consulted: inferred from an acted-through call; semantic consultation is not
  persisted.
- Acted through: unique tool capability names in persisted assistant calls.
- Checked: unique acted-through capability names with a result whose tool-call
  ID and tool name both match the originating call.
- Accepted: a persisted human/system acceptance signal.

## JSON and CLI contracts

Top-level JSON keys are stable for v0.1:

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

Portable paths use `$HERMES_HOME`, `$PROJECT_ROOT`, `$CWD`, or
`<outside-scope>`. The report never includes config secret values, memory
text, message text, tool arguments other than validated skill identifiers,
environment values, or private absolute paths.

```text
hermes harness-health [--platform cli] [--cwd PATH] [--session ID] [--json]
                      [--output PATH]
```

Plain text is the concise operator view. `--json` prints the contract.
`--output` writes JSON with exclusive-create semantics and fails if the
destination exists. A destination inside the audited profile is rejected.
Standard output never silently redirects or overwrites.

The audit target is read-only. Output creation is the only write and occurs
outside target-state collection after the report is complete. CLI startup
suppresses normal target-profile log bootstrap. Native context/config probes,
built-in tool discovery, and requirement checks that may initialize state run
under a disposable `HERMES_HOME`. Session DB/WAL bytes are queried only from a
stable scratch snapshot.

## Operating model

Daily: collect quietly and locally; emit nothing when a comparable contract
has not changed and no source became inaccessible.

Weekly: digest changes and anomalies only: newly shadowed context,
setup/runtime divergence, previously used capabilities becoming ineligible,
incomplete traces, and safeguard drift.

Monthly: review behavior only across comparable task cohorts. Do not claim
productivity improvement from inventory counts or one-off sessions.

## Blind spots, rollout, and rollback

v0.1 does not prove semantic reading, capture the exact historical tool-schema
snapshot, persist per-call approval decisions, infer plugin/MCP startup from
configuration, probe credential stores for auto-enabled toolsets, expose
message content or general tool arguments, or define operator acceptance.
Those remain explicit `INACCESSIBLE` or `INFERRED` evidence.

Rollout stops at source, tests, docs, and isolated profile/session exercises.
It includes no gateway restart, live profile activation, scheduler, dashboard
route, or fleet config change.

Rollback removes the CLI parser/dispatch wiring, collector, tests, and this
document. The diagnostic creates no target state to migrate or clean up; any
explicit output file is operator-selected and remains outside target
collection.
