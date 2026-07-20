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

Authentication is not implemented yet, so the WebSocket does not yet satisfy an
unauthorized-client rejection test. MachineDeck must remain bound to localhost
until the single-administrator authentication boundary is complete.

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

1. Add real-time journal and Docker log WebSockets.
2. Expose execution history and audit-event query APIs.
3. Add manual ports, discovery, conflict checks, and Open Web UI URLs.
4. Add the single-administrator authentication boundary.
5. Build the initial dashboard and application pages.
