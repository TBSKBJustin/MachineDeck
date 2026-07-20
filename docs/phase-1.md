# Phase 1: MVP

Phase 1 is in progress. The first backend slice establishes a persistent,
validated application registry before persisted applications are allowed to
control host workloads.

## Completed foundation

- SQLAlchemy application and audit-event models;
- SQLite session configuration with foreign-key enforcement;
- initial Alembic migration for `applications` and `audit_events`;
- versioned process and Docker Compose manifest schemas;
- allowed-root, executable, working-directory, Compose-file, environment-name,
  port, tag, and unknown-field validation;
- application create, list, get, update, delete, and dry-run validation APIs;
- audit records for application creation, update, and deletion;
- host overview and live metric WebSocket routes carried forward from Phase 0.
- persisted application instances and lifecycle execution history;
- explicit state-transition validation and application-level lifecycle locks;
- safe Docker Compose start, stop, restart, status, and log adapters;
- deterministic user-systemd status and lifecycle operations limited to
  `machinedeck-{application-id}.service`;
- reconciliation of persisted state against actual runtime state;
- lifecycle audit records for successful and failed operations.
- pure, directive-injection-safe user-unit rendering for process manifests;
- same-directory staging, `systemd-analyze --user verify`, atomic replacement,
  user-daemon reload, and automatic restoration of the previous unit on failure;
- startup reconciliation and an API-visible `MATCH`, `MISSING`, `MISMATCH`, or
  `UNSAFE` consistency result for persisted process definitions;
- lifecycle/configuration locking that prevents update or deletion while an
  application is starting, running, unhealthy, or stopping.
- unified journal and Docker Compose log events over WebSocket;
- cursor-based journal history/follow handoff, Compose service/container
  attribution, and Compose reader reconnection after container recreation;
- bounded per-connection queues with dropped-message warnings;
- 64 KiB line limits, invalid UTF-8 handling, sequence numbers, and secret
  redaction before WebSocket output;
- explicit reader cancellation, subprocess termination, and backend-shutdown
  connection cleanup.
- validated manual HTTP, HTTPS, TCP, and UDP port declarations kept separate
  from runtime-observed listeners;
- process listener ownership through deterministic user units, systemd cgroups,
  and all descendant PIDs;
- Compose host-published port discovery through Docker Engine metadata,
  including dynamic ports, IPv4, IPv6, TCP, UDP, service, and container identity;
- conservative pre-start/restart conflict validation with managed-application
  ownership and failed execution/audit persistence;
- trusted Open Web UI URL generation that never consumes request `Host` or
  forwarded-host headers.
- single-administrator setup and login with Argon2id password hashing;
- server-side, revocable sessions using hashed random tokens and `HttpOnly`,
  `Secure`, `SameSite=Strict` cookies;
- CSRF tokens and trusted-Origin checks for state-changing requests;
- persistent per-client login-failure throttling and authentication audit events;
- authentication and Origin validation before either WebSocket is accepted.

Audit event persistence is complete. Query APIs, filtering, pagination, and the
Audit Log UI remain outstanding and are tracked separately in the roadmap.

## API available in this slice

```text
GET    /health
GET    /api/v1/system/overview
GET    /api/v1/applications
POST   /api/v1/applications
POST   /api/v1/applications/validate
GET    /api/v1/applications/{application_id}
PUT    /api/v1/applications/{application_id}
DELETE /api/v1/applications/{application_id}
POST   /api/v1/applications/{application_id}/validate
POST   /api/v1/applications/{application_id}/start
POST   /api/v1/applications/{application_id}/stop
POST   /api/v1/applications/{application_id}/restart
GET    /api/v1/applications/{application_id}/status
GET    /api/v1/applications/{application_id}/logs?lines=200
GET    /api/v1/applications/{application_id}/unit-consistency
GET    /api/v1/applications/{application_id}/ports
POST   /api/v1/applications/{application_id}/ports/refresh
GET    /api/v1/applications/{application_id}/endpoints?scope=local
GET    /api/v1/system/ports
GET    /api/v1/auth/status
POST   /api/v1/auth/setup
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
GET    /api/v1/auth/session
WS     /ws/system-metrics
WS     /ws/v1/applications/{application_id}/logs
```

Lifecycle actions are connected only through saved manifests that are parsed and
revalidated immediately before each operation. No API-provided command, unit
name, Compose path, or subprocess argument can reach a runtime adapter. Lifecycle
POST endpoints reject non-empty request bodies.

Process manifests map to deterministic user units named
`machinedeck-{application-id}.service`. MachineDeck renders only a fixed set of
directives, validates the staged unit, installs it atomically, reloads the user
daemon, and rolls back the previous bytes when reload or startup fails. Unit
files are not deleted when an application record is deleted, and active
applications cannot be updated or deleted.

Host acceptance passed on 2026-07-20: the generated fixture unit was verified,
installed, started as `active/running`, reported `MATCH`, stopped successfully,
and finished with `MainPID=0`, `ActiveState=inactive`, and `SubState=dead`.

The process-log host acceptance received one historical journal event and one
live event after the historical cursor through the WebSocket. Disconnecting the
client left no `journalctl --follow` process, and the acceptance unit was removed
with `scripts/validate-phase1-process.py --cleanup` semantics. The Compose-log
acceptance attributed both `api` and `worker` messages to their service and
container IDs, received a live event, released its reader process, and removed
the test containers and network.

### Log WebSocket protocol

```text
/ws/v1/applications/{application_id}/logs
  ?history=200
  &follow=true
  &since=2026-07-20T12:00:00Z
  &cursor=s=...
  &services=api,worker
```

Only these query parameters are accepted. Unit names, Compose files, project
directories, and subprocess arguments always come from the saved and revalidated
manifest. The stream emits `status`, `log`, `warning`, `error`, and `eof`
envelopes. A slow client receives `LOG_MESSAGES_DROPPED`; an unavailable source
receives `LOG_SOURCE_UNAVAILABLE`.

Each WebSocket currently owns an independent reader. Queues are bounded at 1000
events and individual messages at 64 KiB. Journal events retain their cursor;
Docker Compose events retain service and container attribution. Duplicate
Compose events may occur during reconnect and are preferred over losing logs.

### Administrator authentication

`GET /api/v1/auth/status` is public and reports only whether initial setup is
required and whether the current Cookie authenticates. Initial setup is accepted
only from the direct loopback peer and a database uniqueness constraint permits
exactly one administrator. Passwords are hashed with Argon2id and are scrubbed
from request-validation responses.

Session and CSRF tokens use independent cryptographically random values. Only
SHA-256 token digests are persisted. The session Cookie is always `HttpOnly`,
`Secure`, `SameSite=Strict`, and scoped to `/`; its default lifetime is 12 hours.
State-changing protected requests require `X-CSRF-Token` and reject untrusted
Origins. Login failures are throttled per direct peer over a persistent 15-minute
window. Setup, successful and failed login, rate limiting, and logout produce
audit events without passwords, Cookies, tokens, or raw client addresses.

All application and system `/api/v1` routes require authentication. Both metric
and log WebSockets verify the session and an explicitly trusted Origin before
`accept()`, never accept query-string tokens, and do not reveal whether an
application exists to anonymous callers. Established connections recheck the
server session and close with code `4401` within approximately two seconds after
expiry or logout.

Trusted browser origins default to localhost on port 8080 and can be replaced
with comma-separated `MACHINEDECK_TRUSTED_ORIGINS`. Session lifetime and login
window settings are configurable with `MACHINEDECK_AUTH_SESSION_HOURS`,
`MACHINEDECK_LOGIN_MAX_FAILURES`, and `MACHINEDECK_LOGIN_WINDOW_MINUTES`.
MachineDeck still binds to `127.0.0.1` by default; a LAN deployment must configure
its exact trusted HTTPS origin and terminate TLS without disabling Secure Cookies.

Authentication acceptance is covered by the full 108-test suite: localhost-only
setup, the singleton database constraint, Argon2id persistence, Cookie flags,
CSRF and hostile-Origin rejection, throttled/recorded login failures, logout and
expiry revocation, password-error scrubbing, pre-accept WebSocket rejection, and
established-connection revocation checks all pass.

### Ports and Open Web UI

Manifest ports use `host_port` and a literal-IP `bind_address`. Web endpoints can
also declare `path`, `health_path`, `primary`, and `open_in_browser`; TCP and UDP
ports cannot carry Web-only fields. Ports below 1024 are rejected unless
`MACHINEDECK_ALLOW_PRIVILEGED_PORTS=true` is explicitly configured.

The API preserves declared and observed data independently. Runtime status is
reported as `DECLARED`, `LISTENING`, `NOT_LISTENING`, `UNDECLARED`, `CONFLICTED`,
or `UNKNOWN`. Process discovery resolves the unit control group before matching
host sockets. Compose discovery reads only host-published bindings; Docker SDK
metadata is preferred, with parameterized `docker ps` and `docker inspect` as a
safe Engine-API fallback when the host SDK is unavailable or incompatible.

Start and restart fail closed with `PORT_DISCOVERY_UNAVAILABLE` when declared
ports cannot be checked. Conflicts return `PORT_CONFLICT` with protocol, address,
port, PID/process information when available, and registered-application
ownership. Failed preflight attempts are retained in execution history and the
audit-event store. Wildcard address checks are intentionally conservative and
TCP/UDP namespaces remain separate.

Open Web UI is a validated URL response, never a backend `xdg-open` action.
Wildcard and loopback endpoints use `MACHINEDECK_PUBLIC_HOST_LOCAL` (default
`127.0.0.1`) or the optional `MACHINEDECK_PUBLIC_HOST_LAN`; request headers do
not influence the URL. LAN binding remains prohibited operationally until
authentication is implemented.

Port checks reduce obvious startup failures but cannot eliminate the TOCTOU
window before the workload binds its socket. Post-start observed status remains
the source of truth.

Host acceptance passed on 2026-07-20. A generated systemd user unit was mapped
through its cgroup to its Python HTTP listener, a Compose fixture's dynamically
published `127.0.0.1` port was attributed to its service/container, and an
external Python listener produced a non-managed structured conflict. The
fixture unit, container, and network were removed successfully afterward.

## Development

```bash
cd backend
python3 -m pytest
alembic upgrade head
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

The default database is `backend/data/machinedeck.db`. Override it with
`MACHINEDECK_DATABASE_URL`. Allowed application roots default to the parent
project directory and can be replaced with a colon-separated
`MACHINEDECK_ALLOWED_ROOTS` value.

## Next slice

1. Build the initial dashboard and application pages.
2. Expose execution history and audit-event query APIs and UI.
3. Add the formal systemd installation and upgrade workflow.
4. Prepare the Phase 1 release candidate.
