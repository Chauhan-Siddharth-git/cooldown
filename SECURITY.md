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

## Compared with not doing this at all

Most of this page describes how Cooldown works. This section is the one that matters
most: **what is different about your life afterwards.**

| | Without Cooldown | With Cooldown |
|---|---|---|
| **Who can read your browsing** | You and the websites. Nobody in between — not your internet provider, not the café wifi. | The same, **plus** anyone who gets the master key off your box. |
| **If someone breaks into your home wifi** | They see scrambled noise. Modern encryption holds. | Same — **unless** they also get the key. Then they see everything in plain text. |
| **If hardware is physically stolen** | Stealing your router gets someone almost nothing. | The Pi's memory card holds a working key to your traffic, and it isn't encrypted. Whoever holds the card holds the key — for years. |
| **Code sitting in the path of your traffic** | Your operating system and browser. Written by large teams, patched constantly, attacked and fixed by thousands of people. | All of that, **plus roughly 3,700 lines written by one person in their spare time.** |
| **If that code has a bug** | Not applicable. | A website you visit might be able to reach further than it should — into the box, or your home network. |
| **Who else is affected** | Just you. | Every device you installed the certificate on. Including anyone else's. |
| **Would you notice a break-in?** | Often, eventually. Stolen passwords get used, accounts do odd things. | **Probably not.** A copied key leaves no trace and keeps working silently. |
| **Turning it off** | Not applicable. | Two taps. Switch the VPN off and you're unprotected *and* ungated. That's deliberate. |

**The honest summary:** you are trading a real, permanent increase in risk for a
behavioural benefit. That can be a perfectly sensible trade — people accept comparable
trades for convenience all the time — but it is a trade, and it is not reversible by
wishing. It is reversible by *removing the certificate*, which takes about two minutes
and is the answer to almost everything below.

### Has the "bug in the code" risk ever actually happened?

Yes. Being straight with you about this is more useful than a reassurance.

Reviews of this project have found **twenty issues, five of them rated serious.** The
worst for an ordinary user: a specially-crafted link could have made the box fetch an
attacker's page and hand it to your browser *as though it came from Reddit* — enough to
attack your Reddit account, and to poke at devices on your home network. Others included
the proxy being reachable from the whole home wifi, and the box accepting SSH passwords
from any device on the network. All are fixed; the whole story, including the ones that
took three attempts, is in [SECURITY-CASESTUDY.md](SECURITY-CASESTUDY.md).

Take two things from that. First: **this is the category of risk you are accepting** —
not a hypothetical. Second: it was found because somebody went looking, the fix is tested,
and the tests now fail automatically if anyone reintroduces that class of mistake. Software
in the path of your traffic has bugs. The question is whether anyone is looking.

---

## The risk that only exists because of this: code running inside other websites

This one has no equivalent in a normal setup, so it's worth its own section.

**The background.** Websites are constantly attacked by people trying to sneak their own
JavaScript onto the page — a *cross-site scripting* attack, or XSS. It's the most common
serious web bug there is, and it's nasty because JavaScript running on a page can do
anything you could: read what's on screen, read the cookie that keeps you logged in, fill
in forms, click buttons, make requests **as you**. Browsers and websites spend enormous
effort making sure only the site's own code runs on the site's own pages.

**What Cooldown does.** It puts its own JavaScript on Reddit, YouTube, Spotify, Facebook
and about ninety news sites. That's the timer — it's how "foreground time only" works.

There's no way to soften this: **Cooldown does the exact thing XSS defences exist to
prevent.** It is a benign version, written by you, running on your own devices — but the
mechanism is identical, and it creates three things a normal person doesn't have.

### 1. A compromised box can *act as you*, not just watch you

This is the biggest one, and it's easy to miss.

If someone taps your home network without Cooldown, the worst case is **eavesdropping**,
and modern encryption mostly prevents even that. If someone gets into your Cooldown box,
they don't just read your traffic — they control what gets injected into every gated site.
That means they can run their own JavaScript on Reddit *as you*: read your session, post,
message, change your email, start a password reset.

> The difference is between **a wiretap** and **someone sitting at your keyboard.** Most
> people's mental model of "they can read my traffic" is the first. This setup makes the
> second possible too.

Everything else on this page — guard the key, guard the box, revoke fast — matters more
because of this, not less.

### 2. Sites' own defences are slightly weakened, on purpose

Websites can send a rule saying *"only run scripts I have vouched for"* (a Content
Security Policy). Cooldown's timer isn't vouched for by Reddit, so it would be blocked.

Earlier versions solved that by **deleting the rule entirely**, which switched off the
site's protection for the whole page. That was wrong and it's fixed: the rule is now kept
and *amended* — one single-use random pass added, for our script only, and only on the
handful of pages Cooldown actually injects into. Everything else the site asked for is
still enforced.

Honest residue, because "we amend rather than delete" isn't a clean bill of health:

- The pass is a random value printed in that page's HTML. If the site *already* has a
  bug that lets an attacker read or reflect page content, they could copy the pass and
  use it — turning a bug that wouldn't have run any code into one that does. Narrow, but
  real, and it doesn't exist without Cooldown.
- If a site's policy already allowed inline scripts, nothing is touched at all.
- Cooldown discards the "report violations to me" variant of the rule, so the site loses
  a little telemetry. That costs the site, not you.

### 3. A handful of Cooldown's own endpoints sit on those sites' origins

The gate page and the session endpoints (`/budget`, `/enter`, `/heartbeat` and a few
more — eight in total) have to live on the gated site's address, because the gate must
appear *in place of* Reddit. That means any script already running on Reddit — including
an advertisement — can reach them.

What that buys an attacker is small: they could end your session or burn budget, which is
annoying rather than dangerous. What it *used* to buy them was your device names, home
network addresses and usage history, because the dashboard lived there too. Those pages
have been moved to the box's own address, where a script on Reddit is simply not allowed
to read them — the browser enforces that, rather than Cooldown asking nicely.

### What keeps this from being worse

- **Nothing attacker-controlled is ever injected.** What gets added to a page is fixed
  text plus the site's own name; no part of it comes from the URL, the page or the network.
- **Everything Cooldown displays is escaped.** A device on your network called
  `<script>…</script>` shows up as those literal characters, not as code.
- **Only pages get touched.** Images, JSON and scripts are passed through untouched.
- **Bugs of exactly this shape have been found and fixed here** — one where a crafted link
  made the box serve an attacker's page as though it came from Reddit, which is textbook
  XSS. It's covered in the case study, and there are now automated checks that fail if
  anyone reintroduces that class.

**If this section is the one that worries you, that's a reasonable reaction** — it's the
part of the design with no safe equivalent, and it's the strongest argument for using your
phone's built-in Screen Time instead.

---

## The other side of the ledger

Everything above is what can go wrong, because that's what a page like this is for. But a
list of risks with nothing beside it isn't a decision — it's just a fright. Here is the
rest of the ledger, and none of it is reassurance: every item is something you can check
yourself in this repository or on your own box.

**The risks are almost all conditional on things you control.** Read the list again and
notice the shape of it: don't install a certificate somebody hands you, don't put yours on
other people's devices, don't expose the box to the internet, wipe the card before you
retire it. Those are decisions, not exposures. Compare that with a cloud service, where
the equivalent risk is "their infrastructure is sound and their staff are honest" — true
or not, you cannot check it and you cannot change it. Here the dangerous paths are ones
you walk into deliberately.

**Undoing it is genuinely fast, and it works even against someone holding the hardware.**
Most security problems can't be undone. This one can: remove the certificate from your
devices and the key is inert — instantly, permanently, even if a thief has the memory card
in their hand. Two minutes, no cleverness required.
[RECOVERY.md](RECOVERY.md) is the checklist. That escape hatch is why "the SD card isn't
encrypted" is an acceptable design rather than a fatal one.

**Nothing leaves the box, and there is no company in the picture.** No account, no cloud,
no telemetry, no analytics, no subscription, no privacy policy that can change next year.
Your usage history and your reflections sit in a database on hardware you own. The
comparable commercial tools cannot make that claim, and several of them use the very same
interception mechanism with their key instead of yours.

**It has been looked at, and the results are published.** Twenty findings, all fixed, all
written up in detail — including the two that took more than one attempt and the one where
the fix's own reasoning turned out to be wrong. A project that publishes its own worst
moments is one you can calibrate. There are now **437 automated tests**, including ones
that fail if a whole *class* of past mistake is reintroduced, plus a pre-commit hook that
blocks the specific patterns this codebase has got wrong before.

**The box is not a soft target.** The programs run as locked-down accounts with no
password and no login shell. Between them they hold exactly **one** elevated permission —
reading a network counter — so a bug in the web app cannot become control of the machine.
The proxy ports answer only on your private network and are refused from your own home
wifi and from the public internet. Security updates install themselves.

**You can read all of it.** Not as a slogan — the whole thing is about 3,700 lines, the
security-relevant parts are heavily commented with *why*, and the case study explains the
reasoning behind every control. If you don't want to read code, that's fine, but the
option is real and it is the one thing no closed alternative offers.

**And the thing you're actually trading is narrower than it first sounds.** You are not
giving up "security". You are giving up *privacy from a machine in your own home*, in
exchange for a tool that makes you put your phone down. Whether that's worth it is a
personal call — but it is a much smaller giveaway than the mail analogy makes it feel, as
long as the box stays yours and stays patched.

---

## "Do I even need this?" — the three options

Cooldown is not the only way to spend less time on Reddit, and it is the most invasive.

| | What it costs you | What you get |
|---|---|---|
| **Built-in** — iOS Screen Time, Android Digital Wellbeing | **Nothing.** No key, no certificate, nobody reading anything. | Blunt: block a whole app, or a daily cap. Easy to tap "ignore for today" and forget. |
| **A commercial screen-time app** | Varies, and worth checking. Many route your traffic through a VPN profile on your phone; **some also install their own certificate** — the same mechanism as Cooldown, except the key belongs to a company, sits on their infrastructure, and is governed by a privacy policy you can't audit. | Usually polished, usually a subscription. Someone else maintains it. |
| **Cooldown** | You read your own traffic. The key is yours, stays on hardware you own, and nothing leaves the box — but see the whole of this page. | Foreground-time budgeting, cooldowns that escalate on binges, a wind-down before bed. Every line inspectable. |

If the built-in tools would work for you, **use the built-in tools.** They are strictly
safer. Cooldown earns its keep only if you've genuinely tried the simple thing and the
blunt on/off model didn't hold.

If you'd otherwise install a commercial app that intercepts traffic, the comparison is
narrower than it looks: same mechanism, different key-holder. The question becomes *who
would you rather trust with it* — a company, or yourself plus the state of your home
security.

---

## What could actually happen to you

Plain scenarios, roughly in order of how likely they are.

**Your house is burgled, or you sell/recycle the Pi without wiping it.**
Whoever ends up with the memory card can read the traffic of every device still trusting
the key. They don't need to be a hacker; the card is not encrypted. *What to do:* the
two-minute checklist in [RECOVERY.md](RECOVERY.md) — remove the certificate from your
devices and the key becomes worthless immediately, even to someone holding the hardware.

**Somebody hands you a certificate and a "quick setup."**
The single worst thing you can do, covered in full below. There is never a good reason for
anyone to give you one.

**You put the certificate on a partner's or child's phone.**
Now you can read *their* messages, banking and browsing — whether or not you ever intend
to. And if the arrangement ends, their phone keeps trusting your box until somebody
actively removes it. Don't do this. If you already have: remove it from their device.

**You take the laptop to work with the tunnel on.**
Your employer's traffic now flows through a personal device in your home. Depending on
where you work, that ranges from awkward to a fireable policy breach. Turn the exit node
off before connecting to a network that isn't yours.

**A bug in the software.** See above — this has happened once, publicly. The mitigation is
that the project is small, readable, tested, and reviewed rather than trusted.

**The box quietly rots.**
It's a computer that runs unattended for years. Unpatched software is how boring machines
become someone else's. The installer offers to switch on automatic security updates — say
yes, and leave it on. Upgrades install at 03:00 and, if a kernel landed, the box reboots at
04:00. That reboot used to be off, on the grounds that a gateway restarting drops every
routed device; at 4am the only routed device is a phone nobody is holding, and a kernel
patch that never takes effect is not a patch.

Switching it on is not the same as it working, which this project learned the hard way
(F11, F21): the setting was enabled for weeks while the box installed nothing, because the
package origins excluded everything Raspberry Pi ships, and then the job queue jammed. So
the box now checks its own patching hourly and puts the answer on `/health` — including
"unknown", which is not the same as "fine".

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
- **A read of the key is now noticed.** `cooldown-cawatch` watches the CA directory and pushes an
  alert the moment the private half is opened outside the proxy's own startup. Its limits
  are worth knowing: it reports *that* a read happened and never *who* (inotify events
  carry no process identity), and copying the SD card while the box is powered off
  produces no event at all. Physical possession still beats it.
- The audit pins the CA's fingerprint hourly, but note what that does and does not catch:
  it detects **modification**, and a thief copies the key without changing it. The
  fingerprint check stays green through a theft. That gap is why the read watcher exists.
- If you ever suspect it leaked: **make a new one and stop trusting the old one.** Run
  `./rotate-ca.sh` on the box, then reinstall on your devices. Ten minutes of annoyance,
  problem gone. Full checklist — including lost/stolen/retired — in
  [RECOVERY.md](RECOVERY.md).
- **Before you can revoke, you need to know what trusts it.** There is no CRL and no
  OCSP for a privately-trusted root: nothing phones home to ask whether it is still
  valid, so revocation means removing it from every trust store by hand. Revocation is
  therefore exactly as complete as your inventory is. `tools/ca-trust-scan.sh` takes that
  inventory; [CA-TRUST.md](CA-TRUST.md) holds the removal runbook per platform. The first
  run of that scan on one laptop found the CA trusted in **five** places — the system
  bundle plus three Firefox profiles, one of them twice — because Firefox keeps its own
  store and people think in devices rather than stores.

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
- The setup script adds firewall rules so the proxy ports **and SSH** answer only on that
  private network and are **refused everywhere else** — including from other devices on
  your own home wifi. (SSH was missing from that list until 2026-08-11 while the dashboard
  claimed otherwise — see F22. Key-only authentication is what carried the risk meanwhile.)
- Know the shape of that rule: it is an allowlist of named ports over a default-ACCEPT
  policy, so it protects the ports someone remembered to list. A new service that starts
  listening is **not** covered automatically, and if your connection has native IPv6 then
  "only reachable on the LAN" is not the fallback you might assume — see F23.
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

**About the Content-Security-Policy.** A site's CSP is the rule telling your browser which
scripts it is allowed to run. Cooldown has to run one of its own, so it has to appear in
that rule. Worth being precise about how, and what it costs:

- **The policy is amended, not deleted.** Cooldown adds a one-time `nonce` to
  `script-src` and leaves everything else in place, so `default-src`, `frame-ancestors`,
  `connect-src` and `form-action` all stay enforced. On live Reddit, whose policy is
  `default-src 'none'`, the page arrives with that intact and both nonces present —
  Reddit's own and ours.
- **`'strict-dynamic'` is handled explicitly**, and the absence of that was a silent
  fail-open until 2026-08-14. Under CSP3 a source list containing `'strict-dynamic'`
  causes `'unsafe-inline'` to be ignored *for scripts* — the spec's own algorithm returns
  "Does Not Allow" before it ever reaches the `'unsafe-inline'` branch. Cooldown used to
  read `script-src 'unsafe-inline' 'strict-dynamic'` as "our script is already permitted"
  and leave the policy alone, which would have blocked the heartbeat with nothing
  reporting it — and no heartbeat means no time charged while browsing continues. It now
  adds a nonce in that case, which is precisely what the spec still honours.
- Earlier versions deleted the header outright. That was the blunt version of the same
  idea, and it threw away protections that had nothing to do with script injection —
  written up as F8 in [SECURITY-CASESTUDY.md](SECURITY-CASESTUDY.md).
- It only touches **HTML documents of the sites you gate** — not their JSON/JS/CSS, and
  never a site outside the allow-list. Your bank and email are not modified at all.
- **Nothing else in the response is changed.** HSTS, `X-Frame-Options`,
  `X-Content-Type-Options` and `Referrer-Policy` pass through untouched, and cookie flags
  (`HttpOnly`, `SameSite`) are unaffected.
- **The residual risk, honestly.** The nonce is fresh per response and random, so it
  doesn't hand an attacker a free pass — lifting it out of the DOM is exactly as hard as
  lifting the site's own. What genuinely changes is that our script runs with the site's
  privileges on that origin: a bug in *our* injected code would be a bug on Reddit's
  origin, for you. Far smaller than deleting the policy, but not zero — and the reason the
  injected script stays as small as it is.
- Your largest surface is the **news list** — dozens of domains, many with far weaker
  security than Reddit or YouTube. Keep it as short as you'll actually use.
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

**The monitoring pages live on the box, not inside the sites you gate.** `/stats`,
`/health` and `/devices` show your device names, tailnet addresses and usage history, so
where they are served matters more than what guards them. They used to sit on the *gated
site's* origin — meaning every script on that site, including any ad it loads, was
same-origin with them and only a header check away from reading them. Two rounds of header
checks narrowed that and never closed it.

They now live on **the box's own address** (`http://<your-box>:5000`), reachable over
Tailscale only, never through the proxy. A script on Reddit is now *cross-origin* with
them, so the browser refuses to hand it a response and refuses to let it read the window —
the same rule that stops a random tab reading your email. The gate page links to them
directly, so you never type the address.

What stays on the gated site's origin is only what has to be: the gate itself (it renders
in place of the site), the session endpoints behind it, and `/feed` — two aggregate
bytes-per-second numbers that drive the moving background, behind a token that unlocks
nothing else. That's eight endpoints where it used to be twelve — the eighth is `/worth`,
the post-session "was that worth it?" answer, which is asked *on the gate* and so has to
live where the gate lives.

Moving them also moved them out from behind the proxy's cross-site check, which is where
F25 came from. Writes are now refused at both origins: the proxy rejects cross-site
requests to `/budget/*`, and the app refuses them again on its own listener.

Belt and braces: the app binds loopback plus your tailnet address only — never
`0.0.0.0` — and the firewall drops port 5000 on every other interface, so neither your
LAN nor a public IPv6 address can reach it even if that bind ever widened.

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
