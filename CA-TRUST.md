# CA trust inventory and revocation

The box mints certificates for every gated domain. Whoever holds the CA private key can
impersonate those domains to **every device that trusts it** — that is the whole security
model, stated in one sentence.

`rotate-ca.sh` has always said "every routed device must install the new certificate."
Nothing said *which* devices, and nothing said which **trust stores** on each device. That
gap is what this file closes. It is a prerequisite for F26 (adding a gated site requires a
rotation), not a follow-up to it: a rotation you cannot complete is a rotation you will not
start, and F26 turns that into "you can never add a site."

## Take the inventory, don't remember it

```bash
tools/ca-trust-scan.sh          # on each Linux client; asks the box for the live fingerprint
```

It walks the system bundle, `~/.pki/nssdb`, and **every Firefox profile**, prints trust bits
per entry, and flags CA material that is not the live one.

**People think in devices; trust lives in stores.** One laptop in this deployment had the CA
in five places across four Firefox profiles plus the system bundle. A rotation that updates
"the laptop" updates one of them.

Three traps this scan exists to avoid, each hit while writing it:

- **Firefox ignores the system trust store entirely.** Installing to
  `/usr/local/share/ca-certificates` does nothing for Firefox, and removing it there does
  not untrust it either.
- **`grep`ping `/etc/ssl/certs/ca-certificates.crt` for a CA name finds nothing even when
  the CA is present** — the bundle holds PEM blobs, not subject lines. It reads exactly like
  "not installed". Parse it with `openssl`, never grep it.
- **A certificate present with no trust bits (`,,`) validates nothing.** It is residue, not
  trust. Worth removing, but not urgent, and not a rotation blocker.

## Keep the inventory outside this repo

The filled-in table names your devices, your browser profiles and your CA's fingerprint.
That is deployment data, not project data — keep it wherever you keep the rest of your
infrastructure notes, not in a public repository. What belongs here is the shape:

| Device | Store | Trusted | How it was established |
|---|---|---|---|
| laptop | system bundle | yes | `ca-trust-scan.sh`, fingerprint match |
| laptop | Firefox profile A | yes | same |
| laptop | Firefox profile B | no | holds a *different*, untrusted CA |
| phone | OS trust settings | yes | **inferred** — intercepted requests in the proxy journal |

Two things that table has to record, because they are the ones that go wrong:

**"How it was established."** A phone row can only ever be inference: iOS and Android have
no remote way to enumerate a trust store. Successful interception proves trust — TLS cannot
succeed without it — but says nothing about other profiles or apps, and it is the row most
likely to rot. Write down that it is inferred.

**Which client addresses are real.** Deriving "devices that trust the CA" from the proxy
journal is sound, but the journal contains destination servers too. In the CGNAT range
`100.64.0.0/10`, addresses like `100.28.x` and `100.63.x` are *outside* it and are public
servers. Reading them as devices was the first wrong answer this inventory produced.

### Look for unaccounted CA material while you are there

The scan reports CA material that is not the live one. In this deployment it found a second
CA **with its private key** in `~/.mitmproxy/` — mitmproxy's default confdir, created before
`start.sh` pinned `CONFDIR`. The fix stopped new ones appearing; it did not remove the one
already there. Trusted by nothing, so inert — but a CA private key in a home directory is
one "trust this" click from being live.

```bash
rm -rf ~/.mitmproxy
certutil -D -d sql:$HOME/.mozilla/firefox/<profile> -n "mitmproxy - mitmproxy"
```

## Revocation

**There is no CRL and no OCSP.** A privately-trusted root is trusted because a device says
so locally; nothing phones home to ask whether it is still valid. Revocation means
*removing it from every store in the inventory above*, by hand, per store. Until a device is
cleaned, a stolen key impersonates every gated domain to it, and nothing on that device will
warn.

That is why the inventory is the prerequisite: **revocation is exactly as complete as the
inventory is.**

### If the key is believed stolen

Order matters — the first two steps are the ones that limit damage.

1. **Stop the proxy.** `sudo systemctl stop cooldown-proxy` — it is the thing serving
   certificates from that key.
2. **Rotate.** `./rotate-ca.sh` generates a new CA and keeps the old at the path it prints.
3. **Remove the old CA from every store in the inventory**, using the commands below. Do
   this before installing the new one; a half-finished pass is easier to audit when the old
   one is gone than when both are present.
4. **Re-scan every device** with `ca-trust-scan.sh` and confirm the old fingerprint is
   absent. Not "I removed it" — confirm.
5. **Delete the old CA files on the box** once every device is clean.
6. Consider the window: anything intercepted between theft and rotation was interceptable by
   the holder. The `cooldown-cawatch` alert timestamps the read, which is what bounds it.

### Removal commands per store

```bash
# Linux system bundle (Chrome, curl, wget, anything using OpenSSL)
sudo rm /usr/local/share/ca-certificates/mitmproxy.crt
sudo update-ca-certificates --fresh          # --fresh, or the compiled bundle keeps it

# Firefox — EVERY profile, separately
for p in ~/.mozilla/firefox/*/; do
  [ -f "$p/cert9.db" ] && certutil -D -d sql:"$p" -n "mitmproxy-pi" 2>/dev/null
done

# Chrome/Chromium on Linux (its own NSS store, separate from the system bundle)
certutil -D -d sql:$HOME/.pki/nssdb -n "mitmproxy-pi"

# iOS: Settings → General → VPN & Device Management → remove the profile,
#      THEN Settings → General → About → Certificate Trust Settings and confirm the
#      toggle is gone. Removing the profile without checking that screen has left the
#      trust in place on some iOS versions.

# macOS: sudo security delete-certificate -c "mitmproxy" /Library/Keychains/System.keychain
# Android: Settings → Security → Encryption & credentials → User credentials → remove
```

Then verify, on each device: `tools/ca-trust-scan.sh` reports zero places holding the old
fingerprint. On iOS, load a gated site — a certificate warning is the confirmation.

## Rotation checklist (the F26 path)

Adding a gated site requires a new CA because the name constraints are baked in. The cost is
this list, which is why they should be batched:

1. Add the domain in `news_domains.py` / `SITES`, regenerate `--allow-hosts`.
2. `./rotate-ca.sh`.
3. Install the new CA in **every store the scan listed** on each computer, not just the
   system bundle — that is the step people think they have finished and have not.
4. Install on each phone; confirm via its certificate trust screen.
5. `tools/ca-trust-scan.sh` on each Linux client — expect zero "OTHER CA" rows.
6. Load a gated site on each device before deleting the old CA.
7. Update the inventory table above, including the date.
