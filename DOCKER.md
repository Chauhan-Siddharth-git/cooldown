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

## What this can and can't do

**Can:** gate the tempting sites in **a browser on the same computer** that runs
Docker. Great for trying Cooldown, or for gating your own laptop/desktop browsing.

**Can't (by design):**

| Question | Answer |
|---|---|
| **Will this gate my phone?** | **No.** The proxy is locked to this computer only (`127.0.0.1`). A phone can't reach it. Gating a phone — especially on cellular — needs the Raspberry Pi + Tailscale setup ([SETUP.md](SETUP.md)); that's what it's for. |
| Will it gate other apps, or my whole computer? | Only what you point at the proxy. The steps below set it up for one browser. |
| Will it work when I close the terminal? | Yes — the containers keep running in the background until you `docker compose down`. |

If your goal is "stop *me* from doomscrolling on my **phone**," this Docker version
is **not** the tool — use the Pi setup. If your goal is "try it out" or "gate my
computer's browser," you're in the right place.

---

## Before you start

You need **Docker** installed:

- **Mac / Windows:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  — it includes everything (Compose too). Start it before running the commands.
- **Linux:** install [Docker Engine](https://docs.docker.com/engine/install/) **and**
  the Compose plugin (`sudo apt install docker-compose-v2` on Ubuntu/Debian). To run
  `docker` without `sudo`, add yourself to the group once — `sudo usermod -aG docker $USER`
  — then **log out and back in**.

Check it works: `docker compose version` should print a version number.

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

**The Docker-specific files** (all at the repo root, where `docker compose` expects
them): `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.dockerignore`.
The image also bakes in the app itself — `app.py`, `addon.py`, `requirements.txt` —
so those need to be present too (they are, in a clone). You don't edit any of these
to run it; just the commands below.

---

## Quick start

**1. Get the code and enter the folder.** `docker compose` builds the image from the
files in this repo, so you need them locally first:

```bash
git clone https://github.com/Chauhan-Siddharth-git/cooldown.git
cd cooldown
```

(No `git`? Use GitHub's green **Code → Download ZIP** button, unzip it, and `cd` into
the unzipped folder in your terminal.)

**2. Bring it up** (set `TZ` so the nightly reset and bedtime land at your local time).
Run this from inside the `cooldown` folder — that's where `docker-compose.yml` lives:

```bash
TZ=America/New_York docker compose up -d --build
```

The `--build` step compiles the image the first time (a minute or two); after that it
starts in seconds.

**3. Point your browser at the proxy.** Set the HTTP **and** HTTPS proxy to
`127.0.0.1` port `8080`. In Firefox: *Settings → Network Settings → Manual proxy
configuration*, tick "Also use this proxy for HTTPS". (A browser-level proxy keeps
the interception scoped to that browser — cleaner than a system-wide proxy for a
first try.)

**4. Install the CA (once).** With the proxy on, visit **http://mitm.it**, download
the certificate for your OS, and install it as **trusted**. This is what lets
mitmproxy decrypt the gated sites. (`mitm.it` only resolves *through* the proxy —
that's why it's in the allowlist.)

**5. Try it.** Visit `reddit.com`. You should get the **Countdown** gate instead of
the feed. Everything except the gated sites (Reddit, YouTube, Spotify web, Puzzmo)
tunnels straight through untouched.

### Verifying from the command line

The gate only fires on a **browser navigation** — the addon keys off the
`Sec-Fetch-Mode: navigate` header a browser sends on a top-level page load, so
sub-requests (images, APIs) don't get served the gate HTML. A bare `curl` sends no
such header, so it looks like a sub-request and passes through to the real site
(which may then reject it) — that is *not* a failure. To test like a browser:

```bash
# Should print a 200 whose body contains "Countdown" (or a bedtime message at night):
curl -sk -x http://127.0.0.1:8080 -H "Sec-Fetch-Mode: navigate" https://www.reddit.com/ | grep -o Countdown
```

`curl ... http://mitm.it` returning 200 also confirms the proxy itself is up.

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

## Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `permission denied ... /var/run/docker.sock` | Your user isn't in the `docker` group. `sudo usermod -aG docker $USER`, then **log out and back in** (a new terminal alone isn't enough). Or prefix commands with `sudo`. |
| `docker compose: unknown command` or `unknown shorthand flag: 'd'` | The Compose plugin isn't installed. Linux: `sudo apt install docker-compose-v2`. Mac/Windows: use Docker Desktop. |
| `docker compose logs` shows nothing | If `docker compose ps` says the container is **Up**, it's just fine — nothing has needed to log yet. (Output is unbuffered, so real activity *will* show.) If it says **Exited/Restarting**, the log will have the error. |
| Browser shows a **certificate warning** on a gated site | The CA isn't installed/trusted. Redo step 3 — visit `http://mitm.it` *through the proxy* and install the cert as trusted. Firefox has its **own** cert store (Settings → Certificates), separate from the OS. |
| A gated site loads normally instead of the gate | The browser isn't actually using the proxy (recheck step 2), **or** you have an active session — you'd get the gate again after the budget runs out or you restart. |
| `curl` through the proxy returns `503` / no gate | Not a bug — the gate only fires on a real *browser navigation*. See [Verifying from the command line](#verifying-from-the-command-line). |

**Start fresh** if things get weird: `docker compose down -v` (wipes data **and** the
CA — you'll re-trust a new one), then `docker compose up -d --build`.

**Uninstall completely:** `docker compose down -v`, then untrust/remove the Cooldown
CA from your browser/OS certificate store.

---

## Transparent mode (the Pi setup)

Gating a **phone**, or a whole machine without per-app proxy settings, needs traffic
*transparently* redirected into mitmproxy — which requires host-level iptables and a
way to route the device through the box (Tailscale as an exit node). That is
host networking, not something a container can own, so it lives outside Docker in
`deploy/` (`cooldown-redirect.sh`, the systemd units) and is documented in
[SETUP.md](SETUP.md). Use Docker for a single machine; use the `deploy/` path to
gate other devices.
