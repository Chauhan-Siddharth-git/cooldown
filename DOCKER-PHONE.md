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

**6. Try it.** On the phone, open `reddit.com` → the **Countdown** gate.

**7. (Optional) Gate this computer's browser too.** This variant serves an explicit
proxy for the laptop as well — set the browser's proxy to **`127.0.0.1:8081`** and
install the CA from `http://mitm.it` (same as [DOCKER.md](DOCKER.md) steps 3–4).
⚠️ Note the port: it's **8081** here, not `8080` — in this variant `8080` is reserved
for the phone's transparent interception, so the laptop uses `8081`.

**Stop:** `docker compose -f docker-compose.tailscale.yml down`
(add `-v` to also wipe data, the CA, and the Tailscale identity).

---

## Verify it's working, one layer at a time

This variant is a stack of layers; test them in order so you know exactly where a
failure is instead of guessing. `C=docker compose -f docker-compose.tailscale.yml`.

**Layer 1 — the container is up and both processes run.**
```bash
$C ps                         # cooldown + redis both "Up" (redis healthy)
$C logs cooldown | grep -E "proxy listening|Tailscale up"
```
Expect mitmproxy's `HTTP(S) proxy listening` and the `[tailscale] up ...` line.

**Layer 2 — Tailscale joined the tailnet.**
```bash
$C exec cooldown tailscale status   # shows this node + your other devices
```
Also check the Tailscale **admin console** — `cooldown-docker` should appear. Then
**approve it as an exit node** (setup step 3). Not approved = nothing routes.

**Layer 3 — the router plumbing is in place** (inside the container's namespace):
```bash
$C exec cooldown iptables -t nat -S PREROUTING   # REDIRECT tailscale0 :80/:443 -> 8080
$C exec cooldown iptables -S FORWARD | grep 443  # the QUIC REJECT
```

**Layer 4 — the phone routes through it.** Select the exit node on the phone, then on
the phone load any *non-gated* site (e.g. `example.com`). It should load normally —
that proves forwarding + NAT work. If the phone has **no** internet here, it's almost
always the exit node not being approved (Layer 2).

**Layer 5 — the CA is trusted.** On the phone, `http://mitm.it` should show the
cert-install page (not an error). Install + trust it (setup step 5).

**Layer 6 — the gate fires.** On the phone, open `reddit.com` → the **Countdown**
gate. Then open a YouTube video and let it run — if it gets gated (rather than
sailing through), QUIC blocking is working too.

**Layer 7 — the laptop browser still works** in parallel: set its proxy to
`127.0.0.1:8081`, visit `reddit.com`, expect the gate.

If a layer fails, the one below it is fine — so you only ever debug one thing.

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
