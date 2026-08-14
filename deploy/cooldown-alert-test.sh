#!/bin/bash
# Send a clearly-labelled test through the alert path, without faking an alarm.
#
#   sudo /usr/local/sbin/cooldown-alert-test.sh
#
# Why this exists. Verifying the alert channel previously meant lying to the box: setting
# a stale boot_id so boot_watch() would fire. That worked, and it also sent a phone
# notification saying "unexplained reboot" when none had happened, and wrote a false
# entry into boot_events -- a log whose entire purpose is to be evidence. An alert path
# you can only test by crying wolf is one nobody tests, and then it is an unverified
# claim again.
#
# THE TRAP THIS AVOIDS. COOLDOWN_ALERT_URL is set by a systemd drop-in, so it exists in
# the SERVICE's environment and not in yours. Reading it from the shell reports "not
# configured" on a box where it is configured perfectly well -- which is exactly what
# happened the first time this feature was verified. It is read from the unit here.
set -u

UNIT="${ALERT_TEST_UNIT:-cooldown-app.service}"
TIMEOUT=8

url="$(systemctl show "$UNIT" -p Environment --value 2>/dev/null \
       | tr ' ' '\n' | sed -n 's/^COOLDOWN_ALERT_URL=//p' | head -1)"

if [ -z "$url" ]; then
    # Not configured is a distinct answer from failed. Reporting them the same way is how
    # "no alert arrived" becomes ambiguous.
    echo "NOT CONFIGURED — no COOLDOWN_ALERT_URL in ${UNIT}'s environment."
    echo "  The alert feature ships off by default; this is not a fault."
    echo "  To switch it on, add a drop-in:"
    echo "    /etc/systemd/system/${UNIT}.d/alert.conf"
    echo "      [Service]"
    echo "      Environment=COOLDOWN_ALERT_URL=https://ntfy.sh/<your-topic>"
    exit 2
fi

# Unmistakable. Someone reading this on a phone at 3am must not spend one second
# wondering whether the box actually rebooted.
msg="cooldown: TEST ALERT — nothing is wrong, no reboot occurred. Sent by hand from $(hostname) at $(date '+%Y-%m-%d %H:%M')."

echo "  unit:  $UNIT"
echo "  topic: ${url%/*}/<topic>            (the topic is the credential; not printed)"
echo "  sending…"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" \
        -H 'Content-Type: text/plain' -d "$msg" "$url" 2>/dev/null)"

case "$code" in
    2*) echo "  SENT — the endpoint accepted it (HTTP $code)."
        echo "  That is delivery to the service, not to your phone. Confirm it arrived."
        rc=0 ;;
    000) # curl reports 000 for refused, DNS failure and timeout alike. Naming only the
         # timeout sends the reader looking for a network stall that may not exist.
         echo "  FAILED — no HTTP response: refused, DNS failure, or no reply in ${TIMEOUT}s."
         echo "  Check the box has egress and that the URL's host resolves."
         rc=1 ;;
    *)  echo "  FAILED — endpoint answered HTTP $code."
        rc=1 ;;
esac

# Deliberately does NOT touch alert_last, boot_events or unacked_boot. alert_last records
# what happened to a REAL alert and is shown on /health; a test writing there would
# corrupt the one signal this whole feature exists to provide.
echo "  (no state changed: alert_last, boot_events and unacked_boot are untouched)"
exit "$rc"
