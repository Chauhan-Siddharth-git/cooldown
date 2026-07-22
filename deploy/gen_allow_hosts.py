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
# mitm.it serves the CA to clients through the proxy, so it must be intercepted too.
EXTRA = ["mitm.it"]


def pattern(domain):
    esc = domain.replace(".", "[.]")
    return f"^(.+[.])?{esc}([:][0-9]+)?$"


def main():
    regex = "|".join(pattern(d) for d in CORE + NEWS_DOMAINS + EXTRA)
    # systemd needs a literal $ written as $$ in ExecStart; --plain skips that.
    print(regex if "--plain" in sys.argv else regex.replace("$", "$$"))


if __name__ == "__main__":
    main()
