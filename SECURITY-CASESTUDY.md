# Security Case Study — Cooldown

A walkthrough of every weakness found in this setup, why it mattered, and how it
was fixed. Written as a learning reference — each finding is
**what it is → how it bit us → impact → the fix → the concept behind it.**

> This is the public copy. Findings are described in the detail needed to learn from them;
> live addresses, configuration and anything directly exploitable stay in the private
> version-controlled copy.

New to any of the words below? *Nonce*, *same-origin*, *least privilege* and the rest are
all explained in plain English in [**CONCEPTS.md**](CONCEPTS.md).

**Scorecard:** 12 findings fixed & verified live · 4 risks accepted by design · 172 tests green.

Findings F1–F5 came from an initial review of the design. **F6–F12 came later, from
auditing the box that was actually running** — which is where the more serious ones were,
and a reminder that reviewing a design is not the same as reviewing a deployment.

---

## First, the trust model

Everything hinges on knowing where your data is encrypted and the one place it isn't:

```
  Your phone  ──WireGuard (encrypted)──▶  ┌──────── plaintext zone ────────┐  ──real TLS──▶  Reddit/YouTube
                                          │  THE BOX: mitmproxy decrypts   │
                                          │  + injects  (holds the CA key) │
                                          └────────────────────────────────┘
```

- **In transit, phone→box is double-encrypted** (your browser's TLS *inside* the
  WireGuard tunnel), so a network eavesdropper sees nothing.
- **The box is a concentration of trust** — it holds the CA key and is the only
  place your data exists in cleartext. Most findings are about protecting *that
  box*, or the code it runs.

---

## F1 — Proxy exposed to the LAN & public IPv6  ·  HIGH  ·  FIXED

**What it is.** mitmproxy listened on `0.0.0.0` and `[::]` (every interface). The
Pi's `eth0` had a LAN address *and* a globally-routable IPv6, and the firewall was
open — so the proxy ports (`8080`/`8081`) were reachable from other LAN devices and
**potentially from the open internet over IPv6.**

```
             BEFORE (open)              AFTER (firewalled to tailscale0)
  Tailscale device   reaches ✓          Tailscale device   reaches ✓
  LAN Wi-Fi device   reaches ✗ (bad)    LAN Wi-Fi device   DROP ✓
  Internet via IPv6  reaches ✗ (bad)    Internet via IPv6  DROP ✓
```

**Impact.** Anyone reaching a port *and* trusting the CA could be MITM'd; an exposed
MITM proxy on the internet is an abuse vector regardless. This kicked off the review.

**The fix.** Interface-scoped firewall rules (IPv4 + IPv6): accept the proxy ports
only on `tailscale0` and loopback, `DROP` everywhere else. Matching by *interface*
(not IP) covers both proxy modes and both IP families in one rule; re-applied on
boot. Verified live: LAN and public-IPv6 went from OPEN to BLOCKED.

**Concept — attack surface & least exposure.** `0.0.0.0` is leaving every door
unlocked because you use one. Bind to the narrowest interface; default-deny at the
firewall. IPv6 is the classic blind spot — people firewall v4 and forget the public
v6 address.

---

## F2 — Open redirect via parser differential  ·  MEDIUM  ·  FIXED

**What it is.** The "return to the link you clicked" feature carried a return URL,
guarded by a same-site check. **Python's `urlparse` and the browser disagree on how
to read a URL**, so a string can look same-site to the check while the browser goes
elsewhere.

```
  Input:  https://evil.com\@reddit.com/

  Python urlparse (the check)        Browser (the reality)
    userinfo = evil.com\               treats "\" as "/"
    host     = reddit.com   ✓ ALLOW    → https://evil.com/@reddit.com/
                                        host = evil.com  → goes to attacker
```

**How it bit us.** Chained with F3 (CSRF), a malicious page could auto-submit a
request that granted a session *and* bounced you to the attacker's site right after
"Enter" — a primed fake-login setup.

**The fix.** Reject the differential-driving characters (backslashes, whitespace,
control chars) and any credential `@` in the authority *before* the host check.
Tested against a bypass corpus; legit links (incl. YouTube `/@handle`) still pass.

**Concept — parser differentials.** When two components parse the same input
differently, the gap is the bug (same root cause as HTTP request smuggling). Don't
reflect user URLs; validate strictly and rebuild from trusted parts.

---

## F3 — CSRF on state-changing endpoints  ·  MEDIUM  ·  FIXED

**What it is.** `/enter`, `/exit`, `/study`, `/heartbeat` were unauthenticated
POSTs with no anti-forgery check, and CORS was a wildcard (`*`). Any site you
visited could fire requests at your gate.

```
  You visit evil.com ──forged POST /budget/enter──▶ [proxy boundary]
                                                     Sec-Fetch-Site: cross-site → 403 ✕
                                                     (never reaches the gate)
```

**Impact.** Could burn your budget / toggle sessions, and — worse — deliver the F2
redirect. Low on its own (it's your state), but the delivery vehicle for F2.

**The fix.** Drop the wildcard CORS (every endpoint is same-origin), and reject
cross-site POSTs to `/budget/*` at the proxy boundary via the browser's
`Sec-Fetch-Site` header. Verified: cross-site → `403`, same-origin passes.

**Concept — CSRF / confused deputy.** CSRF abuses your browser's trust — it attaches
your context to a request another site triggered. Defenses assert intent: anti-CSRF
tokens, `SameSite` cookies, or `Sec-Fetch-*` metadata.

---

## F4 — Substring host matching  ·  LOW  ·  FIXED

**What it is.** Gating asked `"reddit.com" in host` — a *substring* test. True for
`reddit.com`, but also `evil-reddit.com` and `reddit.com.attacker.io`.

```
  host                     substring "in"      suffix (fixed)
  reddit.com               match ✓             match ✓
  www.reddit.com           match ✓             match ✓
  evil-reddit.com          match ✗ (wrong)     no match ✓
  reddit.com.attacker.io   match ✗ (wrong)     no match ✓
```

**Impact.** A look-alike domain would be decrypted and script-injected by your box —
traffic you never meant to touch. Privacy/correctness more than direct compromise.

**The fix.** Suffix match (`host == d or host.endswith("." + d)`) and anchor the
mitmproxy `--allow-hosts` regexes (`^(.+\.)?reddit\.com$`). Verified real sites
still gate.

**Concept — canonicalization & allow-lists.** Identity checks compare *structure*,
not fuzzy text. "Contains" is almost never right for a domain/path/origin. Anchor
and normalize before comparing.

---

## F5 — Development server in production  ·  LOW  ·  FIXED

**What it is.** The app ran on Flask's Werkzeug **dev** server — fine for a laptop,
not for 24/7 duty; its debug console is an RCE vector if ever enabled.

**Why only Low here.** Loopback-only (unreachable from any network) and `debug=False`
— so no RCE console. A robustness/hygiene issue, not an open door.

**The fix.** Swapped in **waitress** (production WSGI), a two-line change. No
behaviour/speed change at single-user scale.

**Concept — dev tooling never ships to prod.** Debuggers/dev servers trade safety
for convenience, and each convenience is attack surface. The Werkzeug debugger is
the textbook debug-feature-becomes-RCE.

---

## F6 — The web app could become root  ·  HIGH  ·  FIXED

**What it is.** Both the Flask app and mitmproxy ran as the login user, which on a
Raspberry Pi has blanket `NOPASSWD: ALL`. The one process listening to the network held
the power to do anything on the box.

**How it bit us.** Nothing exploited it — but the app is the part exposed to hostile
input, and it was one code-execution bug away from full compromise of a machine holding
the CA that decrypts a household's traffic.

**The fix.** Two dedicated system accounts (`cooldownapp`, `cooldownproxy`): no password,
no login shell, not in `sudo`. The app's entire elevated capability is now a single
fully-qualified read pinned in `sudoers.d`. The proxy has none at all. Verified on the box
that both are refused `iptables -F`, `cat /etc/shadow`, `systemctl restart` and a shell.

**The concept — least privilege.** A process should hold the authority its job needs and
not a scrap more. The two accounts are also isolated from each other: the app cannot read
the proxy's CA key, so compromising the web tier does not yield the crown jewel.

---

## F7 — Unauthenticated GET performed privileged firewall writes  ·  HIGH  ·  FIXED

**What it is.** `/feed` rebuilt an `iptables` accounting chain on every request, so an
unauthenticated GET triggered up to ten `sudo` invocations *including writes*.

**How it bit us.** Anything able to reach that endpoint could drive privileged firewall
operations and contend with the redirect service for the iptables lock. Measured at 101 ms
per request — a cheap denial-of-service against the box's own gateway rules.

**The fix.** The chain is created once at boot by the root-run redirect script. The app
now performs exactly one read and has no write path; `grep` for write verbs in the app
returns zero.

**The concept — separate setup from use, and never mutate on a read.** A `GET` that
changes system state is a design error before it is a security one.

---

## F8 — Content-Security-Policy deleted wholesale  ·  MEDIUM  ·  FIXED

**What it is.** To inject its stopwatch the proxy deleted the site's entire CSP — on
*every* response, not just the HTML it injected into.

**How it bit us.** It threw away protections that had nothing to do with script
injection: `frame-ancestors`, `form-action`, `connect-src`. On a site with an XSS bug, the
exfiltration limits that would have contained it were gone — and gone *only for you*.

**The fix.** Two steps. First, strip only on `text/html`, which is the only thing ever
injected into. Then stop stripping at all: add a per-response **nonce** to `script-src`
and leave the rest of the policy enforced. Verified against live Reddit, whose policy is
`default-src 'none'` — it arrives intact, with Reddit's own nonce and ours side by side.

**The trap.** A browser ignores `'unsafe-inline'` the moment a nonce appears. Adding one
blindly to a policy that relies on it would disable the *site's* inline scripts and break
the page. That case is detected and the header left untouched.

**The concept — minimum necessary modification.** Removing a control because it obstructs
you discards every other guarantee it was making. Amend the one rule in the way.

---

## F9 — Monitoring pages readable by any script on a gated site  ·  MEDIUM  ·  FIXED

**What it is.** `/budget/stats`, `/health`, `/devices`, `/remaining` and `/feed` are served
on the *gated site's* origin. Any script running on that site — a malicious ad, a
compromised third-party include — was same-origin with them.

**How it bit us.** `fetch('/budget/devices?fmt=json')` from a page on Reddit returned
device names, OS versions, tailnet addresses and traffic volumes. Home-network
reconnaissance, handed to a page the user had chosen to decrypt.

**The fix.** Headers alone can't help — our gate page and the site's own pages are the
same origin, so `Sec-Fetch-Site` is identical for both. So: allow a real navigation
(`Sec-Fetch-Dest: document`, a *forbidden header name* scripts cannot forge) **or** a token
only Cooldown's pages carry — and those pages can't be fetched by script either, so the
token can't be harvested. The two conditions reinforce each other. Verified through the
live proxy: scripted fetch 403, navigation 200, token'd poll 200.

**The concept — same-origin is a weak boundary when you *add* pages to someone's origin.**
Injecting your own endpoints into a site you don't control means its scripts inherit access
to them. Prefer a separate origin; if you can't, require something a script cannot produce.

---

## F10 — SSH accepted passwords from the whole LAN  ·  HIGH  ·  FIXED

**What it is.** `sshd` listened on `0.0.0.0:22` with `PasswordAuthentication yes` and no
firewall rule — while the proxy ports right next to it were carefully restricted to the
tailnet.

**How it bit us.** Anyone on the home network — a guest, a compromised IoT device, someone
with the wifi password — could brute-force the account whose box holds the CA. Every other
control in this document is downstream of that.

**The fix.** Key-only via a drop-in (`PasswordAuthentication no`,
`KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`), applied only after
confirming key auth worked, validated with `sshd -t`, reloaded rather than restarted, and
proved from a *new* connection before trusting it.

**The concept — inventory every listener, not just the interesting ones.** The proxy ports
got attention because they were the novel part. `sshd` was old and boring and wide open.

---

## F11 — No automatic patching  ·  MEDIUM  ·  FIXED

**What it is.** An always-on internet-facing box with **18 pending security updates** and
`unattended-upgrades` not installed.

**How it bit us.** The pending set included `gnutls`, `libgcrypt`, `krb5` and `dnsmasq` —
the TLS and DNS libraries this machine's entire job depends on.

**The fix.** Applied (18 → 0) and enabled automatic security upgrades. Auto-reboot
deliberately left **off**: a gateway restarting itself would drop every routed device.

**The concept — appliances rot.** Something you install once and never log into again is
exactly the thing that needs to patch itself.

---

## F12 — Worker starvation silently stopped the clock  ·  LOW  ·  FIXED

**What it is.** `/health` slept 0.2 s inside the request to sample CPU, holding one of only
four server threads; the injected heartbeat swallows its own errors.

**How it bit us.** Saturating the pool made heartbeats fail — and because failures are
silent by design, **time stopped being charged while browsing continued**. A security-ish
failure of the tool's actual purpose, reachable by the same-origin scripts of F9.

**The fix.** CPU is sampled *between* calls instead of by sleeping, `/health` is cached,
and the pool is larger. Measured 340 ms → 39 ms per request.

**The concept — fail loudly, or fail safe.** Silent failure in the enforcement path is
indistinguishable from success.

---

## Accepted by design

Some risks are the cost of what the tool *is* — understood, bounded, documented,
not eliminated. Naming them is itself good practice.

- **Physical access to the box.** The SD card is unencrypted, so whoever holds the
  hardware holds the CA. Full-disk encryption on a headless always-on machine is theatre —
  the key must be readable at boot regardless. The answer is *revocation, not encryption*:
  untrusting the certificate on your devices makes a stolen key inert in about two minutes,
  even against root. Hence `RECOVERY.md`, `rotate-ca.sh`, and a reboot alarm that turns a
  quiet card-copy into a visible event.
- **A weakened CSP on gated sites.** Now minimal rather than total — one nonce added,
  everything else enforced (F8) — but our script does run on those origins by design.
- **The CA key is a single trust anchor.** Whoever holds it can decrypt your
  traffic. It stays on the box, out of git; the mitigation is guarding the box, not
  removing the trust.
- **The VPN-off bypass.** Turning the tunnel off routes around the gate —
  deliberate *soft* friction (a commitment device for a cooperative user), not an
  adversarial lock.
- **Security leans on the firewall.** mitmproxy accepts any source
  (`block_global=false`), so F1's firewall is what contains it. A noted dependency.
- **Dependency ranges instead of pins.** `mitmproxy` and `flask` are ranges so the project
  installs on both current Raspberry Pi OS releases. That is a deliberate trade of a
  slightly wider supply-chain surface for not locking out most users.

---

## What to carry forward

- **Never trust the client for a security decision.** Validate where you control it
  — the box, not the browser. (Same reason changing your phone's clock can't skip
  the cooldown: the Pi is the authority.)
- **Defense in depth.** WireGuard *and* TLS; firewall *and* bind rules; filter *and*
  mangle QUIC blocks. Each layer backstops the next.
- **Minimize and default-deny.** Narrowest interface, tightest allow-list,
  drop-by-default. Every open port/wildcard/"contains" is surface you didn't need.
- **Mind the parser gap.** When two components read input differently, the
  disagreement is the bug. Validate strictly; rebuild from trusted parts.
- **Name your trust anchors and your accepted risks.** Know the one thing whose
  compromise unravels everything (the CA key / the box), protect it hardest, and
  write down what you chose *not* to fix and why.
- **Review the deployment, not just the design.** F1–F5 came from reading the code.
  F6–F12 came from logging into the running box — and included the two most serious
  findings. A threat model is a hypothesis; the machine is the evidence.
- **Verify the boundary, don't assert it.** Every fix here was checked against the thing
  itself: a new SSH connection before trusting the old one, a live fetch returning 403, a
  mutation test proving a new test actually fails when the protection is removed. A test
  that cannot fail is decoration.
