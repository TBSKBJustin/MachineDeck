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

## Remaining work

- optional trusted-network use for setup-token policy, notifications, and risk
  reporting without authentication bypass;
- safe access-mode changes through the Settings UI;
- application endpoint exposure policies (`local`, `lan`, `tailnet`, and
  `custom`);
- observed bind/exposure mismatch warnings;
- doctor checks for firewall policy;
- complete LAN HTTP and proxy HTTPS host-acceptance testing.
