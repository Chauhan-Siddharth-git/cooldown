#!/bin/bash
# Redirect phone web traffic (via Tailscale exit node) into mitmproxy, and block
# QUIC so clients fall back to interceptable TCP. IPv4 + IPv6.
#
# QUIC blocking is done in TWO places on purpose:
#   1. filter/FORWARD REJECT  — sends icmp-port-unreachable so the client drops
#      QUIC and falls back to TCP *fast*. BUT Tailscale owns the filter/FORWARD
#      chain: its `ts-forward` MARK+ACCEPT runs first and, whenever tailscaled
#      (re)starts or reconfigures (e.g. exit-node toggle), it reinserts itself at
#      the top and demotes anything below it. A rule demoted below ts-forward's
#      ACCEPT never fires. So this layer is best-effort/fast-path only.
#   2. mangle/FORWARD DROP    — the durable guarantee. Tailscale does NOT manage
#      the mangle table, so a rule here can't be reordered out of the way. mangle
#      FORWARD is traversed before filter FORWARD, so this reliably kills QUIC
#      even after Tailscale rewrites its filter rules. Slightly slower fallback
#      than REJECT (client waits for timeout), but it always fires.
# See: the "YouTube not gating over QUIC" incident — the filter rule had been
# silently demoted below ts-forward, so QUIC sailed through and YouTube bypassed
# the proxy entirely.
IF=tailscale0
r4(){ iptables  -t nat -C PREROUTING -i "$IF" -p tcp --dport "$1" -j REDIRECT --to-ports 8080 2>/dev/null || iptables  -t nat -A PREROUTING -i "$IF" -p tcp --dport "$1" -j REDIRECT --to-ports 8080; }
r6(){ ip6tables -t nat -C PREROUTING -i "$IF" -p tcp --dport "$1" -j REDIRECT --to-ports 8080 2>/dev/null || ip6tables -t nat -A PREROUTING -i "$IF" -p tcp --dport "$1" -j REDIRECT --to-ports 8080; }
d4(){ iptables  -t nat -D PREROUTING -i "$IF" -p tcp --dport "$1" -j REDIRECT --to-ports 8080 2>/dev/null || true; }
d6(){ ip6tables -t nat -D PREROUTING -i "$IF" -p tcp --dport "$1" -j REDIRECT --to-ports 8080 2>/dev/null || true; }
# filter REJECT: force it to the TOP of FORWARD every time. Delete any existing
# copies first (they may be stranded below ts-forward), then insert at line 1.
# Don't use `-C` to guard — it matches regardless of position and would let a
# demoted, dead rule masquerade as "already installed".
q_up(){ while iptables  -D FORWARD -i "$IF" -p udp --dport 443 -j REJECT 2>/dev/null; do :; done
        iptables  -I FORWARD 1 -i "$IF" -p udp --dport 443 -j REJECT
        while ip6tables -D FORWARD -i "$IF" -p udp --dport 443 -j REJECT 2>/dev/null; do :; done
        ip6tables -I FORWARD 1 -i "$IF" -p udp --dport 443 -j REJECT
        # durable backstop in the Tailscale-untouched mangle table
        iptables  -t mangle -C FORWARD -i "$IF" -p udp --dport 443 -j DROP 2>/dev/null || iptables  -t mangle -I FORWARD 1 -i "$IF" -p udp --dport 443 -j DROP
        ip6tables -t mangle -C FORWARD -i "$IF" -p udp --dport 443 -j DROP 2>/dev/null || ip6tables -t mangle -I FORWARD 1 -i "$IF" -p udp --dport 443 -j DROP; }
q_dn(){ while iptables  -D FORWARD -i "$IF" -p udp --dport 443 -j REJECT 2>/dev/null; do :; done
        while ip6tables -D FORWARD -i "$IF" -p udp --dport 443 -j REJECT 2>/dev/null; do :; done
        iptables  -t mangle -D FORWARD -i "$IF" -p udp --dport 443 -j DROP 2>/dev/null || true
        ip6tables -t mangle -D FORWARD -i "$IF" -p udp --dport 443 -j DROP 2>/dev/null || true; }

# Restrict the mitmproxy ports to the Tailscale interface (+ loopback). mitmproxy
# listens on 0.0.0.0 and [::], and on a typical Pi eth0 has a LAN address AND a
# globally-routable IPv6 — so without this the proxy is reachable from the LAN and
# potentially the public internet over v6. Interface-scoped so it covers both proxy
# modes and IPv4+IPv6 with one rule set; transparent-redirected packets arrive on
# tailscale0 so they still pass. ACCEPTs go at the top of INPUT, DROP at the end.
PORTS=8080,8081
fw_up(){ for ipt in iptables ip6tables; do
          $ipt -C INPUT -i "$IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -i "$IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT
          $ipt -C INPUT -i lo    -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null || $ipt -I INPUT 2 -i lo    -p tcp -m multiport --dports "$PORTS" -j ACCEPT
          $ipt -C INPUT          -p tcp -m multiport --dports "$PORTS" -j DROP   2>/dev/null || $ipt -A INPUT          -p tcp -m multiport --dports "$PORTS" -j DROP
        done; }
fw_dn(){ for ipt in iptables ip6tables; do
          $ipt -D INPUT -i "$IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null || true
          $ipt -D INPUT -i lo    -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null || true
          $ipt -D INPUT          -p tcp -m multiport --dports "$PORTS" -j DROP   2>/dev/null || true
        done; }
case "$1" in
  up)   r4 80; r4 443; r6 80; r6 443; q_up; fw_up ;;
  down) d4 80; d4 443; d6 80; d6 443; q_dn; fw_dn ;;
esac
