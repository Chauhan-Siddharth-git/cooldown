# Security & Trust Model

**Read this before you run Cooldown.** Cooldown works by man-in-the-middling your own
HTTPS traffic. That is powerful and, done wrong, dangerous. This document is the
part you don't get to skip.

## What Cooldown actually does to your traffic

Cooldown routes your phone/laptop traffic through a small box you run (a Raspberry Pi
is the reference target), where **mitmproxy decrypts HTTPS, injects a script, and
serves a gate page** for the sites you choose to budget. To decrypt HTTPS,
mitmproxy generates a **root Certificate Authority (CA)**, and you install that
CA as *trusted* on your device.

A device that trusts a CA will accept **any** certificate that CA signs. So the
holder of that CA's **private key** can transparently decrypt and modify HTTPS
from that device — for *any* site, not just the ones Cooldown gates.

## The rules that follow from that

1. **Generate your OWN CA. Never install a CA you didn't generate.**
   mitmproxy creates one on first run (in `~/.mitmproxy/`). Use that. **Do not**
   download, copy, or trust a CA certificate from this repo, from a release, from
   a stranger, or from anyone else — trusting someone else's CA hands them the
   ability to decrypt your traffic. This repo ships **no** CA material, on
   purpose.

2. **The CA private key never leaves your box, and never enters git.**
   It lives in `~/.mitmproxy/mitmproxy-ca.pem` (or wherever you point mitmproxy).
   The `.gitignore` here blocks `*.pem`, `*.key`, `certs/`, `.mitmproxy/`, etc. —
   keep it that way. If a key is ever exposed, **regenerate it** (delete
   `~/.mitmproxy/`, restart, re-install the new CA) and untrust the old one.

3. **This is for gating YOUR devices via a box YOU control.**
   Cooldown is not a hosted service and must never be run as one. The whole model
   assumes you own both ends. Don't install its CA on anyone else's device.

## Network exposure

- The reference deployment reaches the box over **Tailscale** (a private WireGuard
  mesh), so the proxy isn't exposed to the public internet.
- mitmproxy binds `0.0.0.0`/`[::]` on its proxy ports, so `deploy/cooldown-redirect.sh`
  firewalls them: it adds interface-scoped `INPUT` rules (v4 + v6) that accept
  `8080/8081` only on the Tailscale interface (and loopback) and **DROP them
  everywhere else**. Without this the proxy is reachable from your LAN — and, if the
  box has a routable IPv6, from the public internet. Anything that can reach the
  proxy *and* trusts your CA can be MITM'd, so keep those rules in place (they're
  re-applied on boot by `cooldown-redirect.service`). Verify with
  `sudo iptables -S INPUT` — you should see the `tailscale0`/`lo` ACCEPTs above a
  catch-all DROP for those ports.
- QUIC (UDP/443) is blocked so clients fall back to interceptable TCP. See
  `deploy/cooldown-redirect.sh`.

## Data

All state (time spent, cooldowns, usage history) lives in a local Redis on your
box. There is **no telemetry** and nothing leaves your machine. Usage history is
just per-day, per-site charged seconds.

## The bypass is intentional

Enforcement relies on your device routing through the box. Turning the VPN off
bypasses the gate. That's a deliberate *soft* friction, not a security boundary —
Cooldown is a commitment device for a cooperative user (you, vs. your own impulses),
**not** an adversarial lockdown. Don't rely on it to restrain a motivated
attacker; that was never its threat model.

## Reporting

Found a real security issue in the code? Open an issue describing the impact (omit
live exploit details if it puts users at risk) or contact the maintainer directly.
