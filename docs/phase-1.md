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
WS     /ws/system-metrics
```

Lifecycle actions are connected only through saved manifests that are parsed and
revalidated immediately before each operation. No API-provided command, unit
name, Compose path, or subprocess argument can reach a runtime adapter. Lifecycle
POST endpoints reject non-empty request bodies.

Process manifests currently map to deterministic user units named
`machinedeck-{application-id}.service`. Rendering and installing those units from
validated process manifests is the next process-runtime slice; until then, the
unit must already exist. Compose applications are operational in this slice.

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

1. Render and install user-level systemd units for validated process manifests.
2. Add real-time journal and Docker log WebSockets.
3. Expose execution history and audit-event query APIs.
4. Add the single-administrator authentication boundary.
5. Build the initial dashboard and application pages.
