#!/bin/bash
# Alert when the CA private key is read by anything other than the proxy loading it.
#
# The CA is the whole trust story: anyone holding it can mint a certificate for any gated
# domain and impersonate it to every device that trusts this box. Nothing previously
# noticed a read. cooldown-audit.sh pins the CA's fingerprint, but that detects MODIFICATION
# -- a thief copies the key and changes nothing, so the fingerprint check stays green.
#
# WHY THIS WORKS AT ALL: the legitimate read pattern is almost empty. mitmproxy loads the
# CA once at startup and holds no descriptor afterwards (verified: zero open handles on it
# while running). So a read that is not within GRACE seconds of cooldown-proxy.service starting has
# no innocent explanation.
#
# WHAT IT CANNOT DO, stated plainly so nobody reads silence as safety:
#   - inotify reports the file and the event, never the process. There is no PID or UID in
#     an event. This says the key was read, not who read it. auditd is the tool that
#     answers "who", at the cost of a daemon and unverified kernel support.
#   - Reading the SD card offline -- powered down, in another machine -- produces no event
#     on a system that is not running. Physical possession still beats this entirely.
#   - Root can stop this service. It runs as root rather than as cooldownproxy precisely so
#     that compromising the network-facing proxy is not enough to blind it, but it is not
#     armour against an attacker who already owns the box.
#
# atime was the cheaper design -- no daemon, just a stat in the hourly audit -- and it is
# impossible here: / is mounted noatime, so reads are never recorded. The key's atime
# currently reads 2026-06-18 despite being read at every proxy restart since.
set -u

CA_DIR="${CAWATCH_DIR:-/var/lib/cooldown/mitmproxy}"
# Only the files carrying the private half. mitmproxy-ca-cert.pem/.cer are the public
# certificate that every trusting device already has; alerting on those is pure noise.
SECRETS="mitmproxy-ca.pem mitmproxy-ca.p12"
GRACE="${CAWATCH_GRACE:-15}"        # seconds after proxy start in which a read is benign
QUIET="${CAWATCH_QUIET:-300}"       # after alerting, log but do not re-alert for this long
PROXY_UNIT="${CAWATCH_PROXY_UNIT:-cooldown-proxy.service}"
ALERT_UNIT="${CAWATCH_ALERT_UNIT:-cooldown-app.service}"
TAG=cooldown-cawatch

# Same trick as cooldown-alert-test.sh: the URL lives in a systemd drop-in, so it is in the
# app's environment and not in ours. Reading it from this shell would find nothing.
alert_url() {
    systemctl show "$ALERT_UNIT" -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | sed -n 's/^COOLDOWN_ALERT_URL=//p' | head -1
}

proxy_start_epoch() {
    local ts
    ts="$(systemctl show "$PROXY_UNIT" -p ActiveEnterTimestamp --value 2>/dev/null)"
    # Empty when the unit has never started. Returning 0 makes the age enormous, so the
    # event is treated as suspicious -- failing loud, which is the right direction here.
    [ -n "$ts" ] && date -d "$ts" +%s 2>/dev/null || echo 0
}

last_alert=0

logger -t "$TAG" "watching $CA_DIR for reads of: $SECRETS (grace ${GRACE}s)"

# Watch the DIRECTORY, not the files. A watch on a file follows the inode, so ./rotate-ca.sh
# replacing the key would silently leave this watching a deleted inode -- armed, reporting
# nothing, which is the worst failure a monitor has.
inotifywait -m -q -e open --format '%f' "$CA_DIR" | while read -r fname; do
    case " $SECRETS " in *" $fname "*) ;; *) continue ;; esac

    now="$(date +%s)"
    age=$(( now - $(proxy_start_epoch) ))

    if [ "$age" -ge 0 ] && [ "$age" -le "$GRACE" ]; then
        logger -t "$TAG" "benign: $fname read ${age}s after $PROXY_UNIT start"
        continue
    fi

    logger -t "$TAG" "ALERT: $fname was read ${age}s after $PROXY_UNIT started -- not a startup load"

    # An attacker running `grep -r` over the directory generates an event per file. Alert
    # once, keep logging the rest: a phone buzzing forty times is a phone put face-down.
    if [ $(( now - last_alert )) -lt "$QUIET" ]; then
        logger -t "$TAG" "(alert suppressed: within ${QUIET}s of the previous one)"
        continue
    fi
    last_alert="$now"

    url="$(alert_url)"
    if [ -z "$url" ]; then
        logger -t "$TAG" "no COOLDOWN_ALERT_URL configured -- detection logged, not sent"
        continue
    fi
    if curl -sf --max-time 8 -H 'Content-Type: text/plain' \
            -d "cooldown: CA PRIVATE KEY READ on $(hostname) at $(date '+%F %H:%M') -- $fname, ${age}s after the proxy started. If this was not you, the CA can impersonate every gated site to every device that trusts it." \
            "$url" >/dev/null 2>&1; then
        logger -t "$TAG" "alert sent"
    else
        logger -t "$TAG" "alert FAILED to send -- detection stands, delivery did not"
    fi
done
