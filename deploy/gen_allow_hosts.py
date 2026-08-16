#!/usr/bin/env python3
"""Generate the mitmproxy --allow-hosts regex for every gated host.

--allow-hosts is the TLS-decrypt allowlist: ONLY these hosts get intercepted (missing
one = it tunnels through, no gate). It lives in the systemd unit, and it's the one
config that can't cleanly import addon.py (mitmproxy dependency), so it's generated
here from the same news_domains.py list plus the stable core sites.

Workflow after adding a site to news_domains.py:
    python3 deploy/gen_allow_hosts.py         # systemd-ready regex ($$ escaped)
    # paste into deploy/cooldown-proxy.service ExecStart (--allow-hosts "..."), then:
    sudo systemctl daemon-reload && sudo systemctl restart cooldown-proxy.service

    python3 deploy/gen_allow_hosts.py --plain # single-$ version (for a config file)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from news_domains import NEWS_DOMAINS

# Stable core matches — keep in sync with addon.py SITES (these rarely change).
CORE = ["reddit.com", "youtube.com", "open.spotify.com", "puzzmo.com"]
# Overlay-inject hosts, web-page hosts ONLY (not bare facebook.com): Messenger's realtime
# hosts (edge-chat/graph/gateway.facebook.com) pin their cert, so decrypting them breaks
# the Messenger app. Those stay tunneled — we only need the HTML doc to inject the overlay.
DECLUTTER = ["www.facebook.com", "web.facebook.com", "m.facebook.com", "mbasic.facebook.com"]
# mitm.it serves the CA to clients through the proxy, so it must be intercepted too.
EXTRA = ["mitm.it"]


def domains():
    """Every host this box is willing to decrypt. The one derivation two different
    consumers read: the --allow-hosts regex, and the CA's name constraints."""
    return CORE + NEWS_DOMAINS + DECLUTTER + EXTRA


def pattern(domain):
    esc = domain.replace(".", "[.]")
    return f"^(.+[.])?{esc}([:][0-9]+)?$"


def name_constraints():
    """An openssl.cnf fragment constraining a CA to exactly the hosts above.

    --allow-hosts decides what the proxy *chooses* to intercept. This decides what its
    CA is *able* to vouch for at all, which is the half that still holds if the key is
    copied off the box: a thief gets Reddit and the news list, not your bank.

    Two details that are load-bearing:

    · A DNS constraint matches the name AND any subdomain (RFC 5280 4.2.1.10), so
      "reddit.com" covers www.reddit.com and nothing else — the same semantics as
      addon.host_matches(), and it rejects the two shapes F4 was about
      (evil-reddit.com, reddit.com.attacker.io) at the trust layer.

    · A name TYPE absent from the permitted subtrees is UNCONSTRAINED, not forbidden.
      Permitting only DNS while saying nothing about email or URI would leave a stolen CA
      free to issue for those. Hence the explicit excludes for the two types nothing here
      ever legitimately needs.

    · IP is DELIBERATELY NOT EXCLUDED, and that is a trade made with evidence (F38).
      It was excluded originally, for the reason above, and it broke every gated site on
      the phone. In TRANSPARENT mode mitmproxy learns the destination from SO_ORIGINAL_DST
      -- an IP -- and addons/tlsconfig.py appends it to the SAN unconditionally:

          if conn_context.server.address:
              altnames.append(_ip_or_dns_name(conn_context.server.address[0]))

      So every certificate minted for a transparently-proxied client carried an IP SAN
      inside the excluded subtree, which RFC 5280 requires validators to reject. Devices
      using the REGULAR proxy port were unaffected, because there the destination arrives
      as a hostname via CONNECT -- which is why the laptop passed every test while the
      phone failed everything.

      What excluding IP actually bought: a stolen key could not vouch for https://<an-ip>.
      Exploiting that needs the victim to browse to a bare IP and disregard the hostname,
      which is close to worthless. What it cost: the tool did not work. The DNS constraint
      is the one that matters and it is untouched -- a stolen key still cannot produce a
      certificate for your bank.

    Emitted as a config SECTION rather than one long value: there are ~96 domains and a
    single line is both unreadable and close to openssl's comfort with long values.
    """
    out = ["nameConstraints=critical,@cooldown_nc", "", "[cooldown_nc]"]
    out += [f"permitted;DNS.{i}={d}" for i, d in enumerate(domains())]
    # No excluded;IP here -- see the note above. Transparent-mode certificates legitimately
    # carry the destination IP, so excluding IP rejects every one of them.
    out += ["excluded;email.0=.", "excluded;URI.0=."]
    return "\n".join(out)


def main():
    if "--name-constraints" in sys.argv:
        print(name_constraints())
        return
    regex = "|".join(pattern(d) for d in domains())
    # systemd needs a literal $ written as $$ in ExecStart; --plain skips that.
    print(regex if "--plain" in sys.argv else regex.replace("$", "$$"))


if __name__ == "__main__":
    main()
