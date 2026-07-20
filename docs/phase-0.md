# Phase 0: host capability validation

This spike validates the host integrations required by MachineDeck without
building the Phase 1 application registry or UI. The normal report is read-only.
Lifecycle commands are restricted to the included test fixtures.

## Setup

Production targets Python 3.12 or newer. The spike also supports Python 3.10 so
it can validate older Ubuntu hosts.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

## Read-only validation

```bash
cd backend
.venv/bin/machinedeck-phase0 report
.venv/bin/uvicorn app.phase0.websocket_app:app --host 127.0.0.1 --port 8080
```

The WebSocket endpoint is `ws://127.0.0.1:8080/ws/system-metrics`. It publishes
a fresh CPU, RAM, disk, GPU, Docker, and listening-port report every two seconds.
Unavailable integrations return a structured error rather than crashing the API.

## systemd and journal validation

The fixture is a user service and needs no sudo. Installation modifies the
current user's systemd configuration, so it is deliberately separate from the
read-only report.

```bash
./scripts/install-phase0-user-service.sh
cd backend
.venv/bin/machinedeck-phase0 app example-service start
.venv/bin/machinedeck-phase0 app example-service status
.venv/bin/machinedeck-phase0 app example-service logs --lines 20
.venv/bin/machinedeck-phase0 app example-service stop
```

This proves lifecycle and journal access. The system-wide sudo/helper design is
a later security milestone documented in `docs/security-milestone.md`.

User services may stop after logout unless lingering is enabled. Check it with:

```bash
loginctl show-user "${USER}" -p Linger
```

An administrator can enable it with `sudo loginctl enable-linger <username>`.
This is a host configuration step and is not performed by MachineDeck.

## Docker Compose validation

The fixture publishes nginx on a Docker-selected localhost port, avoiding a
fixed-port collision. It may pull `nginx:alpine` on first use.

```bash
cd backend
.venv/bin/machinedeck-phase0 app compose-fixture start
.venv/bin/machinedeck-phase0 app compose-fixture status
.venv/bin/machinedeck-phase0 report
.venv/bin/machinedeck-phase0 app compose-fixture stop
```

`stop` intentionally does not run `docker compose down`, matching the design
specification's non-destructive default.

Docker socket access is required for the Compose fixture. Membership in the
`docker` group effectively grants root-level control over the host; it should be
treated as a privileged deployment decision, not enabled automatically.

## Safety boundary

The API accepts only an application ID and a fixed action. Units, Compose paths,
commands, and arguments come from the operator-controlled registry at
`backend/config/phase0-applications.yaml`. Registry loading rejects unsafe unit
names, path traversal, Compose projects outside configured roots, duplicate IDs,
and unsupported runtimes. No subprocess uses a shell.

Docker, NVML, and systemd errors are returned as structured unavailable or
failed results; one unavailable integration does not prevent other metrics from
being returned.

## Acceptance checklist

- systemd fixture starts and stops, and its journal is readable;
- Compose fixture starts and stops, and its published port is reported;
- NVML reports the expected two GPUs and per-process VRAM;
- listening host ports are identified;
- the WebSocket continuously publishes live reports.

## Validation record (2026-07-20)

- Unit tests: 16 passed.
- systemd user fixture: started, reached `active/running`, emitted its readiness
  journal entry, then stopped with `MainPID=0` and `ActiveState=inactive`.
- Compose fixture: started successfully, published `127.0.0.1:32768 -> 80/tcp`,
  returned HTTP 200, then stopped. The stopped fixture container and network are
  intentionally retained because Phase 0 uses `compose stop`, not `down`.
- WebSocket: two consecutive reports were received with distinct timestamps and
  all five probe categories.
- GPU host acceptance: passed through direct PyNVML validation from the backend
  environment. NVML initialized with driver `580.159.03` and reported one
  `NVIDIA GeForce RTX 3090` with 24.0 GiB total VRAM. Used/free VRAM and GPU/memory
  utilization were reported correctly.
- GPU sandbox behavior: graceful degradation also passed. The sandbox reported
  `Driver Not Loaded` because it does not expose the required NVIDIA device nodes
  and/or userspace driver libraries; this is an execution-environment limitation,
  not a host driver or probe failure.
- Linger: the check is documented, but the sandbox could not query the host
  login manager. An administrator should run the documented `loginctl` command.
