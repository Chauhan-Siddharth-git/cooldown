#!/bin/bash
# Ping an EXTERNAL dead-man's-switch service. If these pings stop, that service tells you.
#
# WHY THIS EXISTS, when the box already has a reboot alarm, a CA-read watcher, an hourly
# audit and an off-box notification channel: every one of those runs on the machine being
# attacked. Whoever holds the SD card can edit all of them. The one thing they cannot edit
# is a record kept somewhere else, and the one thing they cannot avoid is powering the box
# down -- you cannot image a card in a running Pi.
#
# tools/liveness-probe.sh was the first attempt and it is honest about its own limits: it
# runs on the laptop, so it only sees what it is awake for. Measured over 33 days it
# recorded 148 hours of BLIND window, concentrated overnight, and it was asleep through a
# real 04:00 reboot. Overnight is exactly when a card would be pulled. This runs on a
# service that never sleeps.
#
# THE PROPERTY THAT MAKES THIS DIFFERENT FROM EVERY OTHER MONITOR HERE: it cannot fail
# silently. send_alert can break and its silence reads as "nothing to report" -- that is
# why alert_probe exists. A dead-man's switch inverts it. If this script breaks, if the
# timer is disabled, if the network dies, if someone powers the box off: pings stop, and
# stopped pings ARE the alarm. Sabotaging it is indistinguishable from triggering it.
#
# WHAT IT CANNOT DO. Reachability is not authenticity. A modified image that keeps pinging
# looks alive. What you get is the WINDOW -- "quiet 03:12-03:47" -- which is the thing you
# correlate against the boot alert and the CA-read watcher. And the URL is on the card, so
# whoever takes it can forge pings afterwards; they cannot forge the gap they already made.
#
# The URL is a capability: anyone holding it can fake a ping. It lives in a systemd drop-in
# at 0600, never in this repo, for the same reason the alert URL does.
set -u

URL="${COOLDOWN_DEADMAN_URL:-}"
STATE=/var/lib/cooldown-deadman.state
AUDIT=/var/lib/cooldown-audit.json
MANIFEST=/var/lib/cooldown-deployed.manifest

if [ -z "$URL" ]; then
    # Not configured is not the same as broken, and must not be reported as if it were.
    printf '%s not-configured\n' "$(date +%s)" > "$STATE" 2>/dev/null || true
    chmod 644 "$STATE" 2>/dev/null || true
    exit 0
fi

# Carry the baseline off the box on every ping, not only when something happens. The CA
# fingerprint and the deploy revision are pinned ON the box -- cooldown-audit.sh holds the
# fingerprint -- and whoever holds the card can rewrite that pin. A ping history held by
# someone else is a copy of the baseline they cannot reach. Neither value is secret: the
# fingerprint appears in every certificate this box issues.
ca="$(python3 -c 'import json;print(json.load(open("'"$AUDIT"'")).get("ca_fp",""))' 2>/dev/null || echo)"
man="$(sha256sum "$MANIFEST" 2>/dev/null | cut -c1-16)"
up="$(cut -d. -f1 /proc/uptime 2>/dev/null)"

# No hostname and no address in the body, the same rule send_alert follows: this goes to a
# third party by definition.
body="ca=${ca:-?} manifest=${man:-?} uptime=${up:-?}s"

# --max-time so a hung endpoint cannot wedge the timer into the next run; -f so an HTTP
# error is a failure rather than a silently-downloaded error page; retries because a single
# blip should not look like a death when the grace period is generous anyway.
if curl -fsS --max-time 10 --retry 2 --retry-delay 3 \
        -H 'Content-Type: text/plain' --data-binary "$body" "$URL" >/dev/null 2>&1; then
    outcome=ok
else
    outcome="failed"
    logger -t cooldown-deadman "ping failed -- if this persists the switch will fire, which is correct"
fi
printf '%s %s\n' "$(date +%s)" "$outcome" > "$STATE" 2>/dev/null || true
chmod 644 "$STATE" 2>/dev/null || true

# A short rolling history for the heartbeat trace on /health. STATE answers "did the last
# ping work"; a trace needs the rhythm, so a missed ping shows up as a visibly longer flat
# stretch rather than being overwritten. 40 lines is a little over three hours at one ping
# per five minutes. Rewritten through a temp file in the same directory so a reader never
# sees a half-trimmed log.
#
# This is a VIEW of the pinger, not the evidence. It lives on the box, so whoever holds
# the card can fake it; the record that counts is the one healthchecks.io keeps.
LOG="${LOG:-/var/lib/cooldown-deadman.log}"
printf '%s %s\n' "$(date +%s)" "$outcome" >> "$LOG" 2>/dev/null || true
if tmp="$(mktemp "$(dirname "$LOG")/.deadman-log.XXXXXX" 2>/dev/null)"; then
    tail -n 40 "$LOG" > "$tmp" 2>/dev/null && chmod 644 "$tmp" && mv "$tmp" "$LOG" || rm -f "$tmp"
fi
