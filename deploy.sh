#!/usr/bin/env bash
# Deploy Cooldown to the Pi (native systemd + venv — no git/docker on the box).
#
#   ./deploy.sh            push app.py / addon.py / docs that changed, restart what's affected
#   ./deploy.sh units      push systemd units + redirect script from deploy/, daemon-reload
#   ./deploy.sh status     remote health check only
#
# The Pi is reached over Tailscale; override with PI=user@host ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

PI="${PI:-pi@raspberrypi.local}"
DIR=/home/pi/cooldown
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8 "$PI")

remote_md5() { "${SSH[@]}" "md5sum $DIR/$1 2>/dev/null | cut -d' ' -f1"; }
local_md5()  { md5sum "$1" | cut -d' ' -f1; }

status() {
    echo "--- services ---"
    "${SSH[@]}" 'systemctl is-active cooldown-app cooldown-proxy cooldown-redirect redis-server' \
        | paste <(printf '%s\n' cooldown-app cooldown-proxy cooldown-redirect redis-server) -
    echo "--- app ---"
    "${SSH[@]}" 'curl -sf --max-time 3 http://127.0.0.1:5000/remaining' && echo
}

case "${1:-code}" in
  status) status ;;

  units)
    # Staged in a private mktemp dir, NOT /tmp directly. /tmp is world-writable, so
    # anything landing there under a predictable name can be swapped between the scp
    # and the install — and these files are installed as root into
    # /etc/systemd/system, which makes that swap a root shell. mktemp -d gives a
    # 0700 directory with an unguessable name.
    echo "Pushing systemd units + redirect script..."
    STAGE="$("${SSH[@]}" 'mktemp -d /tmp/cooldown-deploy.XXXXXXXX')"
    [ -n "$STAGE" ] || { echo "could not create a staging dir on $PI" >&2; exit 1; }
    trap '"${SSH[@]}" "rm -rf $STAGE" >/dev/null 2>&1 || true' EXIT
    # .timer as well as .service: the glob was *.service only, so cooldown-backup.timer sat
    # in the repo while the box ran whatever had been installed by hand. A deploy that
    # silently skips a file is worse than one that fails.
    scp -o BatchMode=yes deploy/cooldown-*.service deploy/cooldown-*.timer \
        deploy/cooldown-redirect.sh deploy/cooldown-updates.sh deploy/cooldown-audit.sh deploy/cooldown-verify-backup.py \
        deploy/cooldown-journald.conf "$PI:$STAGE/"
    # -D on the journald drop-in: journald.conf.d/ exists on this box but not on a fresh
    # image, and an install that fails there would leave the box on volatile storage while
    # the deploy reported success.
    "${SSH[@]}" "sudo install -m644 $STAGE/cooldown-*.service $STAGE/cooldown-*.timer /etc/systemd/system/ &&
                 sudo install -m755 $STAGE/cooldown-redirect.sh /usr/local/sbin/cooldown-redirect.sh &&
                 sudo install -m755 $STAGE/cooldown-updates.sh /usr/local/sbin/cooldown-updates.sh &&
                 sudo install -m755 $STAGE/cooldown-audit.sh /usr/local/sbin/cooldown-audit.sh &&
                 sudo install -m755 $STAGE/cooldown-verify-backup.py /usr/local/sbin/cooldown-verify-backup.py &&
                 sudo install -D -m644 $STAGE/cooldown-journald.conf /etc/systemd/journald.conf.d/50-cooldown-persistent.conf &&
                 rm -rf $STAGE &&
                 sudo systemctl daemon-reload && echo 'units installed + daemon-reloaded'"
    echo "NOTE: restart services yourself if a unit changed (sudo systemctl restart <svc>)."
    echo "NOTE: journald.conf.d is NOT covered by daemon-reload. If cooldown-journald.conf"
    echo "      changed:  sudo systemctl restart systemd-journald"
    echo "      then verify the OUTCOME, not the setting -- 'journalctl --disk-usage' against"
    echo "      the oldest entry's timestamp is the window; the config file is only a claim."
    ;;

  code)
    restart=()
    for f in app.py addon.py PLAN.md PI-SETUP.md; do
        [ -f "$f" ] || continue
        if [ "$(local_md5 "$f")" = "$(remote_md5 "$f")" ]; then
            echo "unchanged  $f"
            continue
        fi
        # Keep a rollback copy, but only the last few. Unbounded, this leaves one
        # snapshot per deploy forever.
        "${SSH[@]}" "cp $DIR/$f $DIR/$f.bak-\$(date +%Y%m%d-%H%M%S) 2>/dev/null || true;
                     ls -1t $DIR/$f.bak-* 2>/dev/null | tail -n +6 | xargs -r rm -f"
        scp -o BatchMode=yes "$f" "$PI:$DIR/$f"
        echo "deployed   $f"
        case "$f" in
            app.py)   restart+=(cooldown-app.service) ;;
            addon.py) restart+=(cooldown-proxy.service) ;;
        esac
    done
    if [ "${#restart[@]}" -gt 0 ]; then
        echo "Restarting: ${restart[*]}"
        "${SSH[@]}" "sudo systemctl restart ${restart[*]} && sleep 2"
    fi
    status
    ;;

  *) echo "usage: $0 [code|units|status]" >&2; exit 1 ;;
esac
