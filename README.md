# Cooldown

### Doomscroll killer

**A self-hosted anti-doomscroll gateway. It budgets your *foreground time* on the
sites that eat your attention, then forces a cooldown — a pause to break the
trance, not a wall that makes you rip the whole thing out.**

Cooldown runs on a small box you own (a Raspberry Pi is the reference target). Your
phone and laptop route through it, and for the sites you choose — Reddit, YouTube,
etc. — it meters the minutes you actually *look at the screen* and, when the budget
is spent, shows a calm "Countdown" page instead of the feed.

> ⚠️ **Cooldown decrypts your own HTTPS to do this. Read [SECURITY.md](SECURITY.md)
> before you run it.** You generate your own CA; this repo ships none.

---

## Why it's different

Most screen-time tools **block** (on/off) or enforce a **daily cap**. Cooldown's bet
is that *total* time isn't the enemy — the unbroken 45-minute binge is. So instead
of blocking, it:

- **Charges foreground time only.** An injected, visibility-gated heartbeat means
  time counts *while you're looking*. Background tabs and locked screens are free.
- **Forces a cooldown after a session,** to break the scroll trance — then lets you
  back in. The pause is the point.
- **Does surgery, not just blocking.** On YouTube it strips Shorts, the home feed,
  and autoplay while leaving Search and Subscriptions — so the tool removes the
  slot machine without removing the utility.
- **Has a Study mode:** a free, always-open escape hatch locked to an
  allow-listed course playlist.
- **Winds down at night:** a soft bedtime curfew (with a small, independent night
  buffer) instead of a hard shutoff.
- **Is yours.** No subscription, no account, no telemetry. All state is local.

It's closest in spirit to running your own [openpilot](https://github.com/commaai/openpilot):
network-level power and full control, for people who are happy to self-host.

## Who this is for (and who it isn't)

**For you if:** you self-host, you're comfortable with a Raspberry Pi + Tailscale +
installing a CA on your phone, and you want a *time-budget-with-cooldown* model you
fully control.

**Not for you if:** you want a tap-to-install App Store product. For that, look at
Brick (physical tag), Opal/Jomo (Screen Time apps), or one sec (friction pause).
Cooldown trades their easy setup for control, transparency, and the specific cooldown
philosophy — and it asks you to trust a CA you generate. That's a real ask; take it
seriously.

## How it works

> **New here?** [**ARCHITECTURE.md**](ARCHITECTURE.md) is a layered field guide
> (plain-English → under-the-hood, with a glossary) that walks the whole system
> from a single tap to the countdown. The security review of this design lives in
> [**SECURITY-CASESTUDY.md**](SECURITY-CASESTUDY.md).

```
 iPhone / laptop
     │  routes through the box (Tailscale exit node; browser proxy on desktop)
     ▼
 Your box (Raspberry Pi, native venv + systemd)
   ├─ iptables redirect  :80/:443 → mitmproxy, QUIC (UDP/443) blocked
   ├─ mitmproxy (addon.py)   decrypt · strip CSP · inject heartbeat · serve the gate
   ├─ Flask (app.py)         budget logic + gate/stats pages + /heartbeat /enter
   └─ Redis                  state: spent, cooldown, sessions, usage history
```

- **`app.py`** — the brain: the time state machine (shared bucket, per-site caps,
  passive refill, cooldown, day/wind-down/night phases) and all the pages.
- **`addon.py`** — the mitmproxy addon: interception, CSP stripping, heartbeat +
  YouTube-declutter injection, and serving the gate in place of a gated site.
- **`deploy/`** — the systemd units and the iptables redirect script, as run on the
  reference Pi.

Only browser traffic is gated — native apps pin certificates and can't be
intercepted (by design; the answer there is "use the mobile site"). See the
architecture notes for the full picture.

## Quick start

Full walkthrough in **[SETUP.md](SETUP.md)**. The shape of it:

1. Flash a Raspberry Pi, install Redis + Python.
2. `python -m venv venv && venv/bin/pip install -r requirements.txt`
3. Let mitmproxy generate its CA, then **install that CA with full trust** on your
   phone (the step everyone misses — without it, HTTPS silently fails).
4. Put the box on Tailscale and select it as your exit node.
5. Install the `deploy/` systemd units + the iptables redirect.
6. Edit the site list / budgets at the top of `app.py` to taste.

## Configure

The knobs live at the top of `app.py` — budgets, cooldown length, refill rate,
night-curfew hours, the gated `SITES` map, and `STUDY_PLAYLISTS`. Adding a site
touches **three** places: `SITES` in `app.py`, `SITES` in `addon.py`, and the
`--allow-hosts` regex in `deploy/cooldown-proxy.service` (the TLS-decrypt allowlist —
miss it and the site is tunneled un-gated).

## Tests

```
python -m pytest tests/
```

Pins the whole time state machine — phases, refill grace, cooldown lifecycle,
heartbeat charging/blocking per phase, night buffer, usage history, and every gate
state. Run before touching the budget constants.

## Status & limitations

Works, and runs daily on the author's setup — but it's a personal project, not a
polished product:

- **Browser-only** (native apps pin certs).
- **The YouTube declutter tracks YouTube's markup** and will need updates when they
  change it.
- **The bypass is intentional** — turning the VPN off routes around the gate. Soft
  friction, by design.
- The `--allow-hosts` allowlist and a few config values are duplicated across
  files (see above); consolidating them is a good first contribution.

## License

MIT — see [LICENSE](LICENSE).
