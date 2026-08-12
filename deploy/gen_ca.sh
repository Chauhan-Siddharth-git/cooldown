#!/usr/bin/env bash
# Generate the interception CA, constrained to the hosts this box actually gates.
#
#   deploy/gen_ca.sh <confdir>        write a fresh CA into <confdir>
#
# WHY THIS EXISTS. mitmproxy generates its own CA on first start, and that CA can vouch
# for ANY hostname. The SD card is unencrypted, so whoever holds the hardware holds the
# key (see "Accepted by design"), and what they hold is a trust anchor for your bank and
# your email — not just for Reddit. The card is no easier to steal either way; the
# question is what the theft is worth.
#
# A CA carrying X.509 name constraints can only vouch for the permitted subtrees. The
# list comes from gen_allow_hosts.py, so it is the same list --allow-hosts is built from
# and cannot drift from it. Verified in tests/test_ca.py, and by this script on itself
# before it installs anything.
#
# COST, and it is real: adding a gated site now means regenerating the CA and RE-TRUSTING
# EVERY DEVICE, because the old CA cannot vouch for the new domain. Adding a site used to
# be a proxy restart. That is the trade — narrower key, heavier change.
#
# NOT ENFORCED EVERYWHERE. Name constraints on a user-installed root are honoured by
# Firefox and by Apple platforms; Android's support has historically been patchy. Treat
# this as reducing the blast radius on the platforms that check, not as a guarantee on
# all of them.
set -euo pipefail

CONF="${1:-}"
[ -n "$CONF" ] || { echo "usage: $0 <confdir>" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAYS="${COOLDOWN_CA_DAYS:-3650}"      # mitmproxy's own default; this change is about
                                      # constraints only, not about validity
CN="${COOLDOWN_CA_CN:-mitmproxy}"     # keep the name RECOVERY.md tells you to look for

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"

# ── the constraint list, from the one place the domain list lives ──────────────
nc="$(python3 "$HERE/gen_allow_hosts.py" --name-constraints)"
permitted="$(printf '%s\n' "$nc" | grep -c '^permitted;DNS')"
[ "$permitted" -gt 0 ] || {
    echo "refusing to build a CA: the derivation produced no permitted domains." >&2
    echo "An empty permitted set fails CLOSED (nothing would be trusted), but it means" >&2
    echo "gen_allow_hosts.py is broken and the CA would be useless." >&2
    exit 1
}

cat > "$work/ca.cnf" <<EOF
[req]
distinguished_name=dn
x509_extensions=v3_ca
prompt=no
[dn]
CN=$CN
O=$CN
[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
$nc
EOF

openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
        -config "$work/ca.cnf" -keyout "$work/ca.key" -out "$work/ca.crt" 2>/dev/null

# ── prove it before installing it ──────────────────────────────────────────────
# A generator that emits an unconstrained CA on some future openssl would fail silently
# and nothing downstream would notice, so it checks its own output: one in-scope host
# must verify, one out-of-scope host must NOT. A check that cannot fail is decoration.
probe() {                                   # $1 = hostname -> "OK" | "REJECTED"
    openssl req -newkey rsa:2048 -nodes -keyout "$work/p.key" -out "$work/p.csr" \
            -subj "/CN=$1" 2>/dev/null
    openssl x509 -req -in "$work/p.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
            -CAcreateserial -out "$work/p.crt" -days 1 \
            -extfile <(printf 'subjectAltName=DNS:%s\n' "$1") 2>/dev/null
    if openssl verify -CAfile "$work/ca.crt" "$work/p.crt" 2>&1 | grep -q ': OK$'
    then echo OK; else echo REJECTED; fi
}
in_scope="$(printf '%s\n' "$nc" | grep -m1 '^permitted;DNS' | cut -d= -f2)"
[ "$(probe "www.$in_scope")" = OK ] || {
    echo "self-check failed: the CA cannot vouch for www.$in_scope, which it must." >&2
    exit 1; }
[ "$(probe "cooldown-out-of-scope.example")" = REJECTED ] || {
    echo "self-check FAILED: the CA vouched for a host outside its constraints." >&2
    echo "The constraints are not being enforced — do not install this CA." >&2
    exit 1; }

# ── write mitmproxy's confdir layout ───────────────────────────────────────────
# mitmproxy backfills mitmproxy-dhparam.pem on first use, but NOT the .cer/.p12 —
# and those are what mitm.it hands your phone, so generate them here.
umask 077
mkdir -p "$CONF"
cat "$work/ca.key" "$work/ca.crt" > "$CONF/mitmproxy-ca.pem"
openssl pkcs12 -export -out "$CONF/mitmproxy-ca.p12" -inkey "$work/ca.key" \
        -in "$work/ca.crt" -passout pass: 2>/dev/null
cp "$work/ca.crt" "$CONF/mitmproxy-ca-cert.pem"
cp "$work/ca.crt" "$CONF/mitmproxy-ca-cert.cer"        # same PEM, the name Android wants
openssl pkcs12 -export -nokeys -out "$CONF/mitmproxy-ca-cert.p12" \
        -in "$work/ca.crt" -passout pass: 2>/dev/null
chmod 600 "$CONF/mitmproxy-ca.pem" "$CONF/mitmproxy-ca.p12"
chmod 644 "$CONF/mitmproxy-ca-cert.pem" "$CONF/mitmproxy-ca-cert.cer" \
          "$CONF/mitmproxy-ca-cert.p12"

echo "CA written to $CONF"
echo "  constrained to $permitted domains (self-check passed: in-scope OK, out-of-scope rejected)"
echo "  fingerprint: $(openssl x509 -in "$CONF/mitmproxy-ca-cert.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
