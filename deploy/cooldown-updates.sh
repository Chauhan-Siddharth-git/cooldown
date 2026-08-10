#!/bin/bash
# Hourly: keep apt's own timers honest, and record what is pending so /health can show it.
#
# Why the watchdog half exists. apt-daily-upgrade.timer went silent for ten days. It was
# enabled, active, Persistent=true, and the clock was NTP-synced -- but systemd had
# computed no next elapse point, so it would never have fired again. Nothing reported it.
# The box simply stopped patching itself and looked exactly like a box that was fine.
# Same shape as the heartbeat dying: the thing that should be working stops, and silence
# is indistinguishable from success. So: check hourly, kick it, and say so.
#
# Why the reporting half exists. "Are we behind on patches?" was previously answerable
# only by SSHing in. Now it is on the dashboard, which is the only place it will actually
# be seen.
set -u

STATE=/var/lib/cooldown-updates.json
TMP="$(mktemp /var/lib/.cooldown-updates.XXXXXX)" || exit 1
trap 'rm -f "$TMP"' EXIT

# --- half one: the watchdog -------------------------------------------------------
kicked=0
for t in apt-daily.timer apt-daily-upgrade.timer; do
    if ! systemctl is-active --quiet "$t"; then
        logger -t cooldown-updates "$t inactive; starting"
        systemctl start "$t" && kicked=$((kicked + 1))
        continue
    fi
    # An active timer with no scheduled elapse is the failure seen on 2026-07-31. It
    # cannot be spotted with is-active or is-enabled -- both report healthy, and
    # `systemctl status` says "active (running)". The only tell is Trigger: n/a.
    #
    # Cause: this Pi has no battery-backed RTC, so the clock starts wrong at every boot
    # and jumps when NTP syncs. A Persistent= timer that computed its elapse point
    # before that jump keeps it, and once the point is in the past the timer never
    # advances again. `systemctl restart` does NOT fix it -- verified on the live box; it
    # re-triggers the unit and leaves the same stale point. The stamp file has to go so
    # systemd recomputes from scratch.
    # Check BOTH elapse fields. Calendar timers populate Realtime and monotonic ones
    # (OnBootSec/OnUnitActiveSec) populate Monotonic; testing only Realtime would call
    # every healthy monotonic timer wedged and reset it once an hour, forever. Both
    # entries in this loop are calendar timers today -- this is for whoever adds a third.
    next_rt="$(systemctl show "$t" -p NextElapseUSecRealtime --value 2>/dev/null)"
    next_mono="$(systemctl show "$t" -p NextElapseUSecMonotonic --value 2>/dev/null)"
    if [ -z "$next_rt" ] && { [ -z "$next_mono" ] || [ "$next_mono" = "0" ]; }; then
        logger -t cooldown-updates "$t has no next elapse; clearing stamp and restarting"
        systemctl stop "$t"
        rm -f "/var/lib/systemd/timers/stamp-$t"
        systemctl start "$t"
        after="$(systemctl show "$t" -p NextElapseUSecRealtime --value 2>/dev/null)"
        if [ -n "$after" ]; then
            logger -t cooldown-updates "$t rescheduled for $after"
        else
            logger -t cooldown-updates "$t STILL has no next elapse after reset -- needs a human"
        fi
        kicked=$((kicked + 1))
    fi
done

# --- half two: what is pending ----------------------------------------------------
# dist-upgrade, not upgrade: plain upgrade holds back anything needing a new dependency,
# which on this box is every kernel. Simulated only -- this script never installs.
sim="$(apt-get -s dist-upgrade 2>/dev/null || true)"
pending=$(printf '%s\n' "$sim" | grep -c '^Inst ')
security=$(printf '%s\n' "$sim" | grep '^Inst ' | grep -ci 'security' || true)

reboot=false
[ -f /var/run/reboot-required ] && reboot=true

last_run="$(systemctl show apt-daily-upgrade.service -p ExecMainStartTimestamp --value 2>/dev/null)"
last_epoch=0
[ -n "$last_run" ] && last_epoch="$(date -d "$last_run" +%s 2>/dev/null || echo 0)"
last_result="$(systemctl show apt-daily-upgrade.service -p Result --value 2>/dev/null || echo unknown)"

printf '{"pending":%d,"security":%d,"reboot_required":%s,"timers_kicked":%d,' \
       "${pending:-0}" "${security:-0}" "$reboot" "$kicked" > "$TMP"
printf '"last_run":%d,"last_result":"%s","checked":%d}\n' \
       "${last_epoch:-0}" "${last_result:-unknown}" "$(date +%s)" >> "$TMP"

# 644 on purpose: the app runs as a different user than this script, and /var/lib for
# this project is 700. Nothing in here is sensitive -- counts and two booleans.
chmod 644 "$TMP"
mv "$TMP" "$STATE"
trap - EXIT
