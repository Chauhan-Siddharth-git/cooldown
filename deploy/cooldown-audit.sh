#!/bin/bash
# Security and liveness invariants, in two tiers.
#
#   cooldown-audit.sh quick   hourly  -- everything cheap (~30ms total)
#   cooldown-audit.sh full    weekly  -- quick, plus dpkg -V (67 SECONDS on this box)
#
# The split exists because one check was setting the cadence for all of them. Verifying
# every packaged file's checksum takes 67 seconds; checking that the CA has not been
# replaced takes 26 milliseconds. Running them together meant the CA was checked once a
# week when it could be checked 168 times.
#
# Reports, never repairs. A watchdog that silently deleted an unexpected SSH key would
# destroy the evidence that mattered.
#
# WHAT THIS CANNOT DO, so it is not mistaken for more than it is:
#   - It cannot tell you the CA key was READ. Syscall auditing produces no events on
#     this kernel (tested with auditd: a watch on a scratch file logged nothing), and
#     the root filesystem is mounted noatime so access times record nothing either.
#     Copying the key leaves no trace. Detection is limited to the key being CHANGED.
#   - It cannot survive physical access. Whoever holds the SD card holds the CA, and
#     with no TPM there is no way to encrypt it that still lets the box boot unattended.
set -u

MODE="${1:-quick}"
STATE=/var/lib/cooldown-audit.json
PIN=/var/lib/cooldown-audit-baseline
TMP="$(mktemp /var/lib/.cooldown-audit.XXXXXX)" || exit 1
trap 'rm -f "$TMP"' EXIT

CA_DIR=/var/lib/cooldown/mitmproxy
CA_CERT="$CA_DIR/mitmproxy-ca-cert.pem"
# Not project-named: backup.py defaults to this and neither deployment overrides
# COOLDOWN_BACKUP_DIR, so the path is identical in both repos. Guessing a
# project-named path instead produced a "no backups found" alarm on a box that had
# been backing up nightly all along.
BACKUP_DIR=/var/backups/cooldown
findings=()
note() { findings+=("$1"); logger -t cooldown-audit "$1"; }
days_until() { echo $(( ( $1 - $(date +%s) ) / 86400 )); }

# --- the trust anchor -------------------------------------------------------------
if [ -d "$CA_DIR" ]; then
    ca_mode="$(stat -c '%a' "$CA_DIR")"
    ca_owner="$(stat -c '%U' "$CA_DIR")"
    ca_mtime="$(stat -c '%Y' "$CA_DIR/mitmproxy-ca.pem" 2>/dev/null || echo 0)"
    [ "$ca_mode" = "700" ] || note "CA directory is mode $ca_mode, expected 700"
    [ "$ca_owner" = "cooldownproxy" ] || note "CA directory owned by $ca_owner, expected cooldownproxy"

    # Fingerprint, not just mtime. Anyone who swaps the CA can restore a timestamp with
    # `touch -r` in one command; they cannot make a different key hash the same.
    ca_fp="$(openssl x509 -in "$CA_CERT" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
    if [ -s "$PIN" ]; then
        pinned="$(cat "$PIN")"
        [ "$ca_fp" = "$pinned" ] || note "CA FINGERPRINT CHANGED -- was ${pinned:0:20}..., now ${ca_fp:0:20}..."
    elif [ -n "$ca_fp" ]; then
        printf '%s' "$ca_fp" > "$PIN"; chmod 600 "$PIN"
        logger -t cooldown-audit "pinned CA fingerprint ${ca_fp:0:20}... (first run)"
    fi

    # The CA expires. When it does, every gated site breaks at once with a certificate
    # error and no obvious cause.
    ca_end="$(openssl x509 -in "$CA_CERT" -noout -enddate 2>/dev/null | cut -d= -f2)"
    ca_days=$(days_until "$(date -d "$ca_end" +%s 2>/dev/null || echo 0)")
    [ "$ca_days" -gt 90 ] || note "CA certificate expires in $ca_days days -- rotate it (rotate-ca.sh)"
else
    ca_mode="missing"; ca_owner="missing"; ca_mtime=0; ca_fp=""; ca_days=0
    note "CA directory $CA_DIR is missing"
fi

# --- who can log in ---------------------------------------------------------------
shell_accounts="$(awk -F: '$7 !~ /(nologin|false)$/ {print $1}' /etc/passwd | sort | tr '\n' ' ')"
key_count=0
for f in /home/*/.ssh/authorized_keys /root/.ssh/authorized_keys; do
    # NOT `$(grep -c ... || echo 0)`. grep -c ALREADY prints 0 when it matches nothing,
    # and exits 1 while doing it -- so the fallback fired on top of the 0 grep had just
    # printed, the substitution became two lines, and $(( )) died with "syntax error in
    # expression". Observed in the journal every hour: the arithmetic aborted, key_count
    # kept its old value, and the SSH key count published at the bottom of this script
    # silently undercounted. A file with no keys made the whole tally wrong.
    #
    # Same shape as the `grep | head || echo` mistake this project has now made three
    # times: reaching for a fallback on a command whose failure exit status does not mean
    # what it looks like. grep -c's exit code reports "found nothing", not "could not
    # count" -- and "found nothing" is a successful count of zero.
    [ -f "$f" ] || continue
    n="$(grep -c '^ssh-\|^ecdsa-\|^sk-' "$f" 2>/dev/null)"
    key_count=$((key_count + ${n:-0}))
done
pw_auth="$(sshd -T 2>/dev/null | awk '/^passwordauthentication/{print $2}')"
[ "${pw_auth:-no}" = "no" ] || note "sshd now accepts password authentication"

# Only meaningful since the journal became persistent. Before that this was always zero
# because there was nothing to count -- which is exactly how an empty result got
# reported as a clean one.
failed_auth="$(journalctl -u ssh --since '24 hours ago' --no-pager 2>/dev/null \
               | grep -ciE 'Failed password|Invalid user|authentication failure' || true)"
[ "${failed_auth:-0}" -lt 20 ] || note "$failed_auth failed SSH auth attempts in 24h"

# --- the tailnet is now the only way in -------------------------------------------
ts_expiry="$(tailscale status --json 2>/dev/null \
             | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"].get("KeyExpiry") or "")' 2>/dev/null || true)"
ts_days=-1
if [ -n "$ts_expiry" ]; then
    ts_days=$(days_until "$(date -d "$ts_expiry" +%s 2>/dev/null || echo 0)")
    [ "$ts_days" -gt 30 ] || note "Tailscale key expires in $ts_days days -- port 22 is tailnet-only, so expiry means no remote access at all"
fi

# --- what is listening, and is the firewall loaded --------------------------------
listeners="$(ss -tlnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -un | tr '\n' ' ')"
fw_rules="$(iptables -S INPUT 2>/dev/null | grep -c 'multiport\|tailscale0')"
[ "$fw_rules" -gt 0 ] || note "no interface-scoped INPUT rules found -- the firewall is not loaded"

# Exposure depends on the POLICY, not just on which ports a rule names. Under the old
# default-ACCEPT this was "any wildcard listener nobody wrote a rule for"; the moment the
# policy became DROP that inverted -- an unnamed port is now blocked, not open. Verified
# by binding a listener on 9099 and confirming it was unreachable while the old logic
# still called it exposed.
# Both families. The listener set below comes from `ss -tlnH`, which reports [::] binds,
# so judging them against IPv4 rules alone cleared the internet-facing family on evidence
# from the other one -- and this box holds a globally routable v6 address with mitmdump
# bound to [::]. The two chains do not agree today (5 ACCEPT rules on v4, 7 on v6), so
# this was latent only by luck.
#
# A port is judged per family and the harsher verdict wins: exposed on either family is
# exposed. Anything else lets a v4-only rule vouch for a v6 listener.
fw_ports() {   # $1 = iptables|ip6tables, $2 = ACCEPT|DROP
    # Two fixes in this pipeline:
    #
    #  - `! -i tailscale0 ... -j ACCEPT` accepts from everywhere EXCEPT the tailnet. The
    #    old `grep -vE -- '-i (lo|tailscale0)'` matched the substring and discarded it as
    #    interface-scoped, i.e. read the most permissive rule shape as the safest one.
    #    Negated matches are now kept explicitly.
    #  - `--dport 8080` (singular) was invisible: only the multiport `--dports` form was
    #    matched, so a plain wide-open ACCEPT classified the port as contained.
    "$1" -S INPUT 2>/dev/null \
        | awk -v want="-j $2" '
            index($0, want) == 0 { next }
            /! -i/               { print; next }          # negated: NOT interface-scoped
            /-i (lo|tailscale0)/ { next }                 # genuinely scoped to a safe iface
                                 { print }' \
        | grep -oE -- '--dports? [0-9,]+' \
        | tr ',' '\n' | grep -oE '[0-9]+' | sort -u | tr '\n' ' '
}

fw_policy="$(iptables -S INPUT 2>/dev/null | awk '/^-P INPUT/{print $3}')"
fw_policy6="$(ip6tables -S INPUT 2>/dev/null | awk '/^-P INPUT/{print $3}')"
fw_open="$(fw_ports iptables ACCEPT)"
fw_open6="$(fw_ports ip6tables ACCEPT)"
fw_dropped="$(fw_ports iptables DROP)"
fw_dropped6="$(fw_ports ip6tables DROP)"

# Keep the family with the port: [::]:8080 and 0.0.0.0:8080 are different questions.
wildcard="$(ss -tlnH 2>/dev/null | awk '$4 ~ /^(0\.0\.0\.0|\[::\]):/ {print $4}' | sort -u)"
exposed=""; contained=""
for w in $wildcard; do
    p="${w##*:}"
    case "$w" in
        "[::]"*) pol="$fw_policy6"; opn="$fw_open6"; drp="$fw_dropped6"; fam=v6 ;;
        *)       pol="$fw_policy";  opn="$fw_open";  drp="$fw_dropped";  fam=v4 ;;
    esac
    open=no
    case " $opn " in *" $p "*) open=yes ;; esac
    if [ "$pol" = "DROP" ]; then
        # Default-deny: reachable only if something explicitly opened it wide.
        [ "$open" = yes ] && exposed="$exposed$p/$fam " || contained="$contained$p "
    else
        # Default-allow: contained only if a DROP names it and nothing opened it wide.
        case " $drp " in
            *" $p "*) [ "$open" = yes ] && exposed="$exposed$p/$fam " || contained="$contained$p " ;;
            *) exposed="$exposed$p/$fam " ;;
        esac
    fi
done

# The harsher verdict wins across families. Without this a port exposed on v6 and
# contained on v4 appears in BOTH lists, and /health renders the contained one as a
# "firewalled" badge -- a false safety claim built from half the evidence, which is
# the exact failure this whole item is about.
# A for-loop, not `printf '%s\n' $var`: with an empty variable printf emits a bare
# newline, which tr turns into a single space, which is not empty -- so the guard below
# fired and the audit reported "reachable from off-box: " with nothing after the colon.
# A check that invents findings gets ignored as fast as one that misses them.
exposed="$(for x in $exposed; do echo "$x"; done | sort -u | tr '\n' ' ')"
_bare="$(for x in $exposed; do echo "${x%%/*}"; done | sort -u)"
_kept=""
for c in $(for x in $contained; do echo "$x"; done | sort -u); do
    case " $(printf '%s ' $_bare) " in *" $c "*) continue ;; esac
    _kept="$_kept$c "
done
contained="$_kept"
[ -z "$exposed" ] || note "reachable from off-box and not contained by the firewall: $exposed"
[ "$fw_policy" = "DROP" ]  || note "INPUT policy is $fw_policy, not DROP -- new listeners are exposed by default"
[ "$fw_policy6" = "DROP" ] || note "IPv6 INPUT policy is ${fw_policy6:-unreadable}, not DROP -- v6 was previously never checked at all"

# --- is anything still being written down -----------------------------------------
journal_persistent=false
[ "$(find /var/log/journal -name '*.journal' 2>/dev/null | wc -l)" -gt 0 ] && journal_persistent=true
[ "$journal_persistent" = true ] || note "journal is not persisting -- a future audit will have no history to read"

# Redis durability, asked of the RUNNING server rather than of the config file. AOF was
# recorded as done back when this project was planned around Docker, where the route to it
# was mounting the shipped redis.conf as a persistent volume. That deployment was later
# dropped in favour of running natively, and the durability requirement went with its
# carrier -- nobody re-derived what "harden state" meant once the shape changed. The
# redis.conf in this repo is still not installed by anything and still points at /data, a
# path that exists only inside a container.
#
# Asking the server, not the file, is the point: `appendonly yes` in redis.conf proves
# somebody typed it, while aof_enabled proves it is in force. A package upgrade rewriting
# the config would leave the first true and the second false.
aof="$(redis-cli info persistence 2>/dev/null | sed -n 's/^aof_enabled:\([0-9]*\).*/\1/p')"
if [ -z "$aof" ]; then
    note "could not read Redis persistence state -- durability is unverified, not fine"
elif [ "$aof" != "1" ]; then
    note "Redis AOF is OFF -- an unclean stop loses every write since the last RDB snapshot"
fi

# A backup that quietly stopped is the classic silent failure: you find out when you
# need it. The timer runs nightly, so anything older than two days has stopped.
backup_age=-1
newest="$(ls -t "$BACKUP_DIR" 2>/dev/null | head -1)"
if [ -n "$newest" ]; then
    backup_age=$(( ( $(date +%s) - $(stat -c %Y "$BACKUP_DIR/$newest") ) / 86400 ))
    [ "$backup_age" -le 2 ] || note "newest backup is $backup_age days old -- the nightly backup has stopped"
    [ -s "$BACKUP_DIR/$newest" ] || note "newest backup $newest is empty"
else
    note "no backups found in $BACKUP_DIR"
fi

# Kernels accumulate now that unattended upgrades actually install them.
root_pct="$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')"
boot_pct="$(df --output=pcent /boot/firmware 2>/dev/null | tail -1 | tr -dc '0-9')"
[ "${root_pct:-0}" -lt 85 ] || note "root filesystem ${root_pct}% full"
[ "${boot_pct:-0}" -lt 85 ] || note "boot partition ${boot_pct}% full -- old kernels may not be getting removed"

# --- are the features that record things still recording? -------------------------
# Every check above asks whether a FILE matches. This asks whether a BEHAVIOUR still
# happens, which is the class nothing here covered: the reflection prompt recorded
# nothing from 2026-08-27 to 09-11 while entries ran at 11-14 a day (a CSS rule had
# un-hidden the Continue button), and the worth verdict recorded nothing for the five
# days after that while cooldowns fired (three blocked screens never asked). Both were
# found by a person noticing something felt off. A feature that has stopped recording
# looks exactly like one nobody used -- unless it is compared against the thing that
# proves it WAS used. Each pair below is that comparison.
#
# Day keys are read in the accounting zone (tz_accounting), not the box's: while
# travelling the two differ, and looking yesterday up under the wrong date would
# manufacture the very "nothing recorded" this exists to detect. An EMPTY TZ means UTC
# to GNU date, so the variable is only applied when set. Non-digits are stripped
# before arithmetic so a redis error reads as 0 -- toward the alarm, as elsewhere here.
acct_tz="$(redis-cli --raw GET tz_accounting 2>/dev/null)"
dkey() { if [ -n "$acct_tz" ]; then TZ="$acct_tz" date -d "$1 days ago" +%F; else date -d "$1 days ago" +%F; fi; }
_num() { local v="${1//[^0-9]/}"; echo "${v:-0}"; }
sum_llen() { local t=0 i; for i in $(seq 0 $(( $2 - 1 ))); do t=$(( t + $(_num "$(redis-cli --raw LLEN "$1:$(dkey "$i")" 2>/dev/null)") )); done; echo "$t"; }
sum_get()  { local t=0 i; for i in $(seq 0 $(( $2 - 1 ))); do t=$(( t + $(_num "$(redis-cli --raw GET  "$1:$(dkey "$i")" 2>/dev/null)") )); done; echo "$t"; }

# Reflection: shown on ~70% of entries after the first each day, so nine entries over
# three days should have produced several answers. Zero is a broken prompt, not a quiet
# week -- a quiet week has zero entries too, and is not flagged.
entries3="$(sum_get entries 3)"; reflect3="$(sum_llen reflect 3)"
[ "$entries3" -lt 9 ] || [ "$reflect3" -gt 0 ] || \
    note "reflection prompt recorded nothing in 3 days across $entries3 entries -- not being asked, or not answerable"

# The dead-man's switch. Unlike everything else on this list it cannot fail silently --
# if the pings stop, the external service raises the alarm, so sabotage and triggering are
# the same event. What CAN fail silently is the switch never having been set up, or the
# timer being disabled while the state file keeps an old "ok" from last week. Both look
# like health from here, so both are checked.
dm_state="$(cat /var/lib/cooldown-deadman.state 2>/dev/null)"
dm_ok=false
dm_age=-1
if [ -z "$dm_state" ]; then
    note "the dead-man's switch has never run -- nothing off this box would notice it going quiet"
elif [ "${dm_state#* }" = "not-configured" ]; then
    note "the dead-man's switch has no URL configured -- see deploy/cooldown-deadman.service"
elif [ "${dm_state#* }" != "ok" ]; then
    note "the dead-man's switch ping is FAILING (${dm_state#* }) -- the far end will fire, which is correct, but fix the cause"
else
    dm_ts="$(_num "${dm_state%% *}")"
    if [ "$dm_ts" -le 0 ]; then
        note "the dead-man's switch state file is unreadable ('$dm_state') -- unknown, not fine"
    else
        dm_age=$(( $(date +%s) - dm_ts ))
        # It runs every 5 minutes. Twenty means it has stopped running, which the far end
        # is about to notice anyway -- but hearing it here first is the difference between
        # fixing a timer and being woken by an alarm.
        if [ "$dm_age" -gt 1200 ]; then
            note "the dead-man's switch last pinged $(( dm_age / 60 )) minutes ago -- the timer has stopped"
        else
            dm_ok=true
        fi
    fi
fi

# The off-box alert channel. Distinguishes BROKEN from merely UNPROVEN, because they are
# different problems: the first is knowable now, the second only means nothing has needed
# saying. Reporting the second as the first is how a check becomes noise.
#
# This matters more since planned reboots stopped alerting: absence of a notification now
# means "no unplanned reboot", and a dead channel makes absence mean nothing while still
# reading as calm. app.py sends a min-priority probe weekly so "worked recently" stays a
# fact rather than an assumption.
alert_raw="$(redis-cli --raw GET alert_last 2>/dev/null)"
alert_ok=false
alert_age=-1
if [ -z "$alert_raw" ]; then
    note "the alert channel has never sent anything -- an unplanned reboot would reach nobody"
else
    alert_ts="$(_num "${alert_raw%% *}")"
    alert_outcome="${alert_raw#* }"
    if [ "$alert_outcome" != "ok" ]; then
        note "the last off-box alert FAILED ($alert_outcome) -- notifications are down"
    elif [ "$alert_ts" -gt 0 ]; then
        alert_age=$(( ( $(date +%s) - alert_ts ) / 86400 ))
        if [ "$alert_age" -gt 9 ]; then
            note "no alert has succeeded in $alert_age days -- the weekly probe should keep this under 8, so the channel is unproven rather than quiet"
        else
            alert_ok=true
        fi
    else
        # An unparseable timestamp leaves the channel in an UNKNOWN state, and unknown
        # must never fall through quietly: the first version of this check did exactly
        # that, reporting nothing while alert_ok stayed false, so a corrupt key read
        # identically to a healthy one.
        note "alert_last is unreadable ('$alert_raw') -- the channel's state is unknown, not fine"
    fi
fi

# Worth verdict: asked on every screen you land on because a session ended, which a
# cooldown always is. Three cooldowns with no verdict means the question is not reaching
# a screen -- or reaching one that cannot submit it.
cooldowns5="$(sum_llen cooldown_events 5)"; worth5="$(sum_llen worth 5)"
[ "$cooldowns5" -lt 3 ] || [ "$worth5" -gt 0 ] || \
    note "no worth verdict in 5 days across $cooldowns5 cooldowns -- the question is not reaching a screen you land on"

# --- does the RUNNING BOX match what the repo claims? ------------------------------
# The category this file was missing. Every check above asks "is the box in a safe
# state"; these ask "is the box the thing we think we deployed". That is a different
# question, and it is the one this project keeps failing:
#
#   F11 enabled != working.   F21 a timer firing != its job running.
#   F22 documented != true.   F26 correct in the repo != applied to the key.
#   F27 checked on v4 != checked.  F29 ran != succeeded.
#
# Each was found once, by hand, after the fact. The through-line is that intent lived in
# the repo and reality lived on the box and nothing compared them. This section is that
# comparison, and it runs hourly.

# 1. The deploy manifest: is every file we shipped still byte-identical, and from what?
MANIFEST=/var/lib/cooldown-deployed.manifest
deployed_rev="unknown"; drifted=0; manifest_files=0; revs=""
if [ -r "$MANIFEST" ]; then
    while read -r want rev path; do
        [ -n "${path:-}" ] || continue
        manifest_files=$((manifest_files + 1))
        # COLLECT the revisions, do not overwrite. `deployed_rev="$rev"` inside the loop
        # reported whichever path happened to sort last -- one arbitrary file's revision,
        # printed on /health as though it described the box. Caught immediately after
        # shipping this section, when the dashboard said d8af11a while HEAD was c6e6402
        # and six files were from each: `units` and `code` stamp different sets, which is
        # the whole reason the manifest is per-file. A single authoritative-looking number
        # that is not authoritative is the exact defect this section exists to catch.
        case " $revs " in *" $rev "*) ;; *) revs="$revs $rev" ;; esac
        got="$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1)"
        if [ -z "$got" ]; then
            note "deployed file is MISSING: $path (manifest: $rev)"
            drifted=$((drifted + 1))
        elif [ "$got" != "$want" ]; then
            note "deployed file CHANGED since it was deployed: $path"
            drifted=$((drifted + 1))
        fi
    done < "$MANIFEST"
    # An empty manifest reconciles nothing while looking like a clean pass -- the exact
    # shape this whole section exists to catch, so it is called out rather than assumed.
    [ "$manifest_files" -gt 0 ] || note "deploy manifest is EMPTY -- nothing was reconciled"
    set -- $revs
    if [ "$#" -eq 1 ]; then
        deployed_rev="$1"
    elif [ "$#" -gt 1 ]; then
        # Not a finding: `code` and `units` are deployed separately and units rarely
        # change, so a mixed box is routine. It must still be VISIBLE, because "running
        # <one rev>" would be a claim the manifest cannot support.
        deployed_rev="mixed($#): $*"
    fi
else
    note "no deploy manifest at $MANIFEST -- cannot tell whether the box matches the repo"
fi
case "$deployed_rev" in
    *-dirty) note "deployed from a DIRTY working tree ($deployed_rev) -- the revision does not describe what is running" ;;
esac

# 2. The CA carries name constraints -- not merely exists. The fingerprint pin above
# proves the key has not been SWAPPED; it says nothing about what the key may vouch for.
# F26 was marked FIXED for three days while the live CA was unconstrained, and the pin
# was green throughout because the unconstrained key was exactly the one it had pinned.
ca_nc=0
if [ -r "$CA_CERT" ]; then
    ca_nc="$(openssl x509 -in "$CA_CERT" -noout -text 2>/dev/null | grep -c 'X509v3 Name Constraints' || true)"
    [ "${ca_nc:-0}" -gt 0 ] || note "the CA has NO name constraints -- a stolen key can vouch for any host (F26)"
fi

# 3. Every unit's ExecStart points at a file that exists and is executable. A unit whose
# program was never installed fails with 203/EXEC at the moment you need it, and looks
# perfectly installed until then -- cooldown-cawatch.service shipped that way.
missing_exec=0
for u in /etc/systemd/system/cooldown-*.service; do
    [ -e "$u" ] || continue
    while read -r prog; do
        case "$prog" in /usr/local/*|/usr/bin/*|/home/pi/*) ;; *) continue ;; esac
        if [ ! -x "$prog" ]; then
            note "$(basename "$u") ExecStart points at $prog which is not executable/present"
            missing_exec=$((missing_exec + 1))
        fi
    done <<EOF2
$(sed -n 's/^ExecStart=[-@+!]*\([^ ]*\).*/\1/p' "$u")
EOF2
done

# 4. Units that declare [Install] are actually enabled. Installing a unit file and
# enabling it are different acts, and `deploy.sh units` only does the first -- it
# daemon-reloads and says so, which reads like completion.
not_enabled=0
for u in /etc/systemd/system/cooldown-*.service /etc/systemd/system/cooldown-*.timer; do
    [ -e "$u" ] || continue
    grep -q '^\[Install\]' "$u" || continue          # no [Install] = not meant to be enabled
    n="$(basename "$u")"
    case "$(systemctl is-enabled "$n" 2>/dev/null)" in
        enabled|enabled-runtime|static|indirect) ;;
        *) note "$n declares [Install] but is not enabled -- it will not start on boot"
           not_enabled=$((not_enabled + 1)) ;;
    esac
done

# 5. Every timer has a next elapse. F21's lesson generalised: a timer with Trigger: n/a
# reports active and enabled and will never fire again.
dead_timers=0
for t in /etc/systemd/system/cooldown-*.timer; do
    [ -e "$t" ] || continue
    n="$(basename "$t")"
    systemctl is-active --quiet "$n" || continue
    nrt="$(systemctl show "$n" -p NextElapseUSecRealtime --value 2>/dev/null)"
    nmo="$(systemctl show "$n" -p NextElapseUSecMonotonic --value 2>/dev/null)"
    if [ -z "$nrt" ] && { [ -z "$nmo" ] || [ "$nmo" = "0" ]; }; then
        note "$n is active but has NO next elapse -- it will never fire again"
        dead_timers=$((dead_timers + 1))
    fi
done

# 6. The INPUT policy really is DROP, on BOTH families. fw_policy is read above, but only
# to decide whether a listener counts as exposed -- its VALUE was never asserted. F28's
# refusal path leaves the box at ACCEPT on purpose, and until tonight the caller threw
# that refusal away, so "we set DROP" was an assumption with nothing behind it.
for fam in "v4:${fw_policy:-unknown}" "v6:${fw_policy6:-unknown}"; do
    case "${fam#*:}" in
        DROP) ;;
        *) note "INPUT policy on ${fam%%:*} is ${fam#*:}, expected DROP" ;;
    esac
done


# --- the expensive tier -----------------------------------------------------------
KNOWN_MODIFIED='/usr/lib/modprobe.d/g_ether.conf'

# Carry forward what the WEEKLY tier established. The hourly run does not perform these
# checks, and writing -1 for them erases a good result an hour after it was proved --
# "restore verified" was visible for one hour in every 168, which is indistinguishable
# from never. Unknown must stay unknown; proven must stay proven until re-tested, with
# full_checked recording when, so a carried result cannot pose as fresh forever.
prev() {
    [ -r "$STATE" ] || { echo -1; return; }
    python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], -1))
except Exception: print(-1)' "$STATE" "$1" 2>/dev/null || echo -1
}
tampered="$(prev tampered_files)"
tampered_all="$(prev tampered_all)"
backup_restores="$(prev backup_restores)"
full_checked="$(prev full_checked)"
[ "$full_checked" = "-1" ] && full_checked=0

if [ "$MODE" = "full" ]; then
    full_checked="$(date +%s)"
    # Prove the newest backup goes back in. Weekly, not hourly -- it restores every key
    # into a scratch database, which is cheap but not free. Never touches db 0.
    # The venv interpreter, not the system one: redis-py is installed only in the venv,
    # so `python3 verify.py` dies with ModuleNotFoundError and the audit sees an empty
    # string rather than a result.
    PYBIN=/home/pi/cooldown/venv/bin/python3
    [ -x "$PYBIN" ] || PYBIN=python3
    if vb="$("$PYBIN" /usr/local/sbin/cooldown-verify-backup.py 2>/dev/null)"; then
        backup_restores=1
    else
        backup_restores=0
        note "backup does NOT restore: $(printf '%s' "$vb" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error") or "unknown")' 2>/dev/null || echo 'verifier failed to run')"
    fi

    # dpkg -V costs 67 seconds on this box -- measured 2026-09-15, quick tier 2s, whole
    # full tier 68s, so this call IS the weekly audit -- and
    # it was being run twice, once for the raw count and once for the filtered one. The
    # weekly tier therefore spent a minute and a half establishing what a single pass
    # already knew. Run it once; derive both counts from the result.
    #
    # Counted with awk rather than `grep -c`: grep -c exits 1 on no match HAVING ALREADY
    # PRINTED 0, which is the footgun this project has now written three separate times
    # (see the historical-mistakes note in CLAUDE.md). `awk END{print n+0}` prints 0 and
    # exits 0, so the count means the same thing whether or not anything matched.
    mods="$(dpkg -V 2>/dev/null | awk 'NF && $2 != "c" {print $NF}')"
    nlines() { printf '%s' "$1" | awk 'NF {n++} END {print n+0}'; }
    tampered_all="$(nlines "$mods")"
    tampered="$(nlines "$(printf '%s' "$mods" | grep -vxF "$KNOWN_MODIFIED")")"
    [ "$tampered" -eq 0 ] || note "$tampered packaged file(s) differ from their manifest (dpkg -V)"

    # Rule 10: every list of exceptions needs something checking the exceptions still
    # exist. KNOWN_MODIFIED is the fourth exemption list in this project and was the only
    # one with nothing watching it -- the other three each accumulated entries that
    # matched nothing, one within hours of being written. An exemption that no longer
    # exempts anything is indistinguishable from one that quietly permits everything.
    #
    # A here-string, not a pipe: note() appends to the `findings` array, and a `while` on
    # the right of a pipe runs in a subshell, so every finding raised in it would be
    # discarded at the closing `done` -- a check that cannot report is not a check.
    while IFS= read -r ex; do
        [ -n "$ex" ] || continue
        printf '%s' "$mods" | grep -qxF "$ex" || \
            note "KNOWN_MODIFIED excuses $ex, which dpkg no longer reports as modified -- stale exemption"
    done <<< "$KNOWN_MODIFIED"
fi

esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
{
printf '{"mode":"%s","ca_mode":"%s","ca_owner":"%s","ca_mtime":%d,"ca_fp":"%s","ca_days":%d,' \
       "$MODE" "$ca_mode" "$ca_owner" "${ca_mtime:-0}" "${ca_fp:0:16}" "${ca_days:-0}"
printf '"shell_accounts":"%s","ssh_keys":%d,"password_auth":"%s","failed_auth":%d,' \
       "$(esc "$shell_accounts")" "$key_count" "${pw_auth:-unknown}" "${failed_auth:-0}"
printf '"ts_days":%d,"listeners":"%s","firewall_rules":%d,"exposed_ports":"%s",' \
       "${ts_days:--1}" "$(esc "$listeners")" "$fw_rules" "$(esc "$exposed")"
printf '"fw_policy":"%s","fw_contained":"%s",' "${fw_policy:-unknown}" "$(esc "$contained")"
printf '"journal_persistent":%s,"backup_age":%d,"root_pct":%d,"boot_pct":%d,' \
       "$journal_persistent" "${backup_age:--1}" "${root_pct:-0}" "${boot_pct:-0}"
printf '"deployed_rev":"%s","deploy_drift":%d,"manifest_files":%d,"ca_constrained":%s,' \
       "$(esc "$deployed_rev")" "${drifted:-0}" "${manifest_files:-0}" \
       "$([ "${ca_nc:-0}" -gt 0 ] && echo true || echo false)"
printf '"missing_exec":%d,"not_enabled":%d,"dead_timers":%d,"fw_policy6":"%s",' \
       "${missing_exec:-0}" "${not_enabled:-0}" "${dead_timers:-0}" "${fw_policy6:-unknown}"
printf '"entries3":%d,"reflect3":%d,"cooldowns5":%d,"worth5":%d,' \
       "${entries3:-0}" "${reflect3:-0}" "${cooldowns5:-0}" "${worth5:-0}"
printf '"alert_ok":%s,"alert_age_days":%d,' "${alert_ok:-false}" "${alert_age:--1}"
printf '"deadman_ok":%s,"deadman_age":%d,' "${dm_ok:-false}" "${dm_age:--1}"
printf '"tampered_files":%d,"tampered_all":%d,"backup_restores":%d,"full_checked":%d,"findings":%d,"checked":%d}\n' \
       "${tampered:--1}" "${tampered_all:--1}" "${backup_restores:--1}" "${full_checked:-0}" "${#findings[@]}" "$(date +%s)"
} > "$TMP"

chmod 644 "$TMP"
mv "$TMP" "$STATE"
trap - EXIT

[ "${#findings[@]}" -eq 0 ] && logger -t cooldown-audit "$MODE audit clean"
exit 0
