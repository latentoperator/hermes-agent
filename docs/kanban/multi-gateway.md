# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

## Single-dispatcher posture

Only one profile should own the embedded kanban dispatcher. Pin that owner with
`kanban.dispatcher_profile`; non-owner gateways then skip the dispatcher and
kanban notifier before attempting the singleton lock, so they do not race for the
board at all.

`kanban.dispatch_in_gateway: true` remains the feature gate for the embedded
dispatcher. Leave it enabled on gateway profiles that should honour the pin. Set
it to `false` only when the gateway must not participate in kanban dispatching at
all, or when a separate dispatcher process owns the board.

**Why this matters:** the dispatcher and notifier open per-board SQLite
connections. If every profile gateway races for ownership, one stale winner can
self-perpetuate while normal fleet restarts exclude it. Pinning the dispatcher to
one profile makes ownership explicit and recoverable; the singleton lock remains
a same-profile restart/orphan backstop.

## Configuration

On every gateway that has `dispatch_in_gateway` enabled, set the same pin:

```yaml
kanban:
  dispatch_in_gateway: true
  dispatcher_profile: wren      # example: only the wren gateway dispatches
  no_dispatcher_alarm_after_seconds: 300
```

A gateway whose active profile is not `dispatcher_profile` exits the dispatcher
and notifier paths without touching the singleton lock. The pinned profile keeps
retrying the singleton lock every `dispatch_interval_seconds` instead of opting
out forever after a startup-time loss.

For a gateway that must never run kanban dispatching, keep using:

```yaml
kanban:
  dispatch_in_gateway: false
```

Or set the env var: `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`

## What each gateway does

| Gateway role | dispatch_in_gateway | dispatcher_profile match? | Opens per-board DBs? | Runs dispatcher + notifier? |
|---|---|---|---|---|
| pinned dispatch owner | true | yes | yes | yes |
| other profile gateways | true | no | no | no |
| explicitly disabled gateway | false | n/a | no | no |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.) — they just don't poll kanban boards.
