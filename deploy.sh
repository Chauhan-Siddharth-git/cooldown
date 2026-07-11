#!/usr/bin/env bash
# Deploy budget-proxy to the Pi (native systemd + venv — no git/docker on the box).
#
#   ./deploy.sh            push app.py / addon.py / docs that changed, restart what's affected
#   ./deploy.sh units      push systemd units + redirect script from deploy/, daemon-reload
#   ./deploy.sh status     remote health check only
#
# The Pi is reached over Tailscale; override with PI=user@host ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

PI="${PI:-pi@raspberrypi.local}"
DIR=/home/pi/budget-proxy
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8 "$PI")

remote_md5() { "${SSH[@]}" "md5sum $DIR/$1 2>/dev/null | cut -d' ' -f1"; }
local_md5()  { md5sum "$1" | cut -d' ' -f1; }

status() {
    echo "--- services ---"
    "${SSH[@]}" 'systemctl is-active budget-app budget-proxy budget-redirect redis-server' \
        | paste <(printf '%s\n' budget-app budget-proxy budget-redirect redis-server) -
    echo "--- app ---"
    "${SSH[@]}" 'curl -sf --max-time 3 http://127.0.0.1:5000/remaining' && echo
}

case "${1:-code}" in
  status) status ;;

  units)
    echo "Pushing systemd units + redirect script..."
    scp -o BatchMode=yes deploy/budget-*.service "$PI:/tmp/"
    scp -o BatchMode=yes deploy/budget-redirect.sh "$PI:/tmp/"
    "${SSH[@]}" 'sudo install -m644 /tmp/budget-*.service /etc/systemd/system/ &&
                 sudo install -m755 /tmp/budget-redirect.sh /usr/local/sbin/budget-redirect.sh &&
                 rm -f /tmp/budget-*.service /tmp/budget-redirect.sh &&
                 sudo systemctl daemon-reload && echo "units installed + daemon-reloaded"'
    echo "NOTE: restart services yourself if a unit changed (sudo systemctl restart <svc>)."
    ;;

  code)
    restart=()
    for f in app.py addon.py PLAN.md PI-SETUP.md; do
        [ -f "$f" ] || continue
        if [ "$(local_md5 "$f")" = "$(remote_md5 "$f")" ]; then
            echo "unchanged  $f"
            continue
        fi
        "${SSH[@]}" "cp $DIR/$f $DIR/$f.bak-\$(date +%Y%m%d-%H%M) 2>/dev/null || true"
        scp -o BatchMode=yes "$f" "$PI:$DIR/$f"
        echo "deployed   $f"
        case "$f" in
            app.py)   restart+=(budget-app.service) ;;
            addon.py) restart+=(budget-proxy.service) ;;
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
