# Remote Dashboard Auth

Hermes dashboards use two different auth shapes depending on where they bind.

- Loopback binds (`127.0.0.1`, `localhost`, `::1`) use the process-local
  `HERMES_DASHBOARD_SESSION_TOKEN` injected into the served dashboard shell.
- Non-loopback binds are gated by dashboard auth providers. Browser clients use
  a verified session cookie, then mint short-lived WebSocket tickets through
  `POST /api/auth/ws-ticket`.

The `--insecure` flag does not bypass the non-loopback auth gate.

## Native Desktop Token Bridge

Native Desktop clients may connect to a remote dashboard without browser
cookies when the operator explicitly enables the bridge:

```sh
HERMES_DASHBOARD_ALLOW_REMOTE_SESSION_TOKEN=1
HERMES_DASHBOARD_SESSION_TOKEN=<shared-token>
```

When enabled, a valid `X-Hermes-Session-Token` header authenticates remote
`/api/*` calls before the cookie gate runs. The same token-authenticated caller
can mint WebSocket tickets from `POST /api/auth/ws-ticket`, which keeps remote
chat on the gated-mode `?ticket=` WebSocket path instead of reopening the legacy
`?token=` path.

Keep the bridge disabled unless a trusted native client needs it. The token is a
machine credential for the dashboard API surface and should come from the same
secret source as the dashboard services that need to accept it.

## Probe

Use the same token configured for the dashboard service:

```sh
curl -fsS \
  -H "X-Hermes-Session-Token: $HERMES_DASHBOARD_SESSION_TOKEN" \
  "http://<dashboard-host>:<port>/api/profiles/active"

curl -fsS \
  -X POST \
  -H "X-Hermes-Session-Token: $HERMES_DASHBOARD_SESSION_TOKEN" \
  "http://<dashboard-host>:<port>/api/auth/ws-ticket"
```

Expected results:

- With the header: both endpoints return `200`.
- Without the header or a valid cookie: both endpoints return `401` on a
  non-loopback dashboard.
