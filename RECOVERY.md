# If the box is lost, stolen, sold — or you just want to rotate

Written to be followed when you're **not** thinking clearly. Nothing here is urgent
maintenance; it's the fire drill.

---

## The one thing to understand

The box holds a **certificate authority private key**. Whoever has that key can decrypt
traffic from any device that trusts it — but **only while those devices still trust it**,
and only if they can also get themselves in the path of your traffic.

> **So the kill switch isn't on the box. It's on your phone and laptop.**
> Untrust the certificate there and the stolen key becomes a useless file, instantly,
> even if the thief has full root on the hardware.

You cannot un-steal the key. You can make it worthless in about two minutes.

---

## 🚨 The box is lost or stolen — do this now

**1 · Untrust the certificate on every device (the important one)**

*iPhone / iPad:*
- Settings → General → VPN & Device Management → the Cooldown/mitmproxy profile → **Remove Profile**
- Then Settings → General → About → Certificate Trust Settings → make sure it's **off**

*macOS:* Keychain Access → System → find `mitmproxy` → delete.
*Windows:* `certmgr.msc` → Trusted Root Certification Authorities → delete `mitmproxy`.
*Linux/Firefox:* Settings → Privacy & Security → Certificates → View Certificates →
Authorities → find `mitmproxy` → Delete.

**2 · Remove the node from your private network**
- [Tailscale admin console](https://login.tailscale.com/admin/machines) → find the box →
  **Remove**. This cuts its access to your other devices immediately.
- While you're there, check for any auth key it could use to rejoin, and revoke it.

**3 · Turn off the exit node on your devices** so nothing tries to route through a machine
you no longer control. Tailscale app → Exit node → **None**.

**4 · Assume the usage history is gone with it.** It's behavioural data (which sites, how
long, per day) — embarrassing at worst, not credentials.

**What a thief does *not* get** — worth knowing so you don't panic-rotate everything:
this box deliberately holds **no SSH keys to other machines and no git credentials**.
Losing it does not put your laptop, your GitHub, or any other account at risk. Keep it
that way: never store a key on the box that opens something else.

---

## 📦 You're selling, gifting, or retiring the box

Do steps 1–3 above, then wipe the card. Deleting files isn't enough — reflash the SD card
entirely, or at minimum:

```bash
sudo rm -rf /var/lib/cooldown /var/backups/cooldown
sudo systemctl disable --now cooldown-app cooldown-proxy cooldown-redirect
redis-cli FLUSHALL
```

---

## 🔄 Rotating the certificate (do this yearly)

**Why bother:** the certificate is valid for ten years. If someone ever quietly copied the
SD card and put it back, they'd hold a working key for a decade and you'd never know.
Rotating means an old copy stops being useful.

> Theft is loud — you notice and revoke. **A quiet copy is the real risk**, and rotation is
> the only thing that closes it.

```bash
./rotate-ca.sh          # on the box; backs up the old key, makes a new one, restarts
```

Then **install the new certificate on each device** (see SETUP.md step 3). Between the
rotation and the reinstall, browsing on routed devices will fail with certificate
warnings — that's expected, and it's why you do this when you have ten minutes, not on
your way out the door.

**"Each device" is the wrong unit, and this is the step people get wrong.** Trust lives in
*stores*, not devices, and one machine has several: the system bundle (used by Chrome,
curl and everything linked against OpenSSL), Chrome's own NSS database, and **every Firefox
profile separately** — Firefox ignores the system store entirely. The first inventory taken
here found the CA trusted in five places on a single laptop. Updating "the laptop" updates
one of them, and the rest keep trusting a certificate you believe you retired.

```bash
tools/ca-trust-scan.sh          # run on each computer: lists every store, flags strays
```

[CA-TRUST.md](CA-TRUST.md) holds the inventory table to fill in and the per-platform
removal commands. Read the revocation section there before you need it: there is no CRL
and no OCSP for a privately-trusted root, so nothing revokes it for you — **revocation is
exactly as complete as your inventory is.**

Old backups of the previous key are kept in `/var/backups/cooldown/ca-*`; delete them once
every device is on the new certificate.

---

## 🔒 Hardening: stop the box reaching your other devices

By default every machine on your tailnet can reach every other one — so a compromised box
could scan or attack your phone and laptop. The box only needs to *receive* traffic and
send it out to the internet; it never needs to start a conversation with your devices.

In the [Tailscale admin console](https://login.tailscale.com/admin/acls), edit the access
policy:

```jsonc
{
  // The gateway is a tagged device, so it is NOT one of your personal machines
  // and gets no access of its own.
  "tagOwners": { "tag:gateway": ["autogroup:admin"] },

  "acls": [
    // Your own devices can reach anything of yours — including the box.
    { "action": "accept", "src": ["autogroup:member"], "dst": ["*:*"] }
    // Note there is no rule with the gateway as a source. It can answer your
    // devices and route them to the internet, but cannot initiate anything.
  ],

  // Let the tagged gateway offer itself as an exit node without manual approval.
  "autoApprovers": { "exitNode": ["tag:gateway"] }
}
```

Then, **on the box**, re-authenticate it under that tag:

```bash
sudo tailscale up --advertise-exit-node --advertise-tags=tag:gateway
```

> ⚠️ Two things to know. Tagging **transfers the device from you to the tag**, and it
> requires re-authenticating, so have the admin console open. And do this while you can
> reach the box locally (or physically) — if the policy is wrong you can lose remote
> access to it and will need a keyboard and monitor.

Verify afterwards: from the box, `tailscale ping <your-laptop>` should now fail, while
browsing on your phone through the exit node still works.

---

## 🔎 Would you even know?

You **cannot** detect the SD card being pulled. The card is the root filesystem — the
moment it leaves, the code that would raise an alarm is unreadable. Anything claiming
otherwise on a Pi is wishful thinking.

But nobody yanks a card from a running Pi. They **power it down, copy it, and put it
back** — and that leaves a trace. There are now four independent traces, which matters
because each covers a case the others miss.

**1. The reboot alarm.** Cooldown watches `/proc/sys/kernel/random/boot_id`, which changes
on every boot. If the box restarts for a reason you haven't acknowledged, the health page
shows a red banner until you dismiss it:

> ⚠️ **This box restarted on Sun 2 Aug, 12:53 PM** — if that wasn't you, someone had
> physical access.

A power cut will trigger it too. That's the right trade: you'd rather dismiss the odd false
alarm than miss the real one.

**2. The alarm is pushed off the box.** If you configure `COOLDOWN_ALERT_URL`, that same
event is sent to your phone at the moment it happens. This is the part that changes the
threat model: the case study admits anyone holding the card can clear the flag, and that is
still true — but **nobody can un-send a notification.** Tamper-evidence that lives only on
the tampered device is evidence the tamperer controls. Off by default; see
[SETUP.md](SETUP.md).

**3. The restart history outlives the dismissal.** Every unexplained boot is recorded, and
the health page lists the recent ones whether or not the banner has been dismissed.
Previously the banner was the *only* surfacing, so acknowledging a reboot erased the
evidence it had happened — the opposite of what evidence is for.

**4. An off-box record of when the box was unreachable.** `tools/liveness-probe.sh` runs on
a different machine and logs windows like `03:12 → 03:47 UNREACHABLE 35m`. This is the one
that catches an attacker who boots a *modified* image with the alarm suppressed — that
defeats the first three completely and leaves nothing behind on the box, but it cannot
hide the box being off. It distinguishes "the box is gone" from "I can't see it": if its
own network is down, the window is recorded as BLIND rather than as evidence.

**And separately, a read of the CA key is alerted.** `cooldown-cawatch` reports any open of
the private key outside the proxy's own startup. Note what it does *not* cover, because it
is the same blind spot as everything above: reading the card in another machine, with this
one powered off, produces no event anywhere.

**The limits, stated plainly:**
- Someone holding the card can edit these checks or clear the flag. This is
  tamper-**evidence** against the careless, not tamper-proofing against the determined. The
  off-box pieces (2 and 4) are the ones they cannot reach into and edit.
- They tell you the box restarted, or was unreachable — not what was done to it.
- The liveness probe only sees what it is awake for. A laptop asleep overnight records a
  BLIND window, not a clean one. For always-on coverage the box would need to check in with
  an external dead-man's-switch service; that runs on the box and can be stopped, but
  stopping it is itself the alarm.
- **A sticker or tamper tape across the SD slot is still a better detector than any of
  this**, and costs nothing. Physical problems want physical answers.

## Sanity checks, any time

```bash
# what the box is actually running
systemctl is-active cooldown-app cooldown-proxy cooldown-redirect cooldown-cawatch redis-server

# the certificate your devices are trusting
sudo openssl x509 -in /var/lib/cooldown/mitmproxy/mitmproxy-ca-cert.pem \
  -noout -fingerprint -sha256 -enddate

# is SSH still key-only?
sudo sshd -T | grep -i ^passwordauthentication      # must say: no

# is it patching itself?
systemctl is-active apt-daily-upgrade.timer
```
