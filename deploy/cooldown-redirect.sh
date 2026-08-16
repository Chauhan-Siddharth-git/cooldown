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
fw_up(){ rc=0; for ipt in iptables ip6tables; do
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

          # Do NOT flip the policy until the rules that keep you reachable are verified
          # PRESENT. Every insertion above is `-C ... || -I ...`, whose failure is silent,
          # and this script has no `set -e` -- so a conntrack module that will not load, or
          # an interface that does not exist yet, left the accepts missing and the policy
          # flipped to DROP anyway. That is not a degraded state, it is a locked box: the
          # LAN SSH fallback was deliberately removed (see PORTS above), so recovery means
          # a monitor and a keyboard.
          #
          # Checked with -C, which asks the kernel whether the rule is really in the chain,
          # rather than trusting the exit status of the command that tried to add it.
          # ALL FIVE, not three. The comment above calls every one of them a survival
          # requirement and names ICMPv6 as "NOT optional... dropping it does not block
          # pings, it breaks IPv6 entirely" -- and then only conntrack, loopback and the
          # tailnet port accept were ever checked. The ICMP and DHCP-client rules are
          # inserted with the same silent `-C ... || -I ...` pattern and were never
          # re-verified, so the invariant as written was not the invariant as enforced.
          # F28 exists because a missing accept plus a DROP policy is a locked box; that is
          # just as true of the two nobody checked.
          missing=""
          $ipt -C INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null \
              || missing="$missing conntrack-established"
          $ipt -C INPUT -i lo -j ACCEPT 2>/dev/null \
              || missing="$missing loopback"
          $ipt -C INPUT -i "$IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null \
              || missing="$missing $IF:$PORTS"
          # Family-specific, and checked with the same rule shape they were inserted with.
          if [ "$ipt" = ip6tables ]; then
              $ipt -C INPUT -p icmpv6 -j ACCEPT 2>/dev/null \
                  || missing="$missing icmpv6(neighbour-discovery)"
              $ipt -C INPUT -p udp --dport 546 -j ACCEPT 2>/dev/null \
                  || missing="$missing dhcpv6-client"
          else
              $ipt -C INPUT -p icmp -j ACCEPT 2>/dev/null \
                  || missing="$missing icmp"
              $ipt -C INPUT -p udp --sport 67 --dport 68 -j ACCEPT 2>/dev/null \
                  || missing="$missing dhcp-client"
          fi

          if [ -n "$missing" ]; then
              # Leaving the policy at ACCEPT is the weaker posture and the right call: an
              # exposed box can be fixed from anywhere, a locked one cannot be fixed at all.
              logger -t cooldown-redirect -p user.err \
                  "REFUSING to set $ipt INPUT policy to DROP -- these accepts are missing:$missing"
              echo "REFUSING $ipt DROP policy; missing accepts:$missing" >&2
              rc=1
              continue
          fi
          $ipt -P INPUT DROP
        done
        return "${rc:-0}"; }
# Policy back to ACCEPT FIRST, before removing anything: taking the accepts away while
# the policy is still DROP would cut the connection running the teardown.
fw_dn(){ for ipt in iptables ip6tables; do
          $ipt -P INPUT ACCEPT
          $ipt -D INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
          $ipt -D INPUT -i lo -j ACCEPT 2>/dev/null || true
          # Delete by SHAPE, not by today's port list. Matching "$PORTS" only removes
          # rules created with the current value, so every change to PORTS orphaned the
          # previous generation -- the live v6 chain was still carrying two rules from
          # the PORTS=5000,8080,8081 era, months after 22 was added. They were harmless
          # duplicates here, but a teardown that leaves rules behind is not a teardown,
          # and the next reader cannot tell an orphan from something deliberate.
          #
          # Bounded to the three shapes this script creates: a tcp multiport ACCEPT or
          # DROP. Nothing else on this box writes those.
          $ipt -S INPUT 2>/dev/null \
            | grep -E -- '^-A INPUT .*-p tcp -m multiport --dports [0-9,]+ -j (ACCEPT|DROP)$' \
            | while read -r spec; do
                # shellcheck disable=SC2086 -- the spec must expand into separate args
                $ipt -D ${spec#-A } 2>/dev/null || true
              done
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
# beyond a single read; see deploy/sudoers.d-cooldown.
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
  # fw_up's status is CHECKED, not discarded. It refuses to flip the INPUT policy to
  # DROP when the accepts that keep you reachable are missing (see the block above) and
  # returns 1 to say so -- but `fw_up; acct_up` threw that away, acct_up's status became
  # the script's, and the unit is Type=oneshot, so systemd recorded a clean activation.
  # The box then sat at policy ACCEPT with 8080/8081/5000/22 reachable while
  # `systemctl status cooldown-redirect` was green: the refusal was designed to be loud
  # and its only listener was ignoring it. F29's shape (a failed step rendered as
  # success), in the script written to fix F28.
  #
  # acct_up still runs first -- traffic accounting is unrelated to the firewall and
  # skipping it would trade one silent gap for another -- but the exit status is fw_up's.
  up)   r4 80; r4 443; r6 80; r6 443; q_up
        fw_rc=0; fw_up || fw_rc=$?
        acct_up
        exit "$fw_rc" ;;
  down) d4 80; d4 443; d6 80; d6 443; q_dn; fw_dn; acct_dn ;;
esac
