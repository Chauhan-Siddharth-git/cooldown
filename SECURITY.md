# Security — please read this before you install anything

New to the jargon? Every term here is explained in plain English in
[**CONCEPTS.md**](CONCEPTS.md).

---

## The short version

Cooldown works by **reading your own internet traffic** as it passes through a little
computer in your home. To do that, you have to give your phone and laptop a permission
they normally never give anything: *"trust this box completely."*

That is a real thing to hand over. It's fine when the box is yours and nobody else can
touch it — that's the entire design. It is **not** fine if you take shortcuts. This page
explains, without jargon, exactly what could go wrong.

**Three sentences:**

1. You will create a special file on your box called a **certificate authority** — think
   of it as a **master key**.
2. Your phone and laptop will be told to trust anything that master key signs, which is
   what lets Cooldown see (and block) the sites you're trying to cut down on.
3. Whoever holds that master key can read **all** the web traffic from those devices —
   so the whole game is: *make it, keep it on your own box, never let it out, and never
   install someone else's.*

---

## What you're actually agreeing to (the mail analogy)

Imagine all your internet traffic is **sealed envelopes**. Normally nobody between you
and the website can open them — not your internet provider, not the coffee shop wifi.
That sealing is what the little padlock in your browser means.

Cooldown needs to open those envelopes. If it couldn't, it would have no idea whether
you were opening Reddit or your bank, and it couldn't show you the "time's up" page.

So you do this: you make a **master key** on your box, and you tell your phone *"letters
sealed with this key are legitimate — accept them."* From then on your phone sends its
mail through your box, which opens each envelope, reads the address, reseals it with your
master key, and passes it along. Your phone accepts the reseal because you told it to.

That works beautifully **and** it makes one new thing true:

> **Anyone holding that master key can read the mail from every device that trusts it —
> not just Reddit and YouTube. Everything. Your bank, your email, your messages.**

Cooldown only *looks* at the handful of sites you list. But the *capability* is total.
That's why the rest of this page exists.

---

## The three things that can actually go wrong

### 1. Someone steals the master key off your box

**What it would mean:** they could read the internet traffic of every device you set up —
banking, email, everything — for as long as that key stays trusted.

**How likely:** low, if the box stays in your house and isn't exposed to the internet.
This is the risk you accept in exchange for the tool.

**What protects you:**
- The key lives only on your box, readable by one locked-down account and nothing else.
- It is never uploaded, never emailed, never committed to Git (the `.gitignore` here
  blocks it, and **this repository ships no key of its own**).
- If you ever suspect it leaked: **make a new one and stop trusting the old one.** Run
  `./rotate-ca.sh` on the box, then reinstall on your devices. Ten minutes of annoyance,
  problem gone. Full checklist — including lost/stolen/retired — in
  [RECOVERY.md](RECOVERY.md).

### 2. You install *somebody else's* master key ⚠️ the big one

**What it would mean:** you'd hand a stranger the ability to read all your traffic,
permanently, and you'd probably never notice.

This is the one that actually gets people. Someone shares a "quick setup," a ready-made
certificate file, a link that says "just install this to get started." **Don't.**

> **Never install a certificate you did not personally generate on your own box.**
> Not from this repo, not from a release page, not from a forum, not from a friend, not
> from an AI assistant. There is no legitimate reason for anyone to hand you one.

Your box makes its own the first time it runs. That is the only one you should ever trust.

### 3. Other people can reach your box

**What it would mean:** if a stranger — someone on your wifi, or anyone on the internet
if the box is exposed — can send traffic through your box *and* their device trusts your
key, their traffic gets opened too.

**What protects you:**
- Devices reach the box over a **private network** (Tailscale), not the open internet.
- The setup script adds firewall rules so the proxy ports answer only on that private
  network and are **refused everywhere else** — including from other devices on your own
  home wifi.
- Check any time with `sudo iptables -S INPUT`: the accepts for the Tailscale interface
  and loopback should sit above a catch-all `DROP`.

Realistically this only bites if you deliberately open the box to the internet, or install
your key on someone else's device (see risk 2).

---

## What Cooldown is *not*

- **Not a security product.** It doesn't make you safer. It reads your traffic — that's a
  trade you make to gain a self-control tool.
- **Not something to host for other people.** The model assumes you own both the box and
  the devices. Never install your key on someone else's phone.
- **Not unbreakable.** Turning off the VPN bypasses it completely, on purpose — see the
  end of this page.
- **Not private if the box isn't yours.** Don't run this on a work-managed machine or a
  computer someone else administers.

---

## An honest gut check: should you do this?

| Go ahead if… | Please don't if… |
|---|---|
| The box is yours and lives in your home | It's a work machine, or someone else administers it |
| You're setting up **your own** phone/laptop | You're setting up someone else's device |
| You're comfortable following terminal instructions | You'd be pasting commands you don't understand |
| You accept trading privacy-from-your-own-box for self-control | You expected this to make you *more* secure |

If anything in the right-hand column applies, use a normal screen-time app instead.
Genuinely — that's the honest recommendation, no hard feelings.

---

## The technical details

Everything above, stated precisely.

**About stripping CSP.** To run its stopwatch on a page, Cooldown removes that page's
`Content-Security-Policy`, which is the rule telling the browser not to run outside
scripts. Worth being precise about the cost:

- It only happens on **HTML documents of the sites you gate** — not on their JSON/JS/CSS,
  and never on any site outside the allow-list. Your bank and email keep their CSP.
- **Only CSP is removed.** HSTS, `X-Frame-Options`, `X-Content-Type-Options` and
  `Referrer-Policy` pass through untouched, and cookie flags (`HttpOnly`, `SameSite`)
  are unaffected.
- CSP is a *mitigation*, not a fix: removing it doesn't create a hole, it removes a net
  that would have caught one. The real exposure is that **if a gated site ships an XSS
  bug, it is more exploitable for you than for other visitors** — the injected script
  runs, and CSP's `connect-src` is no longer limiting where it can send data. Scope is
  that one origin.
- Your largest surface is the **news list** — dozens of domains, many with far weaker
  security than Reddit or YouTube. Keep it as short as you'll actually use.
- **The policy is amended, not deleted.** Cooldown adds a one-time `nonce` to
  `script-src` and leaves everything else in place, so `default-src`, `frame-ancestors`,
  `connect-src` and `form-action` all stay enforced. On live Reddit, whose policy is
  `default-src 'none'`, the page arrives with that intact and both nonces present —
  Reddit's own and ours.
- One case is deliberately left alone: if a policy allows `'unsafe-inline'` with no nonce
  or hash, adding a nonce would switch `'unsafe-inline'` **off** and break the site's own
  inline scripts. Our script is already permitted there, so the header is untouched.
- `Content-Security-Policy-Report-Only` is still dropped. It blocks nothing, but it would
  post a violation report about our injected script back to the site.

**What it does to your traffic.** Traffic routes through the box, where **mitmproxy**
terminates TLS, injects a script, and can serve a gate page in place of a budgeted site.
To do that it generates a **root Certificate Authority**, and you install that CA as
*trusted* on your devices. A device that trusts a CA accepts **any** certificate that CA
signs, so the holder of the CA's private key can transparently decrypt and modify HTTPS
from that device — for any site, not only the gated ones.

**The rules that follow.**

1. **Generate your own CA; never install one you didn't generate.** mitmproxy creates one
   on first run (in the proxy's `confdir`). This repo ships **no** CA material, deliberately.
2. **The private key never leaves the box and never enters Git.** The `.gitignore` blocks
   `*.pem`, `*.key`, `certs/`, `.mitmproxy/`. If exposed, regenerate and untrust the old one.
3. **Gate your own devices only.** Cooldown is not a hosted service.

**The monitoring pages are not readable by page scripts.** `/budget/stats`, `/health`,
`/devices`, `/remaining` and `/feed` are served on the *gated site's* origin, which means
any script on that site — including a malicious ad — is same-origin with them and could
otherwise read your device names, tailnet addresses and usage history. They now require
either a real navigation (`Sec-Fetch-Dest: document`; these are forbidden header names,
so a script cannot forge them) or a token that only Cooldown's own pages carry, which a
foreign script cannot obtain because the pages holding it can't be fetched by script
either. The gate itself and `/heartbeat` stay open — the gate has to render in place of a
site, and the injected heartbeat legitimately runs on the site's own pages.

**Network exposure.**

- Devices reach the box over **Tailscale** (a private WireGuard mesh), so the proxy isn't
  exposed to the public internet.
- mitmproxy binds `0.0.0.0`/`[::]`, so `deploy/cooldown-redirect.sh` firewalls the proxy
  ports: interface-scoped `INPUT` rules (v4 + v6) accept `8080/8081` only on the Tailscale
  interface and loopback, with a catch-all **DROP** everywhere else. Without this the proxy
  is reachable from your LAN — and from the internet if the box has a routable IPv6. The
  rules are re-applied at boot by `cooldown-redirect.service`.
- QUIC (UDP/443) is blocked so clients fall back to interceptable TCP.

**Privileges: what runs as root, and what each part may do.**

- **`cooldown-redirect.service` runs as root** — it must, since it manages `iptables`. At
  boot it installs the transparent-redirect rules, the QUIC block, the proxy-port firewall,
  and a **`TRAFFIC_ACCT`** counting chain in the `mangle` table.
- **`TRAFFIC_ACCT` is observational only.** Four rules with **no target**: packets are
  counted and fall straight through, so it cannot drop, alter, or reroute anything. It
  exists so the UI can distinguish encrypted traffic (`:443`) from plaintext (`:80`, `:53`).
  Inspect with `sudo iptables -t mangle -nvxL TRAFFIC_ACCT`; remove with
  `sudo /usr/local/sbin/cooldown-redirect.sh down`.
- **The web app never writes to the firewall.** Its single privileged action is one read —
  `sudo -n iptables -t mangle -nvxL TRAFFIC_ACCT` — and `deploy/sudoers.d-cooldown` pins
  sudo to exactly that fully-qualified command. Install with
  `sudo install -m 440 -o root -g root deploy/sudoers.d-cooldown /etc/sudoers.d/cooldown`
  and validate with `sudo visudo -c` before logging out.
- If that read isn't permitted (Docker, non-Linux, or you skipped it) the app degrades
  quietly: counters read zero, the background shows less red, nothing breaks.
- **Both services run as their own unprivileged accounts.** `cooldown-app.service` uses
  `User=cooldownapp`, `cooldown-proxy.service` uses `User=cooldownproxy` — system accounts
  with no password, no login shell, no `sudo` group membership. The app's entire elevated
  capability is that one firewall read; the proxy has none at all. They're isolated from
  each other too: the app account cannot read the proxy's CA private key. Run either as an
  account with blanket `NOPASSWD: ALL` (a stock `pi` account, say) and any flaw in it is
  root — the scoped rule documents the requirement but cannot remove that power.
- **Relocating the CA is the step to get right.** Copy with `cp -a` (preserving the `600`
  key modes) and confirm the fingerprint is unchanged:
  `openssl x509 -in <confdir>/mitmproxy-ca-cert.pem -noout -fingerprint -sha256`. If
  mitmproxy can't read the CA it silently **generates a new one**, and every device with
  the old CA installed starts failing TLS until you install the new one.

**Data.** All state (time spent, cooldowns, usage history) lives in a local Redis on your
box. There is **no telemetry**; nothing leaves your machine. Usage history is per-day,
per-site charged seconds.

---

## The bypass is intentional

Enforcement relies on your device routing through the box. **Turn the VPN off and the gate
is gone.** That's deliberate: Cooldown is a commitment device for a cooperative user (you,
versus your own impulses), not an adversarial lockdown. It's soft friction — enough to
break the trance, not enough to make you rip the whole thing out. Don't rely on it to
restrain someone motivated to defeat it; that was never the threat model.

## Reporting

Found a real security issue in the code? Open an issue describing the impact (omit live
exploit details if that puts users at risk), or contact the maintainer directly.
