#!/bin/bash
# Security and liveness invariants, in two tiers.
#
#   budget-audit.sh quick   hourly  -- everything cheap (~30ms total)
#   budget-audit.sh full    weekly  -- quick, plus dpkg -V (43 SECONDS on this box)
#
# The split exists because one check was setting the cadence for all of them. Verifying
# every packaged file's checksum takes 43 seconds; checking that the CA has not been
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
STATE=/var/lib/budget-audit.json
PIN=/var/lib/budget-audit-baseline
TMP="$(mktemp /var/lib/.budget-audit.XXXXXX)" || exit 1
trap 'rm -f "$TMP"' EXIT

CA_DIR=/var/lib/cooldown/mitmproxy
CA_CERT="$CA_DIR/mitmproxy-ca-cert.pem"
# Not project-named: backup.py defaults to this and neither deployment overrides
# COOLDOWN_BACKUP_DIR, so the path is identical in both repos. Guessing a
# project-named path instead produced a "no backups found" alarm on a box that had
# been backing up nightly all along.
BACKUP_DIR=/var/backups/cooldown
findings=()
note() { findings+=("$1"); logger -t budget-audit "$1"; }
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
        logger -t budget-audit "pinned CA fingerprint ${ca_fp:0:20}... (first run)"
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
    [ -f "$f" ] && key_count=$((key_count + $(grep -c '^ssh-\|^ecdsa-\|^sk-' "$f" 2>/dev/null || echo 0)))
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

covered="$(iptables -S INPUT 2>/dev/null | grep -oE 'dports [0-9,]+' | tr ',' '\n' | grep -oE '[0-9]+' | sort -u | tr '\n' ' ')"
exposed=""
for p in $(ss -tlnH 2>/dev/null | awk '$4 ~ /^(0\.0\.0\.0|\[::\]):/ {print $4}' | sed 's/.*://' | sort -un); do
    case " $covered " in *" $p "*) ;; *) exposed="$exposed$p " ;; esac
done
[ -z "$exposed" ] || note "listening on all interfaces and not covered by a firewall rule: $exposed"

# --- is anything still being written down -----------------------------------------
journal_persistent=false
[ "$(find /var/log/journal -name '*.journal' 2>/dev/null | wc -l)" -gt 0 ] && journal_persistent=true
[ "$journal_persistent" = true ] || note "journal is not persisting -- a future audit will have no history to read"

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
    if vb="$("$PYBIN" /usr/local/sbin/budget-verify-backup.py 2>/dev/null)"; then
        backup_restores=1
    else
        backup_restores=0
        note "backup does NOT restore: $(printf '%s' "$vb" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error") or "unknown")' 2>/dev/null || echo 'verifier failed to run')"
    fi

    tampered_all="$(dpkg -V 2>/dev/null | awk '$2 != "c"' | wc -l)"
    tampered="$(dpkg -V 2>/dev/null | awk '$2 != "c" {print $NF}' | grep -vxF "$KNOWN_MODIFIED" | wc -l)"
    [ "$tampered" -eq 0 ] || note "$tampered packaged file(s) differ from their manifest (dpkg -V)"
fi

esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
{
printf '{"mode":"%s","ca_mode":"%s","ca_owner":"%s","ca_mtime":%d,"ca_fp":"%s","ca_days":%d,' \
       "$MODE" "$ca_mode" "$ca_owner" "${ca_mtime:-0}" "${ca_fp:0:16}" "${ca_days:-0}"
printf '"shell_accounts":"%s","ssh_keys":%d,"password_auth":"%s","failed_auth":%d,' \
       "$(esc "$shell_accounts")" "$key_count" "${pw_auth:-unknown}" "${failed_auth:-0}"
printf '"ts_days":%d,"listeners":"%s","firewall_rules":%d,"exposed_ports":"%s",' \
       "${ts_days:--1}" "$(esc "$listeners")" "$fw_rules" "$(esc "$exposed")"
printf '"journal_persistent":%s,"backup_age":%d,"root_pct":%d,"boot_pct":%d,' \
       "$journal_persistent" "${backup_age:--1}" "${root_pct:-0}" "${boot_pct:-0}"
printf '"tampered_files":%d,"tampered_all":%d,"backup_restores":%d,"full_checked":%d,"findings":%d,"checked":%d}\n' \
       "${tampered:--1}" "${tampered_all:--1}" "${backup_restores:--1}" "${full_checked:-0}" "${#findings[@]}" "$(date +%s)"
} > "$TMP"

chmod 644 "$TMP"
mv "$TMP" "$STATE"
trap - EXIT

[ "${#findings[@]}" -eq 0 ] && logger -t budget-audit "$MODE audit clean"
exit 0
