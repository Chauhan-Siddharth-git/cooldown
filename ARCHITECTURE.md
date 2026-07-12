# How Cooldown Works — a field guide

A tour of the whole machine: what each part does and how a single tap on a Reddit
link becomes a countdown. Each idea is **plain terms first**, then **under the
hood** for the curious. New words are defined in the [glossary](#glossary).

---

## The idea

**The problem:** feeds are engineered to erase your sense of time. Willpower loses;
a hard "blocked" wall just gets ripped out in frustration.

**The bet:** the enemy isn't *total* time — it's the unbroken 45-minute trance. So
Cooldown gives you a small budget of *foreground* time on the tempting sites, then
makes you take a break. **The pause is the whole point.**

> *Under the hood:* the tool has to know when you're actually **looking** at the
> page (not just that traffic flowed), count that time, and swap the site for a
> "Countdown" page when the budget is spent. Doing that needs a box that can see
> inside your traffic — which is the rest of this guide.

---

## The big picture

Everything routes through one small computer you own, between your devices and the
internet:

```
                        ┌─────────── The box · a Raspberry Pi ───────────┐
  You                   │  mitmproxy      Flask          Redis           │
  phone / laptop ──────▶│  (interceptor)  (brain)        (memory)        │──────▶  Reddit
     private tunnel     └────────────────────────────────────────────────┘  real   YouTube
                                                                            internet
```

Three small programs run on the box, easiest to remember by their **jobs**:

- **The interceptor** — reads and rewrites your traffic
- **The brain** — holds the budget rules
- **The memory** — remembers how much time you've spent

---

## The journey of a tap

What actually happens, start to finish, when you open a Reddit link — the heart of it:

1. **You tap a Reddit link.** Your phone's internet travels through the box first.
   *(The phone routes via the box — a private tunnel that works on Wi-Fi and cellular.)*
2. **The box grabs the web traffic.** A firewall rule redirects all web traffic into
   the interceptor.
   *(iptables redirects ports 80/443 → mitmproxy; the faster "QUIC" protocol is
   blocked so the browser falls back to one the box can read.)*
3. **The interceptor unlocks the page.** Because your phone trusts the box's
   certificate, the box can read the encrypted page — the only reason this is possible.
   *(mitmproxy terminates the TLS using its own trusted CA certificate.)*
4. **It asks the brain: any time left?** Checks the memory for an active session and
   remaining budget for this site.
   *(Looks up session + spent time in Redis, via the Flask logic.)*
5. **Decision: gate, or let you in.** No time / cooldown → show the "Countdown" page.
   Time left → let the real page load, but slip in a tiny invisible script first.
   *(Serves the budget page, OR injects the heartbeat script and passes the page through.)*
6. **The clock ticks while you look.** The injected script pings the box every few
   seconds — but only while the tab is on screen. Each ping spends a little budget.
   Spend it all and the gate returns, starting a cooldown.
   *(Visibility-gated heartbeat → server subtracts elapsed time → cooldown at zero.)*

---

## …and the trip back

Reaching the site was only half the round trip — the most interesting rewriting
happens on the way **back** to you.

```
  Reddit ──▶  [ The box · rewriting on the way back ]  ──▶  Your phone
  sends the    1. strip CSP (the page's script rules)        renders it &
  real page    2. inject heartbeat + remove Shorts           runs the script
               3. re-encrypt with the box's certificate
```

The page your phone shows is **not quite** the one the site sent — de-clawed (Shorts
and the endless feed removed) and wired with the timer, all invisibly. Your browser
can't tell: it arrives sealed with a certificate the phone already trusts.

> *Under the hood:* mitmproxy's response hooks run on the return trip —
> `responseheaders` deletes the site's `Content-Security-Policy` (which normally
> forbids injected code), then the `response` hook splices in the heartbeat script
> and the YouTube declutter. mitmproxy re-encrypts with its own certificate, so the
> browser renders it as if it came straight from the site.

From then on the loop runs itself: the injected heartbeat makes its **own** requests
back to the box every few seconds — tunnel → redirect → proxy → brain → memory — so
the box keeps the clock honest without you lifting a finger.

---

## The stack — what runs where

Everything lives on one Raspberry Pi, in layers — the anatomy of the box:

```
┌─ Raspberry Pi · Debian Linux · always on ──────────────────────────┐
│                                                                     │
│  ① Network plumbing — how traffic gets in                          │
│     Tailscale (tailscale0) · iptables :80/:443 → 8080               │
│     firewall: proxy ports → Tailscale only · QUIC (UDP 443) blocked │
│                                                                     │
│  ② Processes — 3 systemd services (user "pi", Python venv)          │
│     cooldown-proxy   mitmproxy   :8080 + :8081                      │
│     cooldown-app     Flask       :5000  (localhost)                 │
│     redis-server     Redis       :6379  (localhost)                 │
│                                                                     │
│  ③ On disk — what persists                                          │
│     ~/.mitmproxy/   the CA key (never leaves the box)               │
│     Redis AOF       spent time, cooldowns, history                  │
│     ~/cooldown/     the code (app.py, addon.py)                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Who talks to whom** — everything but the proxy is localhost-only:

```
  Browser ──▶ mitmproxy ──▶ Flask ──▶ Redis
   (tunnel)   :8080/:8081    :5000     :6379
                            localhost  localhost
```

Only the interceptor faces the network (and it's locked to Tailscale). The brain
and memory listen on localhost only — nothing off the box can reach them.

### Each part, in depth

| Part | Job | Where it lives · listens · talks to |
|---|---|---|
| **Interceptor** — mitmproxy | Reads each page; serves the gate or injects the timer. The only part facing the network. Decrypts HTTPS, strips CSP, injects the heartbeat, removes YouTube Shorts + feed. | `addon.py` · service `cooldown-proxy` · listens `:8080` (transparent) + `:8081` (regular) · → Flask `:5000` |
| **Brain** — Flask | Owns all the rules: budget size, cooldowns, night mode, refills. Serves the gate/stats pages and `/heartbeat`, `/enter`. | `app.py` · service `cooldown-app` · listens `:5000` (localhost) · → Redis `:6379` · run by the waitress WSGI server |
| **Memory** — Redis | Remembers spent time, cooldowns, usage history (keys like `spent:main`, `cooldown:main`). | service `redis-server` · listens `:6379` (localhost) · persists via an append-only file on disk |

---

## The clever bits

### 1 · Charging only the time you're actually looking

```
  Tab on screen                 Tab hidden / phone locked
  ♥ · · ♥ · · ♥ · · ♥           · · · · · · · · · ·
  pings every 10s               no pings
  → budget ticks down           → completely free
```

This is what makes the budget honest. A crude tool charges you for *traffic*;
Cooldown charges you for **attention**, using the browser's own "is this tab
visible?" signal.

### 2 · One shared bucket, with a cooldown wall

- **Shared bucket** — all sites draw from it, with per-site caps (Reddit 10m, YouTube 15m).
- **Drain it completely** → a hard 1-hour cooldown.
- **Step away** → it slowly refills, but only after a grace period, so you can't
  "sip" by waiting a minute.

> *Under the hood:* a refill credits the bucket at a slow rate once you've been idle
> past a grace window; a full drain sets `cooldown:main` and the hard wall. It's a
> small state machine, pinned by 56 tests.

### 3 · A day that winds down to bedtime

```
  |————————— DAY —————————|— WIND-DOWN —|——— NIGHT ———|
  7am                    10pm         11pm          7am
  full budget + refill    ramps down    tiny buffer, then closed
```

Deliberately **soft** — a wind-down and a small (independent, non-refilling) night
buffer rather than a hard lockout, so it never tempts you to switch the whole thing
off. A separate **Study mode** (locked to a course playlist) stays open at all hours
— the productive escape hatch.

---

## Why it's built this way

- **A VPN tunnel, not a DNS blocker.** Routing every packet gives request-level
  control — read paths, inject scripts, work on cellular. DNS only sees domain names.
- **The mobile browser, not the apps.** Native apps pin their certificates and can't
  be intercepted; browsers can. The app being ungateable is *why* the plan is to
  remove it.
- **A Pi at home, not the cloud.** You own the box and the data, no subscription, and
  the certificate that can read your traffic never leaves your house.
- **Soft friction, not a hard lock.** A wall you can't pass gets torn down; a pause
  you respect survives. Every "no" degrades gently and leaves an escape hatch.

---

## Glossary

| Term | In plain language |
|---|---|
| **Proxy** | A middleman your traffic passes through; it can inspect or change what flows by. |
| **MITM** | *Man-in-the-middle* — sitting between two parties reading/altering their conversation. Malicious when done *to* you; here **you** do it to your own traffic, on purpose. |
| **HTTPS / TLS** | The lock icon. TLS scrambles web traffic so only the two ends can read it — which is why the box needs a trick to see inside. |
| **Certificate / CA** | A *Certificate Authority* vouches for who a site is. If your phone trusts the box's CA, it accepts the box's stand-in certificate — letting it decrypt your pages. The tool's superpower and biggest responsibility. |
| **VPN** | *Virtual Private Network* — an encrypted tunnel carrying your traffic elsewhere first (here, to the box). |
| **WireGuard / Tailscale** | WireGuard is the modern VPN tech; Tailscale is the easy tool built on it that connects your devices to the box, even over cellular. |
| **Exit node** | The device a VPN sends your internet *out* through. The box is your exit node. |
| **mitmproxy** | The software that intercepts, decrypts, and rewrites pages. |
| **Flask** | A small Python web framework — the "brain." |
| **Redis** | A fast in-memory database — the "memory." |
| **iptables** | Linux's built-in firewall/routing; steers web traffic into the interceptor and blocks what it can't read. |
| **Port** | A numbered "door" on a computer — programs listen on different ports so traffic reaches the right one (mitmproxy 8080/8081, Flask 5000, Redis 6379). |
| **localhost** | The machine talking to *itself*. Flask and Redis only accept localhost connections, so nothing off the box can reach them. |
| **systemd** | Linux's service manager — keeps the three programs running and restarts them on boot (why they're "services"). |
| **WSGI / waitress** | The plumbing that lets a Python web app (Flask) receive real requests; **waitress** is the production-grade version used here. |
| **QUIC** | A newer, faster web transport (used heavily by YouTube); blocked so browsers fall back to the inspectable kind. |
| **Heartbeat** | The tiny injected script that pings the box every few seconds *while the tab is visible*. |
| **Session / cooldown** | A *session* is an active "you're allowed in" pass; a *cooldown* is the enforced break once the budget is spent. |

---

*See also: [README](README.md) · [SETUP](SETUP.md) · [SECURITY](SECURITY.md) · [SECURITY-CASESTUDY](SECURITY-CASESTUDY.md)*
