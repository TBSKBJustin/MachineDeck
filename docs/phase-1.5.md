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

## Host acceptance

LAN reachability passed on 2026-07-23. The installed configuration was changed
to `mode = "lan"` with a `0.0.0.0` bind, deployed through the normal local
upgrade workflow, and the authenticated Dashboard was successfully accessed
from a different device on the same physical network.

This acceptance confirms the real host binding, upgrade, LAN routing, HTTP
Cookie, and browser login path. It does not by itself complete the remaining
adversarial Origin/WebSocket checks or the local and proxy/Tailscale acceptance
matrix.

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

### Adversarial LAN validation

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

## Remaining work

- optional trusted-network use for setup-token policy, notifications, and risk
  reporting without authentication bypass;
- safe access-mode changes through the Settings UI;
- application endpoint exposure policies (`local`, `lan`, `tailnet`, and
  `custom`);
- observed bind/exposure mismatch warnings;
- complete adversarial LAN Origin/WebSocket checks and the local and proxy HTTPS
  host-acceptance matrix.
