# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

Task subscriptions also cover review feedback. A `changes_requested` review
event is delivered as an actionable review-BLOCK notification. Subscriptions
using `notify+wake` additionally wake the exact originating chat/thread/session
so the controller inspects the existing card and current run; `notify` remains
passive-only and `wake` remains wake-only. Review feedback never creates,
unblocks, requeues, or otherwise mutates a task.

## Single-dispatcher posture

Only one gateway owns the kanban dispatcher. Set `kanban.dispatcher_profile`
to that profile on every gateway that shares the board. Nonmatching gateways
skip dispatcher lock acquisition while continuing their profile-local notifier
watchers.

**Why this matters:** dispatching is single-owner so multiple gateways do not
race to spawn the same work. Notification delivery is profile-owned instead:
each gateway polls only subscriptions for profiles whose platform adapters it
hosts. The atomic event claim prevents duplicate delivery across watcher
processes.

## Configuration

Use the same owner pin on every profile gateway (for example, `wren`):

```yaml
kanban:
  dispatch_in_gateway: true
  dispatcher_profile: wren
```

The pinned gateway retries a contended singleton lock at least every five
seconds and acquires it after an old incumbent releases it. An empty or omitted
`dispatcher_profile` preserves the historical behavior where any
dispatch-enabled gateway may acquire the lock and a contended gateway opts out.

To disable embedded dispatch entirely for a gateway or use the standalone
daemon, set `dispatch_in_gateway: false` (or
`HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`). Its notifier watcher remains active.

## What each gateway does

| Gateway role | dispatch_in_gateway | dispatcher_profile | Dispatcher | Notifier |
|---|---|---|---|---|
| pinned owner | true | matches active profile | yes | owned profiles + legacy unstamped subscriptions |
| other profile gateways | true | does not match | no | that gateway's owned profiles |
| explicitly disabled gateway | false | any | no | that gateway's owned profiles |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.). They do not dispatch tasks, and they skip boards
that have no subscriptions owned by their profiles.
