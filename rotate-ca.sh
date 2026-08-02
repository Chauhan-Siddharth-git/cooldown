#!/usr/bin/env bash
# Rotate the certificate authority this box uses to intercept your own traffic.
#
#   ./rotate-ca.sh            rotate (asks first, backs the old one up)
#   ./rotate-ca.sh --check    show the current certificate and what would happen
#
# WHY: the CA is valid for ~10 years. If anyone ever quietly copied the SD card, they'd
# hold a working key for a decade. Theft you'd notice; a quiet copy you would not.
# Rotating yearly means an old copy stops being useful.
#
# COST: every routed device must install the new certificate. Until it does, browsing on
# that device fails with certificate warnings. Do this when you have ten minutes.
set -euo pipefail

CONF="${COOLDOWN_CONFDIR:-/var/lib/cooldown/mitmproxy}"
BACKUPS="${COOLDOWN_BACKUP_DIR:-/var/backups/cooldown}"
PROXY_USER="${COOLDOWN_PROXY_USER:-cooldownproxy}"
SERVICE="${COOLDOWN_PROXY_SERVICE:-cooldown-proxy}"

if [ -t 1 ]; then B=$'\e[1m'; DIM=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else B=""; DIM=""; G=""; Y=""; R=""; C=""; N=""; fi
say()  { printf "  %s\n" "$*"; }
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
die()  { printf "\n${R}✗ %s${N}\n" "$*" >&2; exit 1; }

fingerprint() { sudo openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2; }

[ "$(id -u)" -eq 0 ] && die "Run as your normal user, not root — it will use sudo when needed."
sudo test -f "$CONF/mitmproxy-ca.pem" || die "No certificate found at $CONF — is this the right box?"

printf "\n${B}Current certificate${N}\n"
say "location:    $CONF"
say "fingerprint: $(fingerprint "$CONF/mitmproxy-ca-cert.pem")"
sudo openssl x509 -in "$CONF/mitmproxy-ca-cert.pem" -noout -dates | sed 's/^/  /'

if [ "${1:-}" = "--check" ]; then
  printf "\n${DIM}--check only. Re-run without it to rotate.${N}\n"; exit 0
fi

cat <<EOF

${Y}${B}This replaces the certificate your devices trust.${N}
  · Every routed device (phone, laptop) must install the NEW one afterwards.
  · Until it does, ${B}HTTPS on that device will fail${N} with warnings.
  · The old key is backed up, so this is reversible if something goes wrong.
EOF
read -r -p "  ${B}Type 'rotate' to continue:${N} " ans </dev/tty || true
[ "$ans" = rotate ] || die "Cancelled — nothing changed."

STAMP="$(date +%Y-%m-%d-%H%M%S)"
OLD="$BACKUPS/ca-$STAMP"
printf "\n${B}Rotating${N}\n"
sudo mkdir -p "$BACKUPS"
sudo cp -a "$CONF" "$OLD"                      # -a keeps the 600 key modes
sudo chmod 700 "$OLD"
ok "old certificate backed up to $OLD"

sudo rm -f "$CONF"/mitmproxy-ca*.pem "$CONF"/mitmproxy-ca*.p12 "$CONF"/mitmproxy-ca*.cer
ok "old certificate removed"

# mitmproxy regenerates a CA on start when confdir has none.
sudo systemctl restart "$SERVICE"
for _ in $(seq 1 20); do
  sudo test -f "$CONF/mitmproxy-ca-cert.pem" && break
  sleep 1
done
sudo test -f "$CONF/mitmproxy-ca-cert.pem" || {
  printf "\n${R}Generation failed — restoring the old certificate.${N}\n"
  sudo rm -rf "$CONF"; sudo cp -a "$OLD" "$CONF"
  sudo chown -R "$PROXY_USER:$PROXY_USER" "$CONF"; sudo systemctl restart "$SERVICE"
  die "Rolled back. Your devices still trust the previous certificate."
}
sudo chown -R "$PROXY_USER:$PROXY_USER" "$CONF"
sudo chmod 700 "$CONF"
NEWFP="$(fingerprint "$CONF/mitmproxy-ca-cert.pem")"
[ "$NEWFP" = "$(fingerprint "$OLD/mitmproxy-ca-cert.pem")" ] && die "New fingerprint matches the old — rotation did not happen."
ok "new certificate generated"

cat <<EOF

${B}${G}Rotated.${N}
  new fingerprint: ${C}$NEWFP${N}
  ${DIM}old one kept at $OLD — delete once every device is on the new certificate${N}

${B}Now, on each device you route through this box:${N}
  1. Remove the OLD profile/certificate (see RECOVERY.md for where it lives).
  2. Visit ${C}http://mitm.it${N} and install the new one.
  3. ${Y}iOS also needs:${N} Settings → General → About → Certificate Trust Settings → on.

  Until you do, that device will show certificate warnings. That's expected.
EOF
