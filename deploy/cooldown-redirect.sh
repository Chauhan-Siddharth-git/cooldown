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
# 5000 is the Flask app. It binds loopback + the tailscale0 address (never 0.0.0.0), so
# this rule is the second layer rather than the only one — but it is what keeps the
# dashboard off the LAN and off the public IPv6 address if that bind ever widens.
# 22 included as of 2026-08-10. It was listening on 0.0.0.0 and covered by nothing,
# while /health listed it as firewalled -- a dashboard asserting a control that did not
# exist. Reachable from any device on the LAN, though key-only, so brute force was never
# the risk; the risk was believing otherwise.
#
# TRADE-OFF, on purpose: this removes the LAN fallback. If tailscaled ever fails you
# cannot SSH in from the same network any more, and recovery means a monitor and
# keyboard. Revert by dropping 22 from this list and re-running `cooldown-redirect.sh up`.
PORTS=22,5000,8080,8081
# DEFAULT-DENY. Until 2026-08-11 the INPUT policy was ACCEPT with an allowlist of named
# ports, which protected exactly the ports somebody had remembered to name: avahi sat on
# UDP 5353 on every interface, including a globally routable IPv6 address, and was never
# covered because the list only held TCP (F23). The class of problem is "the next service
# to start listening is exposed by default", and only the policy fixes that.
#
# Rules are inserted in reverse so the final order is deterministic regardless of how many
# times this runs, and every one of them is a survival requirement:
#
#   ESTABLISHED,RELATED  replies to connections WE opened. Without this, flipping the
#                        policy kills every outbound connection's return traffic --
#                        including the ssh session running this script.
#   lo (all of it)       the old rule covered four ports; Flask, Redis and the proxy talk
#                        to each other on others.
#   ICMPv6 (all types)   NOT optional and not about ping: neighbour discovery IS ICMPv6.
#                        Dropping it does not block pings, it breaks IPv6 entirely.
#   DHCP client          or the lease never renews and the box loses its address.
#   ts-input             Tailscale's own chain, which already accepts tailscale0 and its
#                        UDP 41641. Left to Tailscale; we only ensure the jump exists.
#
# The explicit port DROP is KEPT even though the policy now covers it: if anything ever
# resets the policy to ACCEPT, the named ports stay closed rather than falling wide open.
fw_up(){ for ipt in iptables ip6tables; do
          # inserted last-to-first, so the resulting order reads top-down as written above
          if [ "$ipt" = ip6tables ]; then
            $ipt -C INPUT -p udp --dport 546 -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -p udp --dport 546 -j ACCEPT
            $ipt -C INPUT -p icmpv6 -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -p icmpv6 -j ACCEPT
          else
            $ipt -C INPUT -p udp --sport 67 --dport 68 -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -p udp --sport 67 --dport 68 -j ACCEPT
            $ipt -C INPUT -p icmp -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -p icmp -j ACCEPT
          fi
          $ipt -C INPUT -i "$IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -i "$IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT
          $ipt -C INPUT -i lo -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -i lo -j ACCEPT
          $ipt -C INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || $ipt -I INPUT 1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
          # Tailscale owns ts-input and reinserts its own jump; make sure one exists so a
          # DROP policy cannot strand the tailnet if this runs before tailscaled is ready.
          $ipt -nL ts-input >/dev/null 2>&1 && { $ipt -C INPUT -j ts-input 2>/dev/null || $ipt -A INPUT -j ts-input; }
          $ipt -C INPUT -p tcp -m multiport --dports "$PORTS" -j DROP 2>/dev/null || $ipt -A INPUT -p tcp -m multiport --dports "$PORTS" -j DROP
          $ipt -P INPUT DROP
        done; }
# Policy back to ACCEPT FIRST, before removing anything: taking the accepts away while
# the policy is still DROP would cut the connection running the teardown.
fw_dn(){ for ipt in iptables ip6tables; do
          $ipt -P INPUT ACCEPT
          $ipt -D INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
          $ipt -D INPUT -i lo -j ACCEPT 2>/dev/null || true
          $ipt -D INPUT -i "$IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null || true
          $ipt -D INPUT -i lo    -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null || true
          $ipt -D INPUT          -p tcp -m multiport --dports "$PORTS" -j DROP   2>/dev/null || true
          if [ "$ipt" = ip6tables ]; then
            $ipt -D INPUT -p icmpv6 -j ACCEPT 2>/dev/null || true
            $ipt -D INPUT -p udp --dport 546 -j ACCEPT 2>/dev/null || true
          else
            $ipt -D INPUT -p icmp -j ACCEPT 2>/dev/null || true
            $ipt -D INPUT -p udp --sport 67 --dport 68 -j ACCEPT 2>/dev/null || true
          fi
        done; }
# Observational byte counters for the packet-feed background: TRAFFIC_ACCT holds four
# rules with NO target, so they only count and fall through — they cannot affect routing.
# :443 = TLS (encrypted), :80 + :53 = HTTP + DNS (the plaintext that leaves in the clear).
# Created here (as root, at boot) rather than by the web app, so the app needs nothing
# beyond a single read; see deploy/sudoers.d-budget-proxy.
acct_up(){
  iptables -t mangle -nL TRAFFIC_ACCT >/dev/null 2>&1 || iptables -t mangle -N TRAFFIC_ACCT
  for spec in "tcp 443" "tcp 80" "udp 53" "tcp 53"; do
    set -- $spec
    iptables -t mangle -C TRAFFIC_ACCT -p "$1" --dport "$2" 2>/dev/null || \
      iptables -t mangle -A TRAFFIC_ACCT -p "$1" --dport "$2"
  done
  for ch in PREROUTING POSTROUTING; do
    iptables -t mangle -C "$ch" -j TRAFFIC_ACCT 2>/dev/null || \
      iptables -t mangle -A "$ch" -j TRAFFIC_ACCT
  done
}
acct_dn(){
  for ch in PREROUTING POSTROUTING; do
    iptables -t mangle -D "$ch" -j TRAFFIC_ACCT 2>/dev/null || true
  done
  iptables -t mangle -F TRAFFIC_ACCT 2>/dev/null || true
  iptables -t mangle -X TRAFFIC_ACCT 2>/dev/null || true
}
case "$1" in
  up)   r4 80; r4 443; r6 80; r6 443; q_up; fw_up; acct_up ;;
  down) d4 80; d4 443; d6 80; d6 443; q_dn; fw_dn; acct_dn ;;
esac
