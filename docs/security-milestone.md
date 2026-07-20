# Future system-wide service security milestone

Phase 0 deliberately uses user-level systemd services and does not grant the
MachineDeck backend sudo or root privileges. The lifecycle protocol is scoped
independently of systemd so a system-wide adapter can be introduced later
without changing API routes or application semantics.

Before a system-wide backend is implemented, the project must define and test a
privilege boundary with at least these properties:

- a dedicated, unprivileged `machinedeck` service account runs the web backend;
- system-level unit files and their drop-ins are root-owned and not writable by
  the backend or a managed application;
- unit names and application paths come only from a trusted registry;
- a narrowly scoped privileged helper, Polkit policy, or rigorously reviewed
  sudo model exposes typed lifecycle operations rather than arbitrary commands;
- symlink, path traversal, unit alias, template-unit, drop-in override, and
  argument-injection attacks are included in integration tests;
- secrets, Docker access, unit generation, and registry modification each have
  an explicit ownership and authorization model;
- compromise impact and recovery procedures are recorded in a threat model.

Docker group membership is effectively root-level host access and must not be
treated as an ordinary low-privilege permission. Compose files, volume mounts,
commands, and paths must remain operator-controlled.

No system-wide sudo or Polkit configuration is part of Phase 0.
