# Phase 1.5: Network Access

MachineDeck's network-access principle is:

> Local-first, LAN-ready, and never public-by-default.

Phase 1.5 separates trusted home-network access from internet exposure. Native
LAN access is supported explicitly; remote access remains an HTTPS proxy or VPN
concern and is never enabled through router port forwarding or UPnP.

## Implemented foundation

- validated `local`, `lan`, and `proxy` server modes;
- mode-consistent literal bind addresses that fail closed on invalid
  combinations;
- `auto`, `secure`, and `insecure` Cookie policies without consulting request
  forwarding headers;
- backward-compatible Secure Cookies for legacy configuration files that do not
  contain a `[security]` table;
- `--access local|lan|tailscale` installation choices;
- repeatable `--trusted-origin` installation arguments, with HTTPS required for
  the Tailscale choice;
- detected hostname and LAN-address Origins for new LAN installations;
- mode-aware systemd unit rendering and health checks;
- access-mode preservation across upgrade and rollback;
- installed access-URL reporting;
- doctor checks for mode/binding validity, Cookie resolution, configured
  external Origins, and unit/config binding consistency;
- canonical trusted-proxy and trusted-network CIDR configuration with separate
  semantics;
- explicit disabling of Uvicorn's implicit proxy-header transformation;
- bounded `Forwarded` and `X-Forwarded-*` parsing only when the raw TCP peer is
  a configured trusted proxy;
- rejection of mixed, malformed, overlong, obfuscated, or non-IP forwarding
  chains;
- nearest-untrusted-hop client resolution for login throttling and setup safety;
- trusted-network classification that never bypasses administrator
  authentication;
- read-only, LAN-only UFW diagnostics for unavailable, inactive, unreadable,
  missing, subnet-scoped, and IPv4/IPv6 Anywhere rules;
- custom-port and multiple-trusted-network firewall evaluation with
  configuration-derived subnet guidance;
- first-run setup protection that requires both a loopback peer and a local
  browser Origin, preventing an external reverse-proxy request from appearing
  local merely because the proxy connects from loopback.

## Configuration

Local-only mode remains the default:

```toml
[server]
mode = "local"
host = "127.0.0.1"
port = 8080

[security]
cookie_secure = "auto"

[network]
trusted_proxies = []
trusted_networks = ["127.0.0.0/8", "::1/128"]
```

Native home-LAN mode is explicit:

```toml
[server]
mode = "lan"
host = "0.0.0.0"
port = 8080
public_host_lan = "machine-name"
trusted_origins = [
  "http://127.0.0.1:8080",
  "http://localhost:8080",
  "http://machine-name:8080",
  "http://192.168.1.50:8080",
]

[security]
cookie_secure = "auto"

[network]
trusted_proxies = []
trusted_networks = [
  "127.0.0.0/8",
  "::1/128",
  "192.168.1.0/24",
]
```

Tailscale Serve and HTTPS reverse proxies retain loopback binding:

```toml
[server]
mode = "proxy"
host = "127.0.0.1"
port = 8080
trusted_origins = ["https://machine-name.tailnet-name.ts.net"]

[security]
cookie_secure = "auto"

[network]
trusted_proxies = ["127.0.0.1/32", "::1/128"]
trusted_networks = ["127.0.0.0/8", "::1/128"]
```

`auto` resolves to a Secure Cookie in `proxy` mode and a non-Secure Cookie in
`local` and `lan` modes. Every mode retains `HttpOnly`, `SameSite=Strict`, CSRF,
Origin checks, authentication, session expiration, throttling, WebSocket
authentication, and auditing.

Trusted Origins authorize browser CSRF and WebSocket Origin checks. Trusted
proxy CIDRs authorize the direct peer to supply forwarding metadata. Trusted
network CIDRs describe local network policy and diagnostics; they do not grant
authentication or suppress CSRF checks. Forwarding metadata never changes the
configured Cookie policy or public URL.

`trusted_proxies` may remain empty when MachineDeck does not need the original
client IP, protocol, or host supplied by the proxy. Tailscale Serve HTTPS still
works because the Cookie policy and trusted public Origin come from explicit
configuration. Ignoring forwarding metadata reduces the proxy-header attack
surface; Doctor retains an informational warning so the operational tradeoff is
visible.

## Host acceptance

LAN reachability passed on 2026-07-23. The installed configuration was changed
to `mode = "lan"` with a `0.0.0.0` bind, deployed through the normal local
upgrade workflow, and the authenticated Dashboard was successfully accessed
from a different device on the same physical network.

This acceptance confirms the real host binding, upgrade, LAN routing, HTTP
Cookie, and browser login path.

Firewall Doctor host acceptance also passed on 2026-07-23. LAN mode, the
`0.0.0.0:8080` binding, Origin configuration, trusted networks, ignored
untrusted proxy headers, service health, and the read-only UFW behavior all
matched the design.

The following non-PASS results were expected and are retained as risk
information rather than hidden:

- the LAN HTTP session Cookie is not Secure;
- firewall policy is `UNKNOWN` when an unprivileged account cannot read UFW,
  because Doctor does not invoke `sudo`;
- systemd linger is disabled when the operator deliberately elects not to
  enable it.

The accepted configuration also included a direct Tailnet HTTP Origin such as
`http://<tailscale-ip>:8080` and the corresponding trusted `/32` host network.
The Tailnet encrypts transport between peers, but the browser still treats this
URL as HTTP. A future Tailscale Serve HTTPS deployment should use `proxy` mode
with its exact `https://...ts.net` Origin and remove the direct Tailnet HTTP
Origin.

### Adversarial network validation

Run the repeatable security acceptance from a different device on the trusted
LAN for the strongest direct-peer evidence:

```bash
python3 -m venv .validation-venv
source .validation-venv/bin/activate
python3 -m pip install -e './backend[test]'
python3 scripts/validate-phase1.5-lan-security.py \
  --base-url http://192.168.1.50:8080 \
  --username justin
```

The environment setup is only needed when that checkout does not already have
the test dependencies. Use the exact configured trusted Origin. The script
prompts for the password so it does not appear in shell history. It verifies
that a trusted network does not bypass authentication or CSRF, forged
`Forwarded` and `X-Forwarded-*` headers do not bypass authentication, unknown
HTTP Origins are rejected, and an unknown WebSocket Origin cannot read a
Dashboard event. It creates and then revokes one administrator session but does
not register, start, stop, or modify any application.

Adversarial LAN host acceptance passed on 2026-07-23. The live test confirmed:

- the health endpoint returned HTTP 200;
- a trusted network did not bypass authentication or CSRF;
- forged `Forwarded` and `X-Forwarded-*` headers did not bypass authentication;
- an unknown login Origin returned HTTP 403 with `ORIGIN_NOT_ALLOWED`;
- login from the configured Origin succeeded;
- an authenticated POST from an unknown Origin was still rejected;
- an unknown WebSocket Origin was rejected during the handshake with HTTP 403,
  before any Dashboard event was exposed;
- a WebSocket from the configured Origin received `dashboard_snapshot`;
- the temporary test session was revoked successfully with HTTP 204.

Local-mode host acceptance passed on 2026-07-23 with three independent layers
of evidence:

- Doctor reported `local` mode, a `127.0.0.1:8080` bind, and consistent binding
  configuration;
- the host socket table showed a listener only on `127.0.0.1:8080`, with no
  listener on `0.0.0.0` or a LAN address;
- a different device connecting to the host's LAN address received
  `Connection refused`, confirming that the request never reached the
  MachineDeck application layer.

For proxy/Tailscale HTTPS acceptance, first confirm on the MachineDeck host:

```bash
~/.local/share/machinedeck/current/venv/bin/machinedeck doctor
ss -ltnp '( sport = :8080 )'
curl --fail --silent --show-error http://127.0.0.1:8080/health
```

Doctor must report `proxy` mode, `127.0.0.1:8080`, a Secure session Cookie
policy, the exact HTTPS Origin, and consistent unit binding. Configured trusted
proxy CIDRs are required only when forwarded client metadata is intentionally
consumed. An empty list and its Doctor warning are acceptable when forwarding
headers are deliberately ignored. The socket table must contain only the
loopback listener for port 8080.

Then run the same validator from another Tailnet device using the exact HTTPS
Origin:

```bash
python3 scripts/validate-phase1.5-lan-security.py \
  --base-url https://machine-name.tailnet-name.ts.net \
  --username justin
```

For an HTTPS Origin the validator additionally requires the session Cookie to
contain `Secure`, `HttpOnly`, and `SameSite=Strict`, proves that the Cookie is
sent back successfully to an authenticated API, uses certificate-verified
HTTPS through `httpx`, and upgrades the Dashboard test to `wss://`. Its unknown
HTTP and WebSocket test Origins use HTTPS as well.

Proxy/Tailscale HTTPS host acceptance passed on 2026-07-23. The live test
confirmed:

- Doctor reported `proxy` mode with a `127.0.0.1:8080` backend bind and
  `Secure=true`;
- the backend socket listener remained loopback-only;
- Tailscale Serve exposed a tailnet-only HTTPS endpoint that proxied to the
  localhost backend;
- TLS certificate verification and browser login through the configured HTTPS
  Origin succeeded;
- the session Cookie contained `Secure`, `HttpOnly`, and `SameSite=Strict` and
  completed an authenticated HTTPS round trip;
- an authenticated POST from an unknown HTTPS Origin was rejected;
- an unknown WebSocket Origin was rejected during the handshake with HTTP 403;
- the configured `wss://` Origin received `dashboard_snapshot`;
- forged forwarding headers did not bypass authentication.

The accepted host intentionally left trusted proxy CIDRs empty. MachineDeck did
not depend on forwarded metadata for Cookie security, public-Origin selection,
authentication, or routing, so the corresponding Doctor warning was expected.

The current host-acceptance matrix is:

```text
LAN reachability and login:          PASS
LAN adversarial authentication:      PASS
LAN CSRF enforcement:                PASS
LAN Origin enforcement:              PASS
LAN WebSocket Origin enforcement:    PASS
Forwarded-header spoof resistance:   PASS
Local-mode isolation:                PASS
Proxy/Tailscale HTTPS acceptance:    PASS
```

The `local`, `lan`, and `proxy` network foundations and their security
boundaries have now completed real-host acceptance.

## Remaining product work

- optional trusted-network use for setup-token policy, notifications, and risk
  reporting without authentication bypass;
- safe access-mode changes through the Settings UI;
- application endpoint exposure policies (`local`, `lan`, `tailnet`, and
  `custom`);
- observed bind/exposure mismatch warnings.
