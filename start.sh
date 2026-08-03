#!/bin/bash
# Run the stack in the FOREGROUND for local development. Not the deployment path —
# that's systemd (deploy/*.service).
#
# This script used to start mitmdump with no --allow-hosts (so it decrypted every site
# you visited, not just the gated ones), no --set confdir (so it generated a SECOND
# certificate authority under ~/.mitmproxy, separate from the one your devices trust),
# and mitmproxy's default bind of 0.0.0.0 — an intercepting proxy on every interface.
# That quietly undid F1 and F5 for anyone who ran it. All three are fixed below.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Refuse to fight the real services for port 5000 / the proxy ports.
for svc in cooldown-app cooldown-proxy; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "✗ $svc is running under systemd — stop it first, or you'll be testing the wrong process:" >&2
        echo "    sudo systemctl stop cooldown-app cooldown-proxy" >&2
        exit 1
    fi
done

PY="./venv/bin/python";   [ -x "$PY" ]   || PY=python3
MITM="./venv/bin/mitmdump"; [ -x "$MITM" ] || MITM=mitmdump

# A dev CA of its own, under this checkout and gitignored. Deliberately NOT the
# deployed one at /var/lib/cooldown/mitmproxy: that is owned by cooldownproxy and
# mode 700, and a dev script has no business asking for it.
CONFDIR="${CONFDIR:-$PWD/.mitmproxy-dev}"
mkdir -p "$CONFDIR"; chmod 700 "$CONFDIR"

# The same TLS-decrypt allowlist the unit uses, from the same generator — so dev
# intercepts exactly the gated hosts and tunnels everything else untouched.
ALLOW_HOSTS="$(python3 deploy/gen_allow_hosts.py --plain)"

cleanup(){ jobs -p | xargs -r kill 2>/dev/null || true; }
trap cleanup EXIT INT TERM

redis-cli ping >/dev/null 2>&1 || { echo "redis isn't up: sudo systemctl start redis-server" >&2; exit 1; }
echo "· app   -> 127.0.0.1:5000"
"$PY" app.py &
sleep 1

echo "· proxy -> 127.0.0.1:8080  (loopback only — point your browser's proxy here)"
echo "  dev CA: $CONFDIR"
exec "$MITM" \
  --mode regular@8080 \
  --listen-host 127.0.0.1 \
  --showhost \
  -s addon.py \
  --set http2=true \
  --set confdir="$CONFDIR" \
  --allow-hosts "$ALLOW_HOSTS"
