#!/bin/bash
# Inventory every place on THIS machine that trusts a CA, and say which ones matter.
#
#   tools/ca-trust-scan.sh                      # compare against the live box CA over ssh
#   tools/ca-trust-scan.sh <sha256-fingerprint> # compare against a fingerprint you supply
#   EXPECT=$(cat fp.txt) tools/ca-trust-scan.sh
#
# WHY THIS EXISTS. rotate-ca.sh says "every routed device must install the new
# certificate". Nothing said WHICH devices, or -- the part that actually bites -- which
# trust stores on each device. People think in devices; trust lives in stores, and one
# laptop turned out to have five:
#
#     the system bundle          (Chrome, curl, wget, anything using OpenSSL)
#     ~/.pki/nssdb               (Chrome/Chromium on Linux, separately)
#     each Firefox profile       (Firefox ignores the system store entirely)
#
# A rotation that updates the system store and one Firefox profile leaves the others
# broken, and a REVOCATION that misses one leaves a stolen CA trusted on a machine you
# believe you cleaned.
#
# It also reports CA material that is NOT the expected one. That is how a stale CA from a
# superseded dev setup was found still sitting in a browser profile months later.
set -u

EXPECT="${1:-${EXPECT:-}}"
if [ -z "$EXPECT" ]; then
    # Best effort: ask the box. Failing to reach it is not fatal -- the scan still lists
    # what is trusted, it just cannot say which entry is the live one.
    EXPECT="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI:-pi@raspberrypi.local}" \
        'sudo openssl x509 -in /var/lib/cooldown/mitmproxy/mitmproxy-ca-cert.pem -noout -fingerprint -sha256' \
        2>/dev/null | grep -o '[A-F0-9:]\{95\}')"
fi
EXPECT="$(echo "$EXPECT" | tr 'a-f' 'A-F')"
[ -n "$EXPECT" ] && echo "expected (live) CA: $EXPECT" \
                 || echo "expected CA: UNKNOWN — could not reach the box and none supplied;"$'\n'"             entries below are listed but cannot be classified."
echo

# Results go to a file, not shell variables. verdict() is called inside $( ), which is a
# subshell, so any counter it increments is discarded when the substitution ends -- the
# first version of this script printed a confident "0 places trusting the live CA" while
# listing four of them directly above.
TALLY="$(mktemp)"; trap 'rm -f "$TALLY"' EXIT

verdict() {   # $1 = fingerprint, $2 = where
    if   [ -z "$EXPECT" ];     then echo "unclassified"
    elif [ "$1" = "$EXPECT" ]; then echo "LIVE $1 $2" >> "$TALLY"; echo "LIVE CA"
    else                            echo "OTHER $1 $2" >> "$TALLY"; echo "OTHER CA  <-- unexpected"
    fi
}

echo "=== system trust store ==="
# Two checks, because they disagree. The files in /usr/local/share/ca-certificates are what
# you manage; the compiled bundle is what programs actually read. Grepping the bundle for a
# name finds NOTHING even when the CA is present -- it holds PEM blobs, not subject lines --
# which reads exactly like "not installed". Parse it, do not grep it.
shopt -s nullglob
for f in /usr/local/share/ca-certificates/*.crt /etc/ssl/certs/*mitmproxy*; do
    fp="$(openssl x509 -in "$f" -noout -fingerprint -sha256 2>/dev/null | grep -o '[A-F0-9:]\{95\}')"
    [ -n "$fp" ] || continue
    # Tally the resolved path: /etc/ssl/certs/*.pem is a symlink to the managed copy,
    # so keying on the literal path reports one file as two places to update.
    printf "  %-52s %-12s %s\n" "$f" "$(verdict "$fp" "$(readlink -f "$f")")" "${fp:0:23}…"
done
in_bundle="$(openssl crl2pkcs7 -nocrl -certfile /etc/ssl/certs/ca-certificates.crt 2>/dev/null \
    | openssl pkcs7 -print_certs 2>/dev/null | grep -c 'CN *= *mitmproxy')"
echo "  compiled bundle contains $in_bundle mitmproxy CA(s) — this is what curl/Chrome honour"

for db in $(find "${HOME}/.mozilla/firefox" "${HOME}/.pki/nssdb" "${HOME}/snap/firefox/common/.mozilla/firefox" \
            -name 'cert9.db' -o -name 'pkcs11.txt' 2>/dev/null | grep -v pkcs11 | sort -u); do
    prof="$(dirname "$db")"
    hits="$(certutil -L -d sql:"$prof" 2>/dev/null | grep -iE 'mitm|cooldown')"
    [ -n "$hits" ] || continue
    echo
    echo "=== $prof ==="
    while IFS= read -r line; do
        line="$(echo "$line" | sed 's/[[:space:]]*$//')"   # trailing spaces first, or the
        flags="$(echo "$line" | awk '{print $NF}')"        # flags never anchor to $
        nick="$(echo "$line" | sed "s/[[:space:]]*${flags}\$//" | sed 's/[[:space:]]*$//')"
        # -n, not -a. Adding -a makes the lookup fail silently for certs whose nickname is
        # a synthesised "CN - O" string, and an empty result read as "different CA" is a
        # fabricated distinction -- that happened on the first run of this scan.
        fp="$(certutil -L -d sql:"$prof" -n "$nick" 2>/dev/null \
              | grep -A2 'SHA-256' | grep -oE '([0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2}' | head -1 | tr 'a-f' 'A-F')"
        if [ -z "$fp" ]; then
            printf "  %-24s %-9s %s\n" "$nick" "$flags" "COULD NOT READ — treat as unknown, do not assume absent"
            continue
        fi
        # Trust bits are what matter. ",," means the certificate is present but validates
        # nothing; a rotation can ignore it, a cleanup should still remove it.
        case "$flags" in *C*) t=TRUSTED ;; *) t=no-trust ;; esac
        printf "  %-24s %-9s %-12s %s\n" "$nick" "$t" "$(verdict "$fp" "$prof:$nick")" "${fp:0:23}…"
    done <<< "$hits"
done

echo
echo "=== stale CA material with a private key ==="
# mitmproxy's default confdir. start.sh pins CONFDIR precisely so this is not created, but
# a CA generated before that fix does not disappear when the bug does.
for d in "$HOME/.mitmproxy"; do
    [ -d "$d" ] || continue
    for f in "$d"/mitmproxy-ca.pem; do
        [ -f "$f" ] || continue
        fp="$(openssl x509 -in "$f" -noout -fingerprint -sha256 2>/dev/null | grep -o '[A-F0-9:]\{95\}')"
        echo "  $f"
        echo "    $(stat -c '%A %U' "$f")   $(verdict "$fp" "$f")   ${fp:0:23}…"
        echo "    A CA private key. Harmless while nothing trusts it; one click from not being."
    done
done

echo
echo "── summary ──"
# Deduplicated by location: /etc/ssl/certs/x.pem is usually a symlink to the managed copy,
# and counting both overstates how many places actually need touching.
live_n=$(awk '$1=="LIVE"{print $3}'  "$TALLY" | sort -u | wc -l)
other_n=$(awk '$1=="OTHER"{print $3}' "$TALLY" | sort -u | wc -l)
echo "  places holding the live CA:  $live_n   (every one must be updated by a rotation)"
echo "  other CA material found:     $other_n  (investigate; remove if unexplained)"
awk '$1=="OTHER"{print "    unexpected: "$3}' "$TALLY" | sort -u
[ -z "$EXPECT" ] && echo "  NOTE: nothing was classified — rerun with the live fingerprint."
