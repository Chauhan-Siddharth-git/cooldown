#!/bin/bash
# Record, from OFF the box, the windows in which the box was unreachable.
#
#   tools/liveness-probe.sh            # one probe; run it from a timer every couple of minutes
#   tools/liveness-probe.sh --report   # print the gap history
#
# WHAT THIS CATCHES that nothing else does. The reboot alarm and boot_events both live on
# the box, so an attacker who boots a modified image with the alarm suppressed defeats them
# completely and leaves no trace. They cannot suppress a record kept somewhere else. Powering
# the box down -- to image the card, to swap it, to boot something else -- produces an
# unreachable window here, on a machine they do not control.
#
# WHAT IT DOES NOT PROVE. Reachability is not authenticity: a modified image that answers
# /remaining looks alive to this probe. What it gives you is the WINDOW -- "unreachable
# 03:12-03:47" is the thing you correlate against, and an unexplained 35 minutes at 3am is
# the finding. Pair it with the boot alert, which says a boot happened, and cawatch, which
# says the key was read.
#
# THE FALSE-POSITIVE PROBLEM, which is most of the work here. A laptop that suspends, drops
# wifi, or roams looks exactly like a box that has been switched off. A prober that cannot
# tell those apart reports tampering every night and gets ignored within a week. So every
# failed probe is checked against a control: if the control is also unreachable, the fault is
# local and the window is recorded as BLIND -- honestly unknown -- rather than as a gap.
#
# COVERAGE, stated plainly: this only sees what it is awake for. A laptop asleep from 01:00
# to 08:00 records a seven-hour BLIND window, not seven hours of evidence. For always-on
# coverage the box should also ping an external dead-man's-switch service (healthchecks.io's
# free tier is the usual one) from a systemd timer; that runs on the box, so an attacker can
# stop it -- but stopping it is itself the alarm, which is the point of a dead-man's switch.
set -u

BOX="${COOLDOWN_BOX:-100.64.0.1}"        # override: COOLDOWN_BOX=<tailnet ip or name>
PORT="${COOLDOWN_PORT:-5000}"
CONTROL="${COOLDOWN_CONTROL:-1.1.1.1}"   # something that is up when YOUR network is up
GAP_MIN="${COOLDOWN_GAP_MIN:-300}"       # seconds; shorter blips are noise, not evidence
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cooldown-liveness"
STATE="$STATE_DIR/state"
GAPS="$STATE_DIR/gaps.log"
mkdir -p "$STATE_DIR"

alert() {
    url="${COOLDOWN_ALERT_URL:-}"
    [ -n "$url" ] || return 0
    curl -sf --max-time 8 -H 'Content-Type: text/plain' -d "$1" "$url" >/dev/null 2>&1
}

if [ "${1:-}" = "--report" ]; then
    echo "gap history ($GAPS):"
    [ -s "$GAPS" ] && cat "$GAPS" || echo "  none recorded"
    if [ -f "$STATE" ]; then
        read -r st since < "$STATE"
        echo
        echo "current: $st since $(date -d "@$since" '+%F %H:%M:%S') ($(( ($(date +%s) - since) / 60 ))m)"
    fi
    exit 0
fi

now="$(date +%s)"

if curl -sf --max-time 6 "http://${BOX}:${PORT}/remaining" >/dev/null 2>&1; then
    status=UP
elif ping -c1 -W3 "$CONTROL" >/dev/null 2>&1 || curl -sf --max-time 6 "http://${CONTROL}" >/dev/null 2>&1; then
    # Control reachable, box not: the box really is unreachable from here.
    status=DOWN
else
    # Cannot reach the control either. This machine is the problem, or is offline. Recording
    # this as a box outage is how the log fills with tampering reports that are really
    # suspend events.
    status=BLIND
fi

prev=""; since="$now"
[ -f "$STATE" ] && read -r prev since < "$STATE"
[ -n "$prev" ] || prev="$status"

if [ "$status" = "$prev" ]; then
    # Write it back even though nothing changed, preserving the ORIGINAL start time. The
    # first version exited here before ever creating the state file, so no run -- first or
    # thousandth -- had a previous state to compare against and no gap could ever be
    # detected. It failed silently and looked exactly like "nothing to report".
    echo "$prev $since" > "$STATE"
    exit 0
fi

dur=$(( now - since ))
human="$(( dur / 60 ))m"
[ "$dur" -ge 3600 ] && human="$(( dur / 3600 ))h $(( (dur % 3600) / 60 ))m"

# Only closing a window is interesting -- at that point its length is known.
if [ "$prev" = DOWN ] && [ "$dur" -ge "$GAP_MIN" ]; then
    line="$(date -d "@$since" '+%F %H:%M') -> $(date -d "@$now" '+%H:%M')  UNREACHABLE $human"
    echo "$line" >> "$GAPS"
    echo "$line"
    alert "cooldown: box was unreachable for $human, $(date -d "@$since" '+%F %H:%M') to $(date -d "@$now" '+%H:%M'). If you did not power it down, treat this as the window in which the card could have been removed."
elif [ "$prev" = BLIND ] && [ "$dur" -ge "$GAP_MIN" ]; then
    # Written to the log too. A record that says "I was not watching" is worth more than a
    # silent hole that later reads as "the box was fine".
    echo "$(date -d "@$since" '+%F %H:%M') -> $(date -d "@$now" '+%H:%M')  BLIND $human (this machine was offline; box state unknown)" >> "$GAPS"
fi

echo "$status $now" > "$STATE"
