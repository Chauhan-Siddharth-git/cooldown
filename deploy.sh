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


# --- what did we actually put on the box? -------------------------------------------
# Written at deploy time and verified hourly by cooldown-audit.sh. The gap this closes is
# the one that opened this whole review: app.py and addon.py sat a commit behind on the
# Pi with the CSP fail-open still live, while every other deployed file was current, so
# nothing looked wrong. "Correct in the repo" and "running on the box" are different
# claims and only one of them was ever checked.
#
# Recorded per file rather than as a single revision, because the two targets deploy
# different sets: `code` alone leaves units from an older revision, and a manifest that
# claimed otherwise would assert exactly the thing it exists to disprove.
MANIFEST=/var/lib/cooldown-deployed.manifest
REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
git diff --quiet HEAD 2>/dev/null || REV="$REV-dirty"

stamp(){   # each arg: "<installed path on box>=<local source file>"
    local new; new="$(mktemp)" || return 0
    local pair dst src
    for pair in "$@"; do
        dst="${pair%%=*}"; src="${pair#*=}"
        [ -f "$src" ] || continue
        printf '%s %s %s\n' "$(sha256sum "$src" | cut -d" " -f1)" "$REV" "$dst" >> "$new"
    done
    if [ ! -s "$new" ]; then rm -f "$new"; return 0; fi
    local cur; cur="$(mktemp)" || { rm -f "$new"; return 0; }
    "${SSH[@]}" "cat $MANIFEST 2>/dev/null" > "$cur" || true
    local merged; merged="$(mktemp)" || { rm -f "$new" "$cur"; return 0; }
    # Newest entry per path wins; merged locally so the box needs no tooling.
    python3 - "$cur" "$new" > "$merged" <<'PYEOF'
import sys
rows = {}
for f in sys.argv[1:]:
    for line in open(f, encoding="utf-8", errors="replace"):
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows[parts[2].strip()] = (parts[0], parts[1])
for path in sorted(rows):
    h, rev = rows[path]
    print(f"{h} {rev} {path}")
PYEOF
    # mktemp on the box, not a fixed /tmp name (F17). This file is installed as root, so a
    # predictable path in a world-writable directory can be pre-created and swapped between
    # the scp and the install -- turning a manifest write into an arbitrary root write. The
    # units target already stages this way; the pre-commit hook caught this one.
    local dest; dest="$("${SSH[@]}" 'mktemp /tmp/.deployed-manifest.XXXXXXXX')"
    if [ -n "$dest" ]; then
        scp -q -o BatchMode=yes "$merged" "$PI:$dest" \
            && "${SSH[@]}" "sudo install -m644 $dest $MANIFEST; rm -f $dest" \
            && echo "stamped     $(wc -l < "$merged") file(s) at $REV"
    fi
    rm -f "$new" "$cur" "$merged"
}

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
        deploy/cooldown-alert-test.sh deploy/cooldown-cawatch.sh \
        deploy/cooldown-journald.conf "$PI:$STAGE/"
    # -D on the journald drop-in: journald.conf.d/ exists on this box but not on a fresh
    # image, and an install that fails there would leave the box on volatile storage while
    # the deploy reported success.
    "${SSH[@]}" "sudo install -m644 $STAGE/cooldown-*.service $STAGE/cooldown-*.timer /etc/systemd/system/ &&
                 sudo install -m755 $STAGE/cooldown-redirect.sh /usr/local/sbin/cooldown-redirect.sh &&
                 sudo install -m755 $STAGE/cooldown-updates.sh /usr/local/sbin/cooldown-updates.sh &&
                 sudo install -m755 $STAGE/cooldown-audit.sh /usr/local/sbin/cooldown-audit.sh &&
                 sudo install -m755 $STAGE/cooldown-verify-backup.py /usr/local/sbin/cooldown-verify-backup.py &&
                 sudo install -m755 $STAGE/cooldown-alert-test.sh /usr/local/sbin/cooldown-alert-test.sh &&
                 sudo install -m755 $STAGE/cooldown-cawatch.sh /usr/local/sbin/cooldown-cawatch.sh &&
                 sudo install -D -m644 $STAGE/cooldown-journald.conf /etc/systemd/journald.conf.d/50-cooldown-persistent.conf &&
                 rm -rf $STAGE &&
                 sudo systemctl daemon-reload && echo 'units installed + daemon-reloaded'"
    stamp /usr/local/sbin/cooldown-redirect.sh=deploy/cooldown-redirect.sh \
          /usr/local/sbin/cooldown-updates.sh=deploy/cooldown-updates.sh \
          /usr/local/sbin/cooldown-audit.sh=deploy/cooldown-audit.sh \
          /usr/local/sbin/cooldown-cawatch.sh=deploy/cooldown-cawatch.sh \
          /usr/local/sbin/cooldown-alert-test.sh=deploy/cooldown-alert-test.sh \
          /usr/local/sbin/cooldown-verify-backup.py=deploy/cooldown-verify-backup.py
    echo "NOTE: restart services yourself if a unit changed (sudo systemctl restart <svc>)."
    echo "NOTE: journald.conf.d is NOT covered by daemon-reload. If cooldown-journald.conf"
    echo "      changed:  sudo systemctl restart systemd-journald"
    echo "      then verify the OUTCOME, not the setting -- 'journalctl --disk-usage' against"
    echo "      the oldest entry's timestamp is the window; the config file is only a claim."
    ;;

  code)
    restart=()
    # rotate-ca.sh and the two files it calls. These had NO deployment path at all: a box
    # provisioned earlier could be carrying a rotate-ca.sh from before gen_ca.sh existed,
    # which deleted the CA and restarted mitmproxy -- letting it self-generate an
    # UNCONSTRAINED replacement. Running it would break every trusted device and hand back
    # exactly the CA F26 exists to prevent, while looking like it had fixed it. `units`
    # installs system files to /usr/local/sbin and `code` shipped four repo files; the
    # rotation tooling fell between them and nothing noticed, because a stale copy on the
    # box is present and executable and therefore looks deployed.
    "${SSH[@]}" "mkdir -p $DIR/deploy"
    for f in app.py addon.py PLAN.md PI-SETUP.md \
             rotate-ca.sh deploy/gen_ca.sh deploy/gen_allow_hosts.py news_domains.py; do
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
        # Executables must land executable. scp does not carry the mode across, and a
        # rotate-ca.sh that arrives 644 fails at the moment you need it most.
        case "$f" in
            *.sh) "${SSH[@]}" "chmod +x $DIR/$f" ;;
        esac
        echo "deployed   $f"
        case "$f" in
            app.py)   restart+=(cooldown-app.service) ;;
            addon.py) restart+=(cooldown-proxy.service) ;;
        esac
    done
    stamp "$DIR/app.py=app.py" "$DIR/addon.py=addon.py" \
          "$DIR/rotate-ca.sh=rotate-ca.sh" "$DIR/deploy/gen_ca.sh=deploy/gen_ca.sh" \
          "$DIR/deploy/gen_allow_hosts.py=deploy/gen_allow_hosts.py" \
          "$DIR/news_domains.py=news_domains.py"
    if [ "${#restart[@]}" -gt 0 ]; then
        echo "Restarting: ${restart[*]}"
        "${SSH[@]}" "sudo systemctl restart ${restart[*]} && sleep 2"
    fi
    status
    ;;

  *) echo "usage: $0 [code|units|status]" >&2; exit 1 ;;
esac
