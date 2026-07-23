# MachineDeck installation lifecycle

MachineDeck installs as a systemd user service owned by the current, non-root
user. Do not run the installation scripts through `sudo`.

## Layout

```text
~/.local/share/machinedeck/
├── current -> releases/<version>/
└── releases/
    └── <version>/
        ├── backend/
        ├── frontend/
        ├── scripts/
        ├── venv/
        └── VERSION

~/.local/state/machinedeck/
├── machinedeck.db
└── backups/

~/.config/machinedeck/config.toml
~/.config/systemd/user/machinedeck.service
```

Each release has its own virtual environment so a dependency upgrade cannot
make the previous release unstartable. It is built at its final path with an
`.installing` marker so virtualenv console-script shebangs remain valid; the
release cannot become active until the marker is removed. The `current` link is
validated to remain inside the releases directory and is changed with an atomic rename. State,
configuration, audit history, managed application units, and user workloads are
outside the release directory.

## Install

Requirements are Linux, Python 3.10 or newer, and an available systemd user
manager. Port 8080 must be available unless the installed MachineDeck service is
already using it.

```bash
./scripts/install.sh
```

The installer validates the source tree, destination paths, and SQLite state
directory write access, rejects execution as root, prints every target root,
creates a staged release and release-specific venv, installs local
dependencies, validates the generated unit with `systemd-analyze --user verify`,
runs Alembic migrations, atomically selects the release, enables the service,
and waits for `http://127.0.0.1:8080/health`.

The alpha `standard` service profile uses user-manager-compatible controls:
`NoNewPrivileges`, `PrivateTmp`, `RestrictSUIDSGID`, `LockPersonality`, and
control-group process termination. Kernel, control-group filesystem, and module
protection directives are intentionally deferred until a separate hardened
profile because they are not portable across Ubuntu user-manager environments.

To keep the user manager running after logout, explicitly request linger:

```bash
./scripts/install.sh --enable-linger
```

The interactive command asks for confirmation. Automation must be explicit:

```bash
./scripts/install.sh --yes --enable-linger
```

`--no-start` installs and migrates without enabling or starting the service.

The generated `config.toml` and SQLite database have mode `0600`. Configuration
contains no administrator password and is never overwritten by a repeated
installation. Environment variables continue to take precedence over TOML values.

### Access modes

The installer requires an explicit access-mode selection only when the
local-only default is not appropriate:

```bash
./scripts/install.sh --access local
./scripts/install.sh \
  --access lan \
  --trusted-network 192.168.1.0/24
./scripts/install.sh \
  --access tailscale \
  --trusted-origin https://machine-name.tailnet-name.ts.net
```

The modes map to installed server configuration as follows:

| Installer choice | Server mode | Binding | Automatic Cookie behavior |
| --- | --- | --- | --- |
| `local` | `local` | `127.0.0.1:8080` | `Secure=false` |
| `lan` | `lan` | `0.0.0.0:8080` | `Secure=false` |
| `tailscale` | `proxy` | `127.0.0.1:8080` | `Secure=true` |

All modes retain `HttpOnly` and `SameSite=Strict`, CSRF validation, explicit
Origin validation, login throttling, session expiration, WebSocket
authentication, and audit records. Cookie security and public URLs come only
from configuration; MachineDeck does not infer them from request `Host` or
forwarded headers. Uvicorn's implicit proxy-header transformation is disabled.
MachineDeck parses forwarding metadata itself only when the raw TCP peer belongs
to a configured trusted proxy CIDR.

LAN mode adds the detected hostname and non-loopback host addresses to the
Origin allowlist and prints the resulting access URLs. Detection can vary with
host DNS configuration, so review `[server].trusted_origins` after installation.
LAN HTTP is not encrypted. Restrict it to the intended private subnet, for
example:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
```

Replace that example subnet with the actual home network. MachineDeck does not
configure UFW, router port forwarding, or UPnP automatically.

Initial administrator setup remains local-only in every mode. Complete setup
through `http://127.0.0.1:8080` on the host before signing in from another
device. A reverse proxy's loopback TCP peer does not make an external browser
Origin eligible for first-run setup.

Trust boundaries are configured separately:

```toml
[network]
trusted_proxies = ["127.0.0.1/32", "::1/128"]
trusted_networks = [
  "127.0.0.0/8",
  "::1/128",
  "192.168.1.0/24",
]
```

Trusted Origins authorize browser CSRF and WebSocket Origin checks. Trusted
proxies authorize forwarding metadata only when the direct TCP peer is inside
the configured CIDR. Trusted networks support policy and diagnostics but never
bypass login, CSRF, or WebSocket authentication. Forwarded chains are bounded,
must contain literal IP addresses, and cannot mix `Forwarded` with
`X-Forwarded-*`.

## Upgrade

```bash
./scripts/upgrade.sh --from-local .
```

Upgrade stops an active service, creates a consistent SQLite backup, builds and
validates a new staged release, runs migrations, atomically changes `current`,
starts the service, and performs the HTTP health check. Failure restores the
previous release pointer, unit bytes, and database backup before attempting to
restart the prior service. Failed staging and release directories are removed.

Remote version downloads are intentionally not implemented for the alpha source
workflow. A future release installer must verify a published checksum or
signature rather than execute a moving branch through `curl | bash`.

## Doctor

From a source checkout:

```bash
./scripts/doctor.sh
./scripts/doctor.sh --json
```

From an installed virtual environment:

```bash
~/.local/share/machinedeck/current/venv/bin/machinedeck doctor
```

Doctor checks the release pointer, version, configuration permissions, database
and Alembic revision, systemd user bus, service and PID, HTTP health, Docker
Engine and Compose, journal access, NVML, managed unit symlinks, linger, and disk
space.

Exit codes are:

```text
0  healthy
1  healthy with warnings
2  errors
3  unsupported or not installed
```

Docker, Compose, NVML, and linger are optional and produce warnings rather than
making installation unsupported.

## Uninstall

The default is deliberately non-destructive:

```bash
./scripts/uninstall.sh
```

It disables and removes only the MachineDeck control-plane service and installed
application releases. It preserves:

```text
~/.local/state/machinedeck/
~/.config/machinedeck/
~/.config/systemd/user/machinedeck-*.service
```

Permanently deleting the database, administrator, sessions, audit history, and
configuration requires an explicit non-interactive confirmation:

```bash
./scripts/uninstall.sh --purge --yes
```

Even purge retains managed application units. Removing those requires a second,
independent option:

```bash
./scripts/uninstall.sh --purge --remove-managed-units --yes
```

Neither form stops or deletes user application data, Docker containers, images,
volumes, or Compose projects.

## Tailscale HTTPS

For a new private-tailnet installation, select proxy mode and provide the exact
HTTPS Origin:

```bash
./scripts/install.sh \
  --access tailscale \
  --trusted-origin https://machine-name.tailnet-name.ts.net
```

For an existing installation, keep MachineDeck bound to loopback and update
`config.toml` explicitly:

```toml
[server]
mode = "proxy"
host = "127.0.0.1"
trusted_origins = [
  "http://127.0.0.1:8080",
  "http://localhost:8080",
  "https://machine-name.tailnet-name.ts.net"
]

[security]
cookie_secure = "auto"

[network]
trusted_proxies = ["127.0.0.1/32", "::1/128"]
trusted_networks = ["127.0.0.0/8", "::1/128"]
```

Run a local upgrade to regenerate the service unit from configuration, then
configure the private proxy:

```bash
./scripts/upgrade.sh --from-local .
sudo tailscale serve --bg 8080
```

Use the HTTPS `*.ts.net` URL. Do not expose port 8080 on `0.0.0.0` or use a raw
Tailscale IP over HTTP, because that bypasses the proxy boundary and cannot
provide the configured HTTPS transport boundary.

## Failure and ownership guarantees

- MachineDeck-specific destination paths and managed files may not be symlinks.
- The source application, migration, and frontend trees may not contain symlinks.
- Subprocess arguments are passed as arrays and never interpolated through a shell.
- Database backups use the SQLite backup API rather than copying a live database.
- Unit and configuration writes use same-directory temporary files and atomic rename.
- A failed first install removes its new database, configuration, unit, and release.
- A failed upgrade restores the prior release, unit, and database.
- Default uninstall never removes state, configuration, managed units, or workloads.
