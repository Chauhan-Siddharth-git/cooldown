# The concepts, in plain English

Cooldown sits at the crossroads of a few different worlds — networking, certificates,
Linux services — so the docs use words that are obvious once you know them and opaque
until you do. Here they all are, in the order you'll meet them.

You don't need to read this end to end. Skim it, then come back when a word trips you up.

---

## The everyday internet

**Website / server** — the computer that actually holds Reddit. When you "go to Reddit,"
your phone asks that computer to send the page over.

**Request and response** — the two halves of every action online. Your phone *requests*
("send me the front page"), the server *responds* (the page). Loading one page is dozens
or hundreds of these, not one.

**HTTPS and the padlock 🔒** — the sealed-envelope version of that conversation. Everything
between your phone and the website is scrambled, so anyone in between (your internet
provider, the café wifi) sees only *that* you talked to Reddit, never *what* was said.
The padlock in your address bar means the envelope is sealed.

**Encryption** — the scrambling itself. Scrambled data looks like meaningless gibberish;
that's why Cooldown's background shows encrypted traffic as random letters and numbers.

**Headers** — the writing on the envelope, as opposed to the letter inside. Every request
and response carries a few dozen: what kind of content this is, how long to keep it, which
cookies to send. You never see them, and almost every rule in this project is one.

**TLS** — the technology that does the actual sealing. "HTTPS" is just ordinary web traffic
with TLS wrapped around it. When the docs say the box *terminates TLS*, that's the polite
way of saying the box is where the envelope gets opened.

**DNS** — the internet's phone book. Before your phone can reach `reddit.com` it asks
"what's the number for reddit.com?" Here's the catch: **that question is usually sent
unsealed**, in plain readable text. That's why Cooldown's background paints DNS in red —
it's traffic anyone in between can read.

**IP address** — the "number" DNS gives back. Like a street address for a computer.

---

## The pieces Cooldown adds

**The box** — a small computer that stays on in your home. A Raspberry Pi is the usual
choice: cheap, silent, sips electricity. Your phone and laptop send their traffic *through*
it, which is what lets Cooldown do its job.

**Raspberry Pi** — a credit-card-sized computer, roughly $50–80. It runs Linux. That's it;
there's no magic to it.

**Proxy** — a middleman for internet traffic. Instead of your phone talking to Reddit
directly, it hands the request to the box, and the box talks to Reddit on its behalf. Being
in the middle is exactly what lets Cooldown say "nope, time's up" and show its own page.

**VPN / exit node** — a way to make *all* of your phone's traffic take a detour through
your box, automatically, even on cellular. "Exit node" is Tailscale's name for the machine
your traffic exits through.

**Tailscale / WireGuard** — a private network just for your own devices. It gives your
phone, laptop, and box their own private addresses so they can talk directly, from
anywhere, without exposing anything to the public internet. WireGuard is the underlying
technology; Tailscale is the easy wrapper.

**mitmproxy** — the program on the box that opens the envelopes. "MITM" stands for
*man in the middle*, which is exactly what it does — deliberately, for you, on your own
traffic. It's the same technique attackers use, which is why the security page matters.

---

## Certificates: the part worth understanding

This is the one concept genuinely worth five minutes, because it's what you're consenting
to. Fuller version, with the risks, in [SECURITY.md](SECURITY.md).

**Certificate** — a website's ID card. When you visit your bank, it presents a certificate
proving "I really am the bank." Your phone checks it before showing the padlock.

**Certificate Authority (CA)** — the trusted organization that *signs* those ID cards.
Your phone ships with a list of a few hundred CAs it trusts. If a certificate is signed by
one of them, your phone accepts it. If not, you get a big scary warning.

**The CA you create** — Cooldown makes its own certificate authority on your box, and you
add it to your phone's trusted list. From then on, your box can produce an ID card for
*any* website and your phone will accept it. That's how it can open your traffic.

Think of it as a **master key** to your own mail:

- **You make it yourself**, on your own box, and it never leaves.
- Whoever holds it can read the traffic of every device that trusts it — **all** of it,
  not just the sites you're budgeting.
- Which is why: **never install a certificate authority that somebody else gave you.**
  There is no innocent reason for anyone to hand you one.

**Fingerprint** — a short code that uniquely identifies a certificate, like a serial
number. Useful for checking "is this still the same key?" after moving things around.

---

## The words the security pages use

These turn up in [SECURITY.md](SECURITY.md) and the
[case study](SECURITY-CASESTUDY.md). None of them are as forbidding as they look.

**Origin** — one website's territory, written as scheme + host: `https://reddit.com`. The
browser treats it as a fence.

**Same-origin** — the rule that code from one origin can read anything else on that origin
and almost nothing on another. It's the reason a random open tab can't rifle through your
email. Worth understanding here because Cooldown **adds its own pages to a site's origin** —
`/budget/stats` is served on Reddit's — so Reddit's scripts start out on the inside of that
fence with them. Rebuilding that boundary by hand is finding F9.

**Content-Security-Policy (CSP)** — a header in which a site lists what its own pages are
allowed to load and run: which scripts, where data may be sent, who may embed it. It is a
site saying *"even if somebody smuggles code into my page, don't run it."*

**Nonce** — "number used once." A random code word the box invents **fresh for every single
page load**, then writes in two places: into that page's CSP (*"scripts carrying this word
may run"*) and onto the one script Cooldown injects. The browser matches them, runs the
stopwatch, and goes on refusing everything else.

> Why it matters here: to inject anything, Cooldown has to get past the site's CSP. The
> blunt way is to delete the whole header — which also throws away the rules about where
> data may be sent and who may embed the page, none of which had anything to do with our
> script. The nonce is the surgical way: **add one line to the policy, leave the rest
> enforced.** The word is discarded after that page load, so it can't be learned in advance
> and reused. This is finding F8, and it's the single biggest security improvement in the
> project.

**XSS (cross-site scripting)** — the bug CSP exists to contain: an attacker gets their code
running inside someone else's page — smuggled through a comment, a username, a search term
the site echoes back — where it acts with that page's full privileges, including your
logged-in session.

**CSRF (cross-site request forgery)** — tricking *your* browser into making a
state-changing request while you're logged in. Another site quietly submits a form to
yours; your cookies ride along and it looks legitimate. The defence is requiring something
the other site can't know or forge.

**Allow-list** — naming what's permitted and refusing everything else, rather than listing
what's banned. More tedious, much harder to slip past — a ban-list only stops what you
thought of.

**Exfiltration** — the "and then what?" of an attack: getting the stolen data out to
somewhere the attacker controls. A lot of CSP is really about making this step hard.

**Least privilege** — giving each program exactly the power its job needs and not a scrap
more, so a break-in yields as little as possible.

**`root`** — Linux's administrator: unlimited power over the machine. **Privilege
escalation** is an attacker turning a small foothold into that.

**Forbidden header name** — a header the browser flatly refuses to let page code set, no
matter what. `Sec-Fetch-Dest` is one: the browser fills it in with *how* a request was
made, and a script cannot lie about it. That unforgeability is what makes it usable as a
lock (F9).

**Tamper-evidence** — not stopping someone interfering, just making it impossible for them
to do it *unnoticed*. Weaker than prevention, and often the only honest option — the reboot
alarm is this, and so is a sticker over the SD slot.

**Mutation testing** — checking your tests by deliberately breaking the code and confirming
they fail. A test that passes either way is decoration, and this is how you find out.

**Brute force** — simply guessing, millions of times, until a password works. The reason
SSH is key-only now.

**Supply chain** — everything you didn't write but ship anyway. Someone else's bug becomes
yours the moment you install it.

**Open redirect** — a page that forwards visitors to whatever address the link hands it,
which lets an attacker borrow your site's good name to send someone somewhere bad.

**Parser differential** — two pieces of code disagreeing about what the same text means.
Attacks live in the gap: one component sees a harmless address, the other sees a different
one and acts on it.

**Hash** — a one-way fingerprint of some data. Easy to compute, effectively impossible to
reverse.

---

## Inside the box

**Linux** — the operating system the box runs. Not Windows, not macOS.

**Terminal / command line** — the text-based way of telling Linux what to do. The setup
guide's `code blocks` are lines you paste in.

**SSH** — how you get a terminal on the box from your laptop, without plugging in a monitor.

**`sudo`** — "do this as the administrator." Powerful, hence used sparingly. Cooldown is
deliberately built so its programs almost never need it.

**Service / systemd** — a program that starts automatically at boot and keeps running,
restarting itself if it crashes. Cooldown installs three, so the whole thing survives a
power cut without you touching anything.

**Service account** — a fake "user" that exists only to run a program. It has no password
and can't be logged into. Running Cooldown's programs under these means that if one is
ever broken into, the intruder gets an account that can do essentially nothing — instead
of one that controls the whole box.

**Firewall / iptables** — the box's door policy: which traffic is allowed in, from where.
Cooldown uses it to make sure only *your* devices can reach the proxy, and to send your
phone's web traffic into the proxy in the first place.

**QUIC** — a newer, faster way browsers talk to Google and YouTube. Cooldown blocks it, so
the browser falls back to the older form it can actually read. Costs a little speed, buys
the entire feature.

**Transparent redirect** — bending traffic into the proxy without the device being told.
Your phone thinks it's talking to Reddit; the box quietly intercepts on the way past. It's
why there are no proxy settings to configure on the phone.

**Chain (iptables)** — a named list of firewall rules. Most end in a verdict — accept, drop.
`TRAFFIC_ACCT` deliberately has none: packets are counted and fall straight through, which
is why the traffic background can't break your internet even if it's wrong.

**Loopback and IPv6** — loopback is the box talking to itself (`127.0.0.1`). IPv6 is the
newer style of address that runs alongside the old one — worth naming because a firewall
rule written only for the old style leaves the new one wide open, which was finding F1.

**venv** — a private folder of Python libraries belonging to one project, so it can't
collide with anything else on the box.

**Buffering vs streaming** — buffering means holding a whole response so it can be edited;
streaming means passing it straight through untouched. Cooldown buffers only HTML pages it
needs to inject into, and streams everything else — which is why video still plays smoothly.

**Redis** — a small, fast place to store notes. Cooldown keeps its numbers there: minutes
spent today, whether a cooldown is running, your usage history. It's all on your box; none
of it goes anywhere.

**Docker / container** — a way to run software in a sealed box-within-your-computer, so you
can try it without installing pieces all over your system. Cooldown offers this as a
try-before-you-commit option.

---

## How Cooldown thinks about time

**Budget** — the minutes you're allowed on a site. Reddit 10, YouTube 15, and so on.

**Shared bucket** — all the budgeted sites draw from *one* pool of minutes, so hopping
from Reddit to YouTube doesn't hand you a fresh allowance. Each site also has its own cap,
which is why YouTube can show more time left than Reddit at the same moment.

**Foreground time** — time you're *actually looking* at the screen. A tab left open in the
background, or a locked phone, costs you nothing. Cooldown does this with a heartbeat.

**Heartbeat** — a tiny signal the page sends back to your box every few seconds while the
tab is visible. No heartbeat, no charge. This is why the timer matches reality instead of
punishing you for forgotten tabs.

**Cooldown (the pause)** — when the bucket is empty you get a wait, not a wall. The pause
is the entire point: it breaks the trance, then lets you back in.

**Wind-down and night mode** — in the hour before bedtime your allowance shrinks
gradually; overnight there's a small buffer and then it's closed.

**The gate** — the page you see instead of the site: how much time is left, or the
countdown until it reopens.

**Injection** — Cooldown adding a small piece of its own code to a page as it passes
through. That's how the heartbeat gets there, and how YouTube's Shorts and home feed get
hidden.

---

## Where to go next

- [**README**](README.md) — what Cooldown is and how to set it up
- [**SECURITY.md**](SECURITY.md) — the risks, in plain language. Read before installing
- [**ARCHITECTURE.md**](ARCHITECTURE.md) — follow one real request through the whole system
- [**SETUP.md**](SETUP.md) — the step-by-step build
