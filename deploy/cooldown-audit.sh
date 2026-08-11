#!/bin/bash
# Weekly: check the security invariants that survive a reboot, and record drift.
#
# This exists because of a question I could not answer. After the box stopped patching
# itself for eleven days, "did anything happen in that window?" had no evidence behind
# it either way -- the journal was volatile, so the honest answer was "no records exist",
# which is not the same as "nothing happened". Persistent logging fixes the next eleven
# days. This fixes knowing whether the invariants still hold at all.
#
# Every check here is something that survives a reboot and would show tampering:
# the CA's mode and mtime, which accounts can log in, which keys are trusted, whether
# packaged files still match their manifests, what is listening, whether the firewall
# rules are loaded. None of it proves absence of compromise. It proves the specific
# things that should not have changed have not changed, which is what an audit can
# actually offer.
#
# Deliberately reports rather than repairs. A watchdog that silently "fixes" a new SSH
# key by deleting it would destroy the evidence that mattered.
set -u

STATE=/var/lib/cooldown-audit.json
TMP="$(mktemp /var/lib/.cooldown-audit.XXXXXX)" || exit 1
trap 'rm -f "$TMP"' EXIT

CA_DIR=/var/lib/cooldown/mitmproxy
findings=()
note() { findings+=("$1"); logger -t cooldown-audit "$1"; }

# --- the trust anchor -------------------------------------------------------------
# The single thing whose compromise would matter most: whoever holds this key can read
# every device that trusts it.
if [ -d "$CA_DIR" ]; then
    ca_mode="$(stat -c '%a' "$CA_DIR")"
    ca_owner="$(stat -c '%U' "$CA_DIR")"
    ca_mtime="$(stat -c '%Y' "$CA_DIR/mitmproxy-ca.pem" 2>/dev/null || echo 0)"
    [ "$ca_mode" = "700" ] || note "CA directory is mode $ca_mode, expected 700"
    [ "$ca_owner" = "cooldownproxy" ] || note "CA directory owned by $ca_owner, expected cooldownproxy"
else
    ca_mode="missing"; ca_owner="missing"; ca_mtime=0
    note "CA directory $CA_DIR is missing"
fi

# --- who can log in ---------------------------------------------------------------
shell_accounts="$(awk -F: '$7 !~ /(nologin|false)$/ {print $1}' /etc/passwd | sort | tr '\n' ' ')"
key_count=0
for f in /home/*/.ssh/authorized_keys /root/.ssh/authorized_keys; do
    [ -f "$f" ] && key_count=$((key_count + $(grep -c '^ssh-\|^ecdsa-\|^sk-' "$f" 2>/dev/null || echo 0)))
done

# Password auth on a key-only box is a downgrade someone else made.
pw_auth="$(sshd -T 2>/dev/null | awk '/^passwordauthentication/{print $2}')"
[ "${pw_auth:-no}" = "no" ] || note "sshd now accepts password authentication"

# --- packaged files vs their manifests --------------------------------------------
# `c`-marked conffiles are expected to differ; anything else is a modified packaged file.
# Field test, not a space-counting grep: dpkg -V emits "<flags> c <path>" for conffiles
# and "<flags> <path>" otherwise, with a single space. An earlier grep looked for three
# spaces, matched nothing, and reported all six expected config edits as tampering --
# five false positives on the first run, which is how an audit gets ignored.
# Raspberry Pi OS edits a few packaged files after install. They differ forever, so
# counting them makes this finding permanent -- and a permanent finding is one you stop
# reading. Excluded by exact path, and the raw count is still recorded so a change in
# the excluded set is visible rather than absorbed.
KNOWN_MODIFIED='/usr/lib/modprobe.d/g_ether.conf'
tampered_all="$(dpkg -V 2>/dev/null | awk '$2 != "c"' | wc -l)"
tampered="$(dpkg -V 2>/dev/null | awk '$2 != "c" {print $NF}' \
            | grep -vxF "$KNOWN_MODIFIED" | wc -l)"
[ "$tampered" -eq 0 ] || note "$tampered packaged file(s) differ from their manifest (dpkg -V)"

# --- what is listening, and is the firewall loaded --------------------------------
listeners="$(ss -tlnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -un | tr '\n' ' ')"
fw_rules="$(iptables -S INPUT 2>/dev/null | grep -c 'multiport\|tailscale0')"
[ "$fw_rules" -gt 0 ] || note "no interface-scoped INPUT rules found -- the firewall is not loaded"

# Anything listening on a wildcard address that the firewall does not cover is exposed
# to the LAN. This is how port 22 sat open while /health called it firewalled.
covered="$(iptables -S INPUT 2>/dev/null | grep -oE 'dports [0-9,]+' | tr ',' '\n' | grep -oE '[0-9]+' | sort -u | tr '\n' ' ')"
exposed=""
for p in $(ss -tlnH 2>/dev/null | awk '$4 ~ /^(0\.0\.0\.0|\[::\]):/ {print $4}' | sed 's/.*://' | sort -un); do
    case " $covered " in *" $p "*) ;; *) exposed="$exposed$p " ;; esac
done
[ -z "$exposed" ] || note "listening on all interfaces and not covered by a firewall rule: $exposed"

# --- logs actually being kept -----------------------------------------------------
journal_persistent=false
[ "$(find /var/log/journal -name '*.journal' 2>/dev/null | wc -l)" -gt 0 ] && journal_persistent=true
[ "$journal_persistent" = true ] || note "journal is not persisting -- a future audit will have no history to read"

jq_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
printf '{"ca_mode":"%s","ca_owner":"%s","ca_mtime":%d,' \
       "$ca_mode" "$ca_owner" "${ca_mtime:-0}" > "$TMP"
printf '"shell_accounts":"%s","ssh_keys":%d,"password_auth":"%s",' \
       "$(jq_escape "$shell_accounts")" "$key_count" "${pw_auth:-unknown}" >> "$TMP"
printf '"tampered_files":%d,"tampered_all":%d,"listeners":"%s","firewall_rules":%d,"exposed_ports":"%s",' \
       "$tampered" "${tampered_all:-0}" "$(jq_escape "$listeners")" "$fw_rules" "$(jq_escape "$exposed")" >> "$TMP"
printf '"journal_persistent":%s,"findings":%d,"checked":%d}\n' \
       "$journal_persistent" "${#findings[@]}" "$(date +%s)" >> "$TMP"

chmod 644 "$TMP"
mv "$TMP" "$STATE"
trap - EXIT

[ "${#findings[@]}" -eq 0 ] && logger -t cooldown-audit "audit clean"
exit 0
