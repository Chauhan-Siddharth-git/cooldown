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
