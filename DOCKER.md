# Running Cooldown with Docker

This is the quickest way to try Cooldown on **one machine** — your laptop or
desktop. It runs mitmproxy as an **explicit HTTP proxy**: you point a browser (or
the whole OS) at it, install the CA once, and the gated sites start counting down.

No iptables, no Tailscale, no root — that heavier setup is only needed for the
"gate my phone over the network" deployment (see [Transparent mode](#transparent-mode-the-pi-setup) below).

> **Read [SECURITY.md](SECURITY.md) first.** Cooldown works by man-in-the-middling
> your own HTTPS. The CA it generates can decrypt traffic from any device that
> trusts it. Generate your own (the steps below do), keep it on your machine, and
> never install someone else's.

---

## What you get

```
        ┌─────────────── docker compose ───────────────┐
  your  │  cooldown  (one image)          redis         │
browser─┼─▶ mitmproxy :8080  ──▶  Flask :5000 ──▶ :6379 │
 proxy  │   (interceptor)         (brain)      (memory) │
setting └───────────────────────────────────────────────┘
         only :8080 is published, on 127.0.0.1 only
```

- **`cooldown`** — one image running the mitmproxy interceptor (`:8080`, explicit
  proxy) and the Flask brain (`:5000`, container-internal only).
- **`redis`** — state (time spent, cooldowns, history) on a persistent volume.
- Two named volumes: `cooldown-ca` (the CA — so you re-trust only once) and
  `cooldown-redis` (your usage data).

---

## Quick start

**1. Bring it up** (set `TZ` so the nightly reset and bedtime land at your local time):

```bash
TZ=America/New_York docker compose up -d --build
```

**2. Point your browser at the proxy.** Set the HTTP **and** HTTPS proxy to
`127.0.0.1` port `8080`. In Firefox: *Settings → Network Settings → Manual proxy
configuration*, tick "Also use this proxy for HTTPS". (A browser-level proxy keeps
the interception scoped to that browser — cleaner than a system-wide proxy for a
first try.)

**3. Install the CA (once).** With the proxy on, visit **http://mitm.it**, download
the certificate for your OS, and install it as **trusted**. This is what lets
mitmproxy decrypt the gated sites. (`mitm.it` only resolves *through* the proxy —
that's why it's in the allowlist.)

**4. Try it.** Visit `reddit.com`. You should get the **Countdown** gate instead of
the feed. Everything except the gated sites (Reddit, YouTube, Spotify web, Puzzmo)
tunnels straight through untouched.

**Stop / start / logs:**

```bash
docker compose logs -f cooldown   # watch it work
docker compose down               # stop (volumes, so your CA + data, persist)
docker compose down -v            # stop AND wipe data + CA (full reset)
```

---

## Adding or changing gated sites

A site lives in **three** places that must agree, or it won't gate:

1. `SITES` in `app.py` (budget rules)
2. `SITES` in `addon.py` (host matching + rewriting)
3. `--allow-hosts` in `docker-entrypoint.sh` (the TLS-decrypt allowlist)

Miss #3 and the site tunnels through un-intercepted. After editing, rebuild:
`docker compose up -d --build`.

---

## Notes & gotchas

- **The CA is precious.** It lives in the `cooldown-ca` volume. `docker compose down`
  keeps it; `down -v` destroys it (you'd re-trust a new one). If it's ever exposed,
  wipe the volume, bring the stack back up to generate a fresh CA, and untrust the
  old one on your devices.
- **Only `127.0.0.1:8080` is published.** The compose file binds the proxy to host
  loopback on purpose — an intercepting proxy open to your LAN is a real risk. Don't
  change it to `0.0.0.0`.
- **Flask and Redis are never exposed** — they only talk over the private compose
  network. Nothing off your machine can reach them.
- **QUIC:** in explicit-proxy mode the browser sends everything over the proxy as
  TCP, so there's no QUIC to block (that's a transparent-mode concern only).
- **The bypass is intentional.** Turn the proxy setting off and the gate is gone.
  Cooldown is a commitment device for a cooperative user, not an adversarial lock.

---

## Transparent mode (the Pi setup)

Gating a **phone**, or a whole machine without per-app proxy settings, needs traffic
*transparently* redirected into mitmproxy — which requires host-level iptables and a
way to route the device through the box (Tailscale as an exit node). That is
host networking, not something a container can own, so it lives outside Docker in
`deploy/` (`cooldown-redirect.sh`, the systemd units) and is documented in
[SETUP.md](SETUP.md). Use Docker for a single machine; use the `deploy/` path to
gate other devices.
