#!/bin/bash
# Hourly: keep apt's own timers honest, and record what is pending so /health can show it.
#
# Why the watchdog half exists. The box stopped patching itself for eleven days and
# nothing reported it -- silence read exactly like success, the same shape as the
# heartbeat dying.
#
# CORRECTED 2026-08-10, and the correction is the useful part. The first diagnosis was
# that the timers had stale Persistent= elapse points from the Pi's missing RTC, and the
# first fix cleared their stamp files. That was wrong, and worse than wrong: it made the
# timers report a healthy next elapse while changing nothing, because the actual fault
# was one level down. `userconf-pi` was sitting on tty1 asking for a username, so
# multi-user.target was NEVER reached after the 2026-07-30 boot, and every job ordered
# behind it -- apt-daily.service included -- queued forever. The timers fired on
# schedule. Their jobs simply never ran.
#
# The lesson is in the check below: a scheduled timer proves nothing about execution.
# The original version verified only that a next elapse existed, which was true
# throughout the outage. It would have reported all-clear every hour for eleven days.
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
    # An active timer with no scheduled elapse cannot be spotted with is-active or
    # is-enabled -- both report healthy, and `systemctl status` says "active (running)".
    # The only tell is Trigger: n/a. A plain `systemctl restart` does not clear it; the
    # persistent stamp file has to go so systemd recomputes from scratch.
    #
    # This is a real condition worth repairing, but it was NOT the cause of the 2026-08
    # outage -- see the header. Keeping it because it is cheap and the symptom is
    # invisible, not because it has ever fired in anger.
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

# --- half one-and-a-half: did the job actually RUN? --------------------------------
# The check the original version was missing, and the only one that would have caught
# the real outage. A timer can fire perfectly on schedule and still achieve nothing if
# its job then queues behind a target that is never reached. For eleven days that is
# exactly what happened, while every other signal here read healthy.
boot_ok=true
systemctl is-active --quiet multi-user.target || {
    boot_ok=false
    logger -t cooldown-updates "multi-user.target is NOT active -- boot never completed, queued jobs cannot run"
}

# Jobs sitting in 'waiting' are the generic form of the same jam.
jobs_waiting=$(systemctl list-jobs --no-pager 2>/dev/null | grep -c ' waiting' || true)

# The precise form: the timer fired, but the service it activates never started after
# that. Compare last trigger against the service's last actual start; an hour of slack
# absorbs a job that is legitimately mid-run.
stuck=0
for t in apt-daily.timer apt-daily-upgrade.timer; do
    svc="$(systemctl show "$t" -p Unit --value 2>/dev/null)"
    trig="$(systemctl show "$t" -p LastTriggerUSec --value 2>/dev/null)"
    ran="$(systemctl show "$svc" -p ExecMainStartTimestamp --value 2>/dev/null)"
    [ -n "$svc" ] && [ -n "$trig" ] && [ -n "$ran" ] || continue
    ts=$(date -d "$trig" +%s 2>/dev/null || echo 0)
    rs=$(date -d "$ran" +%s 2>/dev/null || echo 0)
    if [ "$ts" -gt 0 ] && [ "$((ts - rs))" -gt 3600 ]; then
        logger -t cooldown-updates "$t fired at $trig but $svc last ran $ran -- job queued, never executed"
        stuck=$((stuck + 1))
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
printf '"boot_ok":%s,"jobs_waiting":%d,"stuck_jobs":%d,' \
       "$boot_ok" "${jobs_waiting:-0}" "${stuck:-0}" >> "$TMP"
printf '"last_run":%d,"last_result":"%s","checked":%d}\n' \
       "${last_epoch:-0}" "${last_result:-unknown}" "$(date +%s)" >> "$TMP"

# 644 on purpose: the app runs as a different user than this script, and /var/lib for
# this project is 700. Nothing in here is sensitive -- counts and two booleans.
chmod 644 "$TMP"
mv "$TMP" "$STATE"
trap - EXIT
