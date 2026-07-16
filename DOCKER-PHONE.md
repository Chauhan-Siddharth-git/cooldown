# Gating a phone with Docker (advanced, Tailscale)

This variant gates the tempting sites **on your phone** — transparently, over
Tailscale, even on cellular — *and* in this computer's browser, at the same time.
It's the containerized version of the Raspberry Pi setup.

> ### Read this before you start
>
> **A Raspberry Pi is the better home for this.** This variant works, but a laptop
> is the wrong shape for it: your phone stays gated **only while this computer is
> awake, online, and on your tailnet**. Close the lid, sleep, or leave the house and
> your phone loses the gate (and, while set as your exit node, routes its internet
> through a machine that just vanished). A Pi is always-on and dedicated — that's the
> whole reason the main project targets one. Use this variant if you don't have a Pi
> yet and accept the tradeoffs.
>
> **This is a privileged container.** To act as a Tailscale exit node and router it
> runs as **root** with `NET_ADMIN`, the `/dev/net/tun` device, and IP forwarding on.
> That's a real trust step — it can manipulate networking. The plain
> [DOCKER.md](DOCKER.md) onramp (non-root, one browser, no networking powers) stays
> the safe default; only use this one deliberately.
>
> **It routes your phone's *entire* internet through this box.** Everything except the
> gated sites passes straight through, but it all flows through the container. Keep the
> [trust model in SECURITY.md](SECURITY.md) in mind.

**Does not work on Docker Desktop for Mac/Windows the "grab my LAN" way — but this
way does.** Tailscale is an *overlay* tunnel that rides the container's normal
outbound internet, so it reaches your phone without needing to see the host's LAN.
That's why phone-gating works here even though `--network host` wouldn't.

---

## What you need

- Docker + Compose (see [DOCKER.md → Before you start](DOCKER.md#before-you-start)).
- A **Tailscale account** with your **phone already on the tailnet** (the Tailscale
  app installed and logged in). Free tier is plenty.
- A **Tailscale auth key** — generate one at
  <https://login.tailscale.com/admin/settings/keys> ("Reusable" on, "Ephemeral" off).

---

## Setup

**1. Get the code and add your auth key.**

```bash
git clone https://github.com/Chauhan-Siddharth-git/cooldown.git
cd cooldown
cp .env.example .env
# edit .env — paste your key into TS_AUTHKEY, set TZ
```

`.env` is gitignored, so your key stays local. **Never commit it.**

**2. Build and start** (note the `-f` — this uses the Tailscale compose file):

```bash
docker compose -f docker-compose.tailscale.yml up -d --build
docker compose -f docker-compose.tailscale.yml logs -f cooldown
```

The logs will print a line telling you the box joined the tailnet and to approve it.

**3. Approve the exit node** in the Tailscale admin console:
*Machines → `cooldown-docker` → ⋯ → Edit route settings → **Use as exit node** → Save.*
(Exit nodes are opt-in; nothing routes through it until you approve.)

**4. Turn it on for your phone.** In the Tailscale app on the phone:
*Exit Node → select `cooldown-docker`.* Now the phone's traffic routes through the
container.

**5. Install the CA on the phone (once).** With the exit node active, visit
**http://mitm.it** on the phone, install the profile, and **trust it fully**:
- **iPhone:** install the profile, then *Settings → General → About → Certificate
  Trust Settings* and toggle it **on** (the step everyone misses).
- **Android:** install under *Security → Encryption & credentials → Install a
  certificate → CA certificate*.

**6. Try it.** On the phone, open `reddit.com` → the **Countdown** gate. Your laptop
browser can use it too by setting its proxy to `127.0.0.1:8081`.

**Stop:** `docker compose -f docker-compose.tailscale.yml down`
(add `-v` to also wipe data, the CA, and the Tailscale identity).

---

## Troubleshooting

| Symptom | Cause & fix |
|---|---|
| Logs say `TS_AUTHKEY is required` | You didn't `cp .env.example .env` or didn't paste a key. |
| Phone has no internet after selecting the exit node | The exit node isn't approved yet (step 3), or the container isn't forwarding — check `docker compose -f docker-compose.tailscale.yml logs cooldown` for the iptables/tailscale lines. |
| Sites load but no gate | CA not trusted on the phone (redo step 5, incl. the iPhone "trust" toggle), or the exit node isn't actually selected on the phone. |
| YouTube slips through | QUIC — should be blocked by the container's rules; confirm the `FORWARD ... udp --dport 443` lines appear in the logs. |
| Gate vanishes randomly | Your computer slept / dropped off wifi. This is the always-on problem — the reason a Pi is better. |
| `/dev/net/tun` errors on build/run | Your Docker can't pass the tun device (rare on Docker Desktop; more common on locked-down hosts). |

---

## Why this exists / when to graduate to a Pi

This proves the phone-gating works from a laptop, and it's a fine way to try it. But
the moment you want it *reliable* — gated all day, not tied to your laptop's lid —
move to the Pi setup in [SETUP.md](SETUP.md). Same code, a box that never sleeps.
