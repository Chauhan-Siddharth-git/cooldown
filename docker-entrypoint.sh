#!/bin/bash
# One container, two processes:
#   1. the Flask "brain"  — waitress on container-localhost:5000 (never exposed)
#   2. mitmproxy          — EXPLICIT-proxy mode on :8080 (the client points its
#                           HTTP-proxy setting here)
# Redis is a separate service, reached via $REDIS_HOST (set in docker-compose.yml).
#
# If EITHER process dies, `wait -n` returns and the container exits, so Docker's
# restart policy brings the whole stack back cleanly instead of limping along with
# half of it dead.
set -euo pipefail

MITM_CONFDIR="${MITM_CONFDIR:-/home/app/.mitmproxy}"

# The brain (binds 127.0.0.1:5000 inside the container; the addon calls it there).
python3 app.py &

# The interceptor. Regular (explicit) mode needs NO host iptables — that's the whole
# point of the Docker build. Transparent mode (phone-over-Tailscale) is the host-level
# Pi setup documented in DOCKER.md, deliberately not used here.
#
# --allow-hosts is the TLS-decrypt allowlist: only these hosts are intercepted, so all
# other HTTPS tunnels straight through untouched. Built from the same source of truth
# as the app (core sites + news_domains.py + mitm.it), so it never drifts out of sync.
ALLOW_HOSTS="$(python3 deploy/gen_allow_hosts.py --plain)"
mitmdump \
  --mode regular@8080 \
  --showhost \
  -s addon.py \
  --set http2=false \
  --set block_global=false \
  --set confdir="$MITM_CONFDIR" \
  --allow-hosts "$ALLOW_HOSTS" &

wait -n
exit $?
