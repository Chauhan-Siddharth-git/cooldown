#!/bin/bash
# ADVANCED variant entrypoint — gate a PHONE (transparent, via Tailscale) AND this
# computer's browser (explicit proxy on :8081) at the same time. See DOCKER-PHONE.md.
#
# Flow for the phone:  phone --Tailscale exit node--> [ this container ]
#                      tailscale0 --iptables REDIRECT--> mitmproxy :8080 (transparent)
# Flow for a browser:  browser --> host 127.0.0.1:8081 --> mitmproxy :8081 (regular)
set -euo pipefail

: "${TS_AUTHKEY:?TS_AUTHKEY is required — put it in .env (generate one at https://login.tailscale.com/admin/settings/keys)}"
TS_HOSTNAME="${TS_HOSTNAME:-cooldown-docker}"

# --- Tailscale -------------------------------------------------------------
mkdir -p /var/lib/tailscale /dev/net
[ -e /dev/net/tun ] || mknod /dev/net/tun c 10 200

tailscaled --state=/var/lib/tailscale/tailscaled.state --tun=tailscale0 --socket=/run/tailscale/tailscaled.sock &

# Join the tailnet and advertise as an exit node (idempotent; retries until tailscaled is ready).
until tailscale up \
        --authkey="${TS_AUTHKEY}" \
        --hostname="${TS_HOSTNAME}" \
        --advertise-exit-node \
        --accept-dns=false; do
    echo "[tailscale] waiting for daemon..."; sleep 1
done
echo "[tailscale] up as '${TS_HOSTNAME}'. NOW: approve the exit node in the admin console"
echo "            (Machines -> ${TS_HOSTNAME} -> Edit route settings -> Use as exit node),"
echo "            then enable it on your phone. See DOCKER-PHONE.md."

# --- Router plumbing (all inside THIS container's network namespace) --------
sysctl -w net.ipv4.ip_forward=1  >/dev/null 2>&1 || true
sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1 || true

# NAT everything the phone sends on out through the container's egress interface,
# so non-intercepted traffic (DNS, other ports, passthrough TLS) still reaches the net.
EGRESS="$(ip route show default | awk '{print $5; exit}')"
EGRESS="${EGRESS:-eth0}"
iptables -t nat -C POSTROUTING -o "$EGRESS" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o "$EGRESS" -j MASQUERADE

# Transparent redirect: pull the phone's web traffic (arriving on tailscale0) into
# mitmproxy's transparent listener. --allow-hosts still decides what's decrypted.
for port in 80 443; do
    iptables -t nat -C PREROUTING -i tailscale0 -p tcp --dport "$port" -j REDIRECT --to-ports 8080 2>/dev/null \
      || iptables -t nat -A PREROUTING -i tailscale0 -p tcp --dport "$port" -j REDIRECT --to-ports 8080
done

# Kill QUIC (UDP/443) so browsers fall back to interceptable TCP. Two layers, because
# tailscaled manages the filter table and can reorder a lone REJECT below its own rules
# (the exact "YouTube slipped through over QUIC" bug from the Pi). The mangle DROP is
# in a table Tailscale doesn't touch, so it always fires.
iptables -C FORWARD -i tailscale0 -p udp --dport 443 -j REJECT 2>/dev/null \
  || iptables -I FORWARD 1 -i tailscale0 -p udp --dport 443 -j REJECT
iptables -t mangle -C FORWARD -i tailscale0 -p udp --dport 443 -j DROP 2>/dev/null \
  || iptables -t mangle -I FORWARD 1 -i tailscale0 -p udp --dport 443 -j DROP

# --- The app ---------------------------------------------------------------
python3 app.py &   # Flask brain on 127.0.0.1:5000

# mitmproxy: transparent for the phone (:8080, fed by the redirect above) AND regular
# for this computer's browser (:8081, published to the host). If either the proxy or
# Flask dies, the container exits and Docker restarts the whole thing.
mitmdump \
  --mode transparent@8080 \
  --mode regular@8081 \
  --showhost \
  -s addon.py \
  --set http2=false \
  --set block_global=false \
  --set confdir="$MITM_CONFDIR" \
  --allow-hosts "$(python3 deploy/gen_allow_hosts.py --plain)" &

wait -n
exit $?
