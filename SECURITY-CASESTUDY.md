# Security Case Study — Cooldown

A walkthrough of every weakness found in this setup, why it mattered, and how it
was fixed. Written as a learning reference — each finding is
**what it is → how it bit us → impact → the fix → the concept behind it.**

> version-controlled copy.

**Scorecard:** 5 findings fixed & verified live · 4 risks accepted by design · 56 tests green.

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

## Accepted by design

Some risks are the cost of what the tool *is* — understood, bounded, documented,
not eliminated. Naming them is itself good practice.

- **CSP stripping.** Injection requires deleting the site's Content-Security-Policy,
  so while proxied those sites lose one XSS-mitigation layer. Scoped to gated sites,
  only for you. (A surgical alternative: rewrite the CSP with a nonce instead of
  deleting it.)
- **The CA key is a single trust anchor.** Whoever holds it can decrypt your
  traffic. It stays on the box, out of git; the mitigation is guarding the box, not
  removing the trust.
- **The VPN-off bypass.** Turning the tunnel off routes around the gate —
  deliberate *soft* friction (a commitment device for a cooperative user), not an
  adversarial lock.
- **Security leans on the firewall.** mitmproxy accepts any source
  (`block_global=false`), so F1's firewall is what contains it. A noted dependency.

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
