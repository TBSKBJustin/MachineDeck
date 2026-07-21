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
- one shared two-second Dashboard collection loop feeding REST and every client;
- timestamped CPU, per-core, load, RAM, swap, configured-disk, uptime, GPU,
  VRAM, sensor, GPU-process, and application-summary snapshots;
- bounded latest-value subscriber queues so slow clients cannot block collection;
- responsive first-run/login and Dashboard pages with LIVE, STALE, and OFFLINE
  semantics and per-collector degraded-state presentation.
- authenticated Audit Log list/detail APIs with stable timestamp-and-ID cursor
  pagination and time, application, actor, action, result, category, execution,
  and keyword filters;
- normalized actor, target, request, category, action, and execution-link output
  while retaining readable legacy and deleted-application events;
- recursive secret, credential, request-body, environment, path, token, and URL
  redaction with bounded detail size at the API boundary;
- responsive Audit Log table, filter controls, pagination, and a detail drawer
  that safely renders unknown and older event types.
- source-based user-service install, upgrade, uninstall, and doctor commands with
  per-release virtual environments and an atomic current-release pointer;
- real TOML configuration with environment precedence and separated application,
  state, backup, configuration, and user-unit directories;
- unit verification, Alembic migration, SQLite backup, HTTP health validation,
  and code/unit/database rollback on deployment failure;
- non-destructive default uninstall, explicit purge and managed-unit deletion,
  linger opt-in, machine-readable doctor results, and Tailscale HTTPS guidance.

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
GET    /api/v1/dashboard
GET    /api/v1/audit-events
GET    /api/v1/audit-events/{event_id}
GET    /api/v1/auth/status
POST   /api/v1/auth/setup
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
GET    /api/v1/auth/session
WS     /ws/system-metrics
WS     /ws/v1/dashboard
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

Browser origins on genuine loopback addresses are trusted on any local port.
Non-loopback origins must be explicitly listed with comma-separated
`MACHINEDECK_TRUSTED_ORIGINS`. Session lifetime and login
window settings are configurable with `MACHINEDECK_AUTH_SESSION_HOURS`,
`MACHINEDECK_LOGIN_MAX_FAILURES`, and `MACHINEDECK_LOGIN_WINDOW_MINUTES`.
MachineDeck still binds to `127.0.0.1` by default; a LAN deployment must configure
its exact trusted HTTPS origin and terminate TLS without disabling Secure Cookies.
Adding a remote origin does not enable first-run setup: create the administrator
through `127.0.0.1` or `localhost` on the server before exposing the HTTPS entry
point.

Authentication acceptance is covered by the full test suite: localhost-only
setup, the singleton database constraint, Argon2id persistence, Cookie flags,
CSRF and hostile-Origin rejection, throttled/recorded login failures, logout and
expiry revocation, password-error scrubbing, pre-accept WebSocket rejection, and
established-connection revocation checks all pass.

### Live Dashboard

`MetricsService` owns one continuous collection loop and one immutable latest
snapshot. REST reads and WebSocket subscribers reuse that snapshot; opening more
browsers does not create additional psutil or NVML calls. The loop intentionally
continues without subscribers so REST is immediately current. Each subscriber
queue has capacity one and replaces an unread snapshot with the newest value.

Snapshots expose `collected_at`, `collection_duration_ms`, and freshness. Data up
to five seconds old is `LIVE`, five to fifteen seconds is `STALE`, and older data
is `OFFLINE`. The browser also computes this from `collected_at`, so a disconnected
stream cannot leave stale numbers looking live. New WebSockets receive the latest
snapshot immediately and accept no query parameters or tokens.

Host, disk, NVML, and application summary collectors fail independently. Missing
NVML returns an empty GPU list and `NVML_NOT_AVAILABLE`; a failed GPU remains in
the list as unavailable without hiding healthy GPUs. Unsupported temperature,
fan, or power sensors remain null. Configured disks that disappear are marked
unavailable. Disk paths default to `/`, the user home, and the project disk root,
and can be replaced with colon-separated `MACHINEDECK_MONITOR_DISKS`.

GPU process enrichment is an independent service and retains PID, process name,
VRAM, managed ownership, and application ID fields for future GPU scheduling.
The Phase 1 page displays process counts while preserving the richer API model.

Host acceptance passed on 2026-07-20. The shared collector returned a LIVE
snapshot in about 111 ms with CPU/RAM and all configured disks, one RTX 3090,
24 GiB VRAM, utilization, temperature, power, fan, and four GPU processes. A
temporary end-to-end server then served the frontend with CSP/frame protections,
returned an authenticated REST snapshot, sent an immediate WebSocket snapshot,
and closed that socket with `4401` after logout. Temporary database and server
state were removed. The complete suite now contains 145 passing tests.

### Audit log

Audit events are ordered by creation time and ID, both descending, so cursor
pagination remains deterministic even when multiple events share a timestamp.
The list endpoint accepts `start`, `end`, `application_id`, `actor`, `action`,
`result`, `category`, `execution_id`, and `keyword` filters. Keyword search is
deliberately limited to non-sensitive indexed event fields rather than raw
details.

Registry and lifecycle events created through authenticated APIs record the
administrator username plus a validated request method and API path. Lifecycle
events link to their persisted execution, including failure codes and safe
runtime context such as a port conflict. Application names are copied into
registry audit events so deletion does not make their history unintelligible.

Every response passes through a recursive output sanitizer even though event
writers are also expected not to persist secrets. Passwords, cookies, session
and CSRF tokens, authorization values, secret environment containers, sensitive
paths, credential-like URL parameters, JWTs, excessive nesting, oversized
collections, and details larger than 16 KiB are redacted or bounded. The UI
inserts all event-controlled text through DOM text nodes rather than HTML.

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
not influence the URL. LAN deployment requires an exact trusted origin and TLS
termination so Secure session Cookies remain effective.

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

Open `http://127.0.0.1:8080/` for first-run administrator setup and the live
Dashboard. The REST schema remains available at `/docs`.

The default database is `backend/data/machinedeck.db`. Override it with
`MACHINEDECK_DATABASE_URL`. Allowed application roots default to the parent
project directory and can be replaced with a colon-separated
`MACHINEDECK_ALLOWED_ROOTS` value.

## Next slice

1. Run the complete install/upgrade/rollback/uninstall/reinstall host acceptance
   using the real systemd user manager.
2. Freeze the alpha directory layout and prepare the Phase 1 release candidate.
