"""Single source of truth for the gated NEWS category.

News is open-ended — enumerating every site is whack-a-mole — so instead of the
per-site allowlist the other gated sites use, the whole category shares ONE list
here, and one shared budget pool ("main"). That's deliberate: because news draws
from the SAME bucket as Reddit/YouTube, hopping sites to dodge a spent budget
doesn't work — it's all one "distraction" allowance.

Quality outlets are included on purpose. The gate is a *budget*, not a block —
you can still read good journalism, just not endlessly.

To add a site: add ONE domain below, then regenerate the proxy allowlist
(`python3 deploy/gen_allow_hosts.py`) and restart the proxy. That's it — app.py
and addon.py both import this list, so there's no third place to edit.

List registrable domains (match covers the domain + any subdomain). For aggregators
that live on a shared apex, list the SPECIFIC subdomain (e.g. news.google.com), never
the bare apex (never google.com), so the gate can't swallow unrelated sites.
"""

NEWS_DOMAINS = [
    # US national / cable
    "cnn.com", "foxnews.com", "msnbc.com", "nbcnews.com", "cbsnews.com",
    "abcnews.go.com", "usatoday.com", "nypost.com", "nydailynews.com",
    "latimes.com", "chicagotribune.com", "bostonglobe.com", "sfgate.com",
    # Papers of record / business
    "nytimes.com", "washingtonpost.com", "wsj.com", "theguardian.com",
    "ft.com", "economist.com", "bloomberg.com", "cnbc.com", "forbes.com",
    "marketwatch.com",
    # Wire services / public / nonprofit
    "apnews.com", "reuters.com", "npr.org", "pbs.org", "propublica.org",
    # Politics & opinion (across the spectrum — a lot of the rage-bait lives here)
    "politico.com", "thehill.com", "axios.com", "realclearpolitics.com",
    "vox.com", "slate.com", "salon.com", "motherjones.com", "newrepublic.com",
    "thedailybeast.com", "mediaite.com", "rawstory.com", "dailykos.com",
    "nationalreview.com", "dailywire.com", "dailycaller.com", "theblaze.com",
    "breitbart.com", "washingtonexaminer.com", "washingtontimes.com",
    "thefederalist.com", "theepochtimes.com", "newsmax.com", "oann.com",
    "zerohedge.com", "thegatewaypundit.com",
    # Magazines / longform
    "theatlantic.com", "newyorker.com", "time.com", "newsweek.com",
    "businessinsider.com", "insider.com", "vanityfair.com", "theintercept.com",
    # Entertainment / gossip rage-bait
    "tmz.com", "buzzfeed.com", "buzzfeednews.com", "huffpost.com", "vice.com",
    # UK tabloids & broadsheets
    "dailymail.co.uk", "thesun.co.uk", "mirror.co.uk", "express.co.uk",
    "metro.co.uk", "telegraph.co.uk", "independent.co.uk", "standard.co.uk",
    # International
    "bbc.com", "bbc.co.uk", "aljazeera.com", "dw.com", "france24.com", "cbc.ca",
    # Aggregators (specific subdomains only — never the bare apex)
    "news.google.com", "apple.news", "drudgereport.com", "memeorandum.com",
    "ground.news", "allsides.com",
]
