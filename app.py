from flask import Flask, jsonify, redirect, request
from urllib.parse import urlparse
from collections import Counter, deque
from datetime import datetime, timezone
import os
import subprocess
import json
import re
import random
import redis
import secrets
import time
import traceback
import uuid
from apscheduler.schedulers.background import BackgroundScheduler
from news_domains import NEWS_DOMAINS

# No CORS: the gate and the injected heartbeat are same-origin with the gated host,
# and the monitoring pages are same-origin with the box. A wildcard
# Access-Control-Allow-Origin only widened the attack surface, and its absence is now
# load-bearing: it is what stops a script on reddit.com reading the monitoring pages
# cross-origin now that they live somewhere else. CSRF on the mutating endpoints is
# enforced at the proxy boundary (addon.py rejects cross-site requests to /budget/*).
app = Flask(__name__)
# Redis lives on localhost for the native/Pi deploy; in Docker it's a separate
# service, so honor REDIS_HOST/REDIS_PORT (defaults preserve native behaviour).
r = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)

# ---------------------------------------------------------------------------
# Where the monitoring pages live.
#
# /stats, /health and /devices used to be served under /budget/* on the GATED site's
# origin, which meant every script on that site was same-origin with them. Two rounds
# of header checks narrowed that (see F9) but could never close it: a script that gets
# one click can window.open a same-origin page and read it, and no header distinguishes
# that from the user opening it themselves.
#
# So they moved. They are now served ONLY on the box's own origin — reachable over the
# tailnet, never through the proxy — which makes a gated site's scripts cross-origin
# with them. With no CORS headers the browser will not hand over a response, and a
# cross-origin window is not readable. That is a boundary the browser enforces for us
# instead of one we assert with headers.
#
# LISTEN_HOST stays loopback-only for the gate itself (the addon talks to it there), and
# the tailnet address is added as a second listener so your phone can reach the
# dashboard. Bound narrowly rather than 0.0.0.0 so a missing firewall rule is not the
# only thing standing between /devices and the LAN.
# ---------------------------------------------------------------------------
MONITOR_PORT = int(os.environ.get("COOLDOWN_MONITOR_PORT", "5000"))
TAILNET_IFACE = os.environ.get("COOLDOWN_TAILNET_IFACE", "tailscale0")

def _iface_ipv4(name):
    """The interface's IPv4 address, or None. ioctl rather than a subprocess so this
    is cheap enough to call in a startup retry loop and works before tailscaled has
    finished coming up."""
    import fcntl
    import socket
    import struct
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(), 0x8915,                                  # SIOCGIFADDR
            struct.pack("256s", name[:15].encode()))[20:24])
    except OSError:
        return None                                              # no such iface / no address yet
    finally:
        s.close()

def monitor_origin(ip=None):
    """The absolute base URL the gate should link to for the dashboard, or "" if the
    box has no tailnet address (in which case the gate omits the links rather than
    pointing at something unreachable)."""
    override = os.environ.get("COOLDOWN_MONITOR_ORIGIN")
    if override:
        return override.rstrip("/")
    ip = ip or _iface_ipv4(TAILNET_IFACE)
    return f"http://{ip}:{MONITOR_PORT}" if ip else ""

# Per-site config. Add a site here and the proxy + budget logic pick it up.
#
# SINGLE SHARED BUCKET: every site draws from ONE spent counter (the "main" pool),
# but each keeps its OWN cap (budget_seconds). So spending anywhere drains the shared
# bucket, and a site is usable only while spent < its own cap. With Reddit=10m and
# YouTube=15m: burn 10m on Reddit and Reddit is out, but YouTube still shows 5m left.
# Redis pool state is keyed by the group ("main"): spent:{pool}, cooldown:{pool},
# last_heartbeat:{pool}. Per-site session state stays keyed by site (active_token:{site}).
SITES = {
    "reddit": {
        "home": "https://www.reddit.com",
        "budget_seconds": 10 * 60,
        "label": "Reddit",
        "emoji": "🤙",
        "group": "main",
    },
    "youtube": {
        "home": "https://www.youtube.com",
        "budget_seconds": 15 * 60,
        "label": "YouTube",
        "emoji": "🎬",
        "group": "main",
    },
    "spotify": {
        "home": "https://open.spotify.com",
        "budget_seconds": 10 * 60,
        "label": "Spotify",
        "emoji": "🎧",
        "group": "main",
    },
    "puzzmo": {
        "home": "https://www.puzzmo.com/today/",
        "budget_seconds": 10 * 60,
        "label": "Puzzmo",
        "emoji": "🧩",
        "group": "main",
    },
    # News is a CATEGORY, not one site: it matches the whole NEWS_DOMAINS list and
    # shares the "main" bucket, so switching between news sites (or from Reddit to a
    # news site) never buys fresh time — one distraction allowance for all of it.
    # "home" is only a rare fallback (Enter almost always returns you to the article
    # you were opening); a neutral non-news page keeps it from being an escape hatch.
    "news": {
        "home": "https://www.google.com",
        "budget_seconds": 10 * 60,
        "label": "News",
        "emoji": "📰",
        "group": "main",
    },
}
DEFAULT_SITE = "reddit"

# One chart colour per site, in SITES order. Validated against the card surface
# (#14171d, dark) for lightness band, chroma floor, adjacent-pair colour-vision-deficiency
# separation, normal-vision separation and contrast — all pass.
#
# SIX IS THE CEILING this surface supports. A seventh hue was tried and fails: the best
# candidate lands 13.5 ΔE from the teal against a floor of 15, meaning full-colour readers
# could not reliably tell those two series apart. So do not just append one — validate it,
# or the chart quietly starts lying about which site is which.
#
# The previous version had five colours for five sites and picked with `i % len(...)`, so
# a sixth site would have silently drawn in an existing site's colour. Cycling categorical
# hues is never right: colour is identity, and a repeated hue asserts an identity that
# isn't there. Overflow now gets a neutral grey instead — visibly "unassigned" rather than
# a false match — and a test fails before it can happen unnoticed.
SERIES_COLORS = ["#3987e5", "#199e70", "#c98500", "#a678de", "#d1495b", "#0d9c94"]
SERIES_OVERFLOW = "#6b7280"

def series_color(i):
    return SERIES_COLORS[i] if i < len(SERIES_COLORS) else SERIES_OVERFLOW

# How long the behavioural history is kept. Was 100 days, which quietly made a
# year-in-review impossible: the data expired before the year was up. Every one of these
# keys is a short string keyed by day, so a full year of them is a few megabytes — Redis
# holds 28 days in 1.2 MB today. Long enough to see a year, short enough that it is still
# a rolling window rather than a permanent record of your behaviour.
HISTORY_TTL = 400 * 86400

RAPID_REPEAT_WINDOW = 3 * 60 * 60  # "a few hours" — a cooldown starting within this of
                                   # the previous one is a "rapid repeat" (binge clustering).
                                   # Also the window escalating cooldowns look back over.
# Escalating cooldowns: a lone cooldown is 1 hour, but back-to-back re-binges (each new
# cooldown starting within RAPID_REPEAT_WINDOW of the previous) get a progressively longer
# wall. The index is how many prior cooldowns already sit in that trailing window, so a
# spread-out day always stays at the 1-hour base; only clustering escalates.
COOLDOWN_LADDER = [60 * 60, 90 * 60, 120 * 60, 180 * 60]  # 1h · 1.5h · 2h · 3h (capped)
COOLDOWN_SECONDS = COOLDOWN_LADDER[0]   # base / back-compat default

# Soft-pause cluster brake: if ONE site trips its own cap CLUSTER_THRESHOLD times inside a
# rolling CLUSTER_WINDOW, a short site-specific cooldown breaks the loop. A lighter, targeted
# cousin of the hard pool cooldown — only bites the Nth re-max in the window, not each one.
CLUSTER_WINDOW = 2 * 60 * 60        # rolling look-back window
CLUSTER_THRESHOLD = 3               # the Nth cap-hit inside the window trips it
CLUSTER_COOLDOWN_SECONDS = 25 * 60  # a short breather, not the 1h hard wall
SESSION_IDLE_TTL = 120         # 2 min without a foreground ping = session expires
HEARTBEAT_MAX_GAP = 30         # gaps between pings larger than this aren't charged (idle/away)
# Passive refill: while nothing in the pool is being actively used, spent ticks back
# down so partial use recovers over time. Rate is set so a fully-drained bucket (the
# largest cap) refills to full after this many seconds fully idle (~1 hour).
REFILL_FULL_SECONDS = 60 * 60
# ...but refill only kicks in after this long OFF the sites (grace window). Briefly
# waiting gives back nothing, so you can't wait a minute and sip another scroll;
# genuinely stepping away for a while still recovers time. Anti-binge lever.
REGEN_DELAY = 15 * 60

# Night mode (soft bedtime curfew). During [NIGHT_START_HOUR, NIGHT_END_HOUR) local
# time the shared bucket is capped small (NIGHT_BUDGET_SECONDS) AND refill is OFF — so
# you get one brief buffer if you truly need it, then the sites stay closed until the
# morning reset (which is moved to NIGHT_END_HOUR). No hard lock, so Tailscale-off is
# still the escape hatch; this is friction, not a wall.
NIGHT_START_HOUR = 23          # 11pm local — full night mode begins
NIGHT_END_HOUR = 7             # 7am local  (also when the daily reset fires)
NIGHT_BUDGET_SECONDS = 5 * 60
# Wind-down: for this long BEFORE night, each site's cap ramps linearly from its daytime
# budget down to the night buffer (and refill turns off), easing you toward lights-out
# instead of a sudden 11pm wall. Study mode stays available at all hours regardless.
WINDDOWN_SECONDS = 60 * 60

# YouTube "study mode" allowlist. Entering study mode grants a FREE session (no
# budget charge, ignores cooldown) that the proxy LOCKS to these playlists —
# search / home feed / Shorts / other channels bounce back to the course. To add a
# course: open its playlist on YouTube and copy the value after "list=" in the URL.
# Keep this list in sync with STUDY_PLAYLISTS in addon.py.
# Study mode: a free, always-open escape hatch locked to an allow-listed YouTube playlist.
# OFF by default — put one or more playlist IDs here AND in addon.py (both lists must match)
# to switch it on. Ships off because a placeholder ID renders a study button that goes
# nowhere, and because the feature only earns its keep if you'll genuinely use it.
STUDY_PLAYLISTS = []

# ---------------------------------------------------------------------------
# THEMES. Every page declares the same core CSS variables, so a theme is just a second
# :root block appended after the page's own — no per-page work, and removing the feature
# is deleting this section.
#
# ONE HARD RULE: --card stays dark. The chart palettes (SERIES_COLORS, CPU_LINE_COLORS)
# were validated for contrast and colour-vision separation against a #14171d surface. A
# theme that lightened the cards would silently invalidate all of that, and the charts
# would stop being readable for exactly the people the validation protects. Themes may
# repaint the backdrop, the accents and the chrome; they may not lighten the surface the
# data sits on. There is a test.
#
# The traffic canvas is also deliberately left alone: its green/red is *semantic* —
# encrypted versus in-the-clear — so recolouring it to match a theme would be making a
# meaningful signal decorative.
# ---------------------------------------------------------------------------
THEMES = {
    "christmas": {
        "label": "Christmas", "emoji": "\U0001F384",
        "season": lambda t: (t.tm_mon == 12 and 18 <= t.tm_mday <= 26),
        "lines": ["It's Christmas. This will be the exact same feed on the 27th.",
                  "Today's posts are mostly people being bored in a different house.",
                  "The whole internet is doing this today. Doesn't make it worth doing."],
        "vars": {"bg": "#0a1410", "card": "#10201a", "line": "#1e3a2c", "fg": "#f2f8f4",
                 "muted": "#8fa89a", "faint": "#5d7568", "go": "#3ecf7c",
                 "wait": "#e0b458", "bad": "#d1495b", "sleep": "#7fb3a0",
                 "accent": "#d1495b", "good": "#3ecf7c", "warn": "#e0b458"},
    },
    "halloween": {
        "label": "Halloween", "emoji": "\U0001F383",
        "season": lambda t: (t.tm_mon == 10 and t.tm_mday >= 25),
        "lines": ["Nothing in here is scarier than the screen-time report.",
                  "The feed is a haunted house with no exit. You know this one.",
                  "You can leave. That's the twist."],
        "vars": {"bg": "#100a14", "card": "#19111f", "line": "#2e2038", "fg": "#f5f1f8",
                 "muted": "#a294ad", "faint": "#6b5c78", "go": "#8f57d4",
                 "wait": "#e8892b", "bad": "#d1495b", "sleep": "#8f57d4",
                 "accent": "#e8892b", "good": "#8f57d4", "warn": "#e8892b"},
    },
    "newyear": {
        "label": "New Year", "emoji": "\U00002728",
        "season": lambda t: ((t.tm_mon == 12 and t.tm_mday >= 30)
                             or (t.tm_mon == 1 and t.tm_mday <= 2)),
        "lines": ["Whatever you resolved, this probably wasn't it.",
                  "New year, same feed. It didn't change over the holiday.",
                  "Day one of the new you, and here you are."],
        "vars": {"bg": "#0a0a0f", "card": "#15151d", "line": "#2b2b3a", "fg": "#f8f5ee",
                 "muted": "#a39c8b", "faint": "#6d6758", "go": "#3ecf7c",
                 "wait": "#d9b45c", "bad": "#d1495b", "sleep": "#7aa2ff",
                 "accent": "#d9b45c", "good": "#3ecf7c", "warn": "#d9b45c"},
    },
    # Spontaneous — no season, so they only ever turn up unannounced.
    "retro": {
        "label": "Outrun", "emoji": "\U0001F31E", "season": None,
        "vars": {"bg": "#0d0916", "card": "#170f24", "line": "#33204a", "fg": "#f4eeff",
                 "muted": "#a892c0", "faint": "#6f5c8a", "go": "#0fa8b8",
                 "wait": "#d9548f", "bad": "#d9548f", "sleep": "#8f57d4",
                 "accent": "#d9548f", "good": "#0fa8b8", "warn": "#d9548f"},
    },
    "terminal": {
        "label": "Phosphor", "emoji": "\U0001F4DF", "season": None,
        "vars": {"bg": "#080a07", "card": "#11140e", "line": "#26301c", "fg": "#e6e4c4",
                 "muted": "#96a077", "faint": "#616a4c", "go": "#7a9e2f",
                 "wait": "#c9a227", "bad": "#c05a3a", "sleep": "#7a9e2f",
                 "accent": "#7a9e2f", "good": "#7a9e2f", "warn": "#c9a227"},
    },
    # No emoji on purpose — see the note above _personal_theme.
    "birthday": {
        "label": "", "emoji": "", "season": None,
        "vars": {"bg": "#0f0a14", "card": "#1a1020", "line": "#3a2450", "fg": "#f8f0ff",
                 "muted": "#b79ccc", "faint": "#7d6390", "go": "#3ecf7c",
                 "wait": "#e0b458", "bad": "#d1495b", "sleep": "#a678de",
                 "accent": "#e0b458", "good": "#3ecf7c", "warn": "#e0b458"},
    },
    "anniversary": {
        "label": "", "emoji": "", "season": None,
        "vars": {"bg": "#080d12", "card": "#101820", "line": "#1e3040", "fg": "#eef6fa",
                 "muted": "#8fa9bb", "faint": "#5d7788", "go": "#3ecf7c",
                 "wait": "#5aa8d8", "bad": "#d1495b", "sleep": "#7aa2ff",
                 "accent": "#5aa8d8", "good": "#3ecf7c", "warn": "#5aa8d8"},
    },
    "ember": {
        "label": "Ember", "emoji": "\U0001F525", "season": None,
        "vars": {"bg": "#120b08", "card": "#1d1210", "line": "#3a221a", "fg": "#faf0ea",
                 "muted": "#b8968a", "faint": "#7d6055", "go": "#3ecf7c",
                 "wait": "#d1732b", "bad": "#d1495b", "sleep": "#c08420",
                 "accent": "#d1732b", "good": "#3ecf7c", "warn": "#d1732b"},
    },
    "slate": {
        "label": "Slate", "emoji": "\U0001F5FF", "season": None,
        "vars": {"bg": "#0a0d11", "card": "#12161c", "line": "#232b34", "fg": "#eef1f4",
                 "muted": "#93a0ad", "faint": "#616d79", "go": "#3ecf7c",
                 "wait": "#8fa3b8", "bad": "#d1495b", "sleep": "#7aa2ff",
                 "accent": "#8fa3b8", "good": "#3ecf7c", "warn": "#c98500"},
    },
    "frost": {
        "label": "Frost", "emoji": "\U00002744", "season": None,
        "vars": {"bg": "#080d12", "card": "#101820", "line": "#1e2f3d", "fg": "#eef5fa",
                 "muted": "#8ba3b5", "faint": "#5a7183", "go": "#3ecf7c",
                 "wait": "#5aa8d8", "bad": "#d1495b", "sleep": "#7aa2ff",
                 "accent": "#5aa8d8", "good": "#3ecf7c", "warn": "#5aa8d8"},
    },
}

# Two one-day anchors, to put something in the nine months that have no season.
#
# NEITHER DATE IS IN THIS FILE, and that is the point. A birthday hardcoded here would be
# committed to a public repository forever, in the history, indexed — a certain disclosure
# rather than a hypothetical one, and a far bigger exposure than anything on the box. It
# comes from an environment variable set on the box alone: COOLDOWN_BIRTHDAY=MM-DD.
#
# The birthday theme is also deliberately UNLABELLED on the gate. The gate is served on
# the gated site's origin, so a script on Reddit can fetch it; a cake emoji there would
# hand that script the date once a year. Nice colours reveal nothing. The greeting itself
# lives on the dashboard, which is on the box's own origin and cross-origin to Reddit.
BIRTHDAY = os.environ.get("COOLDOWN_BIRTHDAY", "")      # "MM-DD", box-local, never in git
PERSONAL_THEMES = {"birthday", "anniversary"}           # date-driven, never picked at random

def _anniversary_md():
    """MM-DD the box first ran. Backfilled from the oldest usage day so an existing
    install gets its real anniversary rather than the day this feature shipped."""
    v = _try(lambda: r.get("first_run"))
    return v[5:] if v and len(v) == 10 else None

def _personal_theme(t):
    md = f"{t.tm_mon:02d}-{t.tm_mday:02d}"
    if BIRTHDAY and md == BIRTHDAY.strip():
        return "birthday"
    if md == (_anniversary_md() or ""):
        return "anniversary"
    return None

# How often an unprompted theme turns up. Seeded by the date, never live-random, so a
# refresh cannot reroll it — same reasoning as the reflection prompt: something that
# changes when you look at it is a slot machine, not a surprise.
SPONTANEOUS_ODDS = 0.22

def active_theme(now=None, override=None):
    """(name, theme) for right now, or (None, None) for the standard look."""
    now = now if now is not None else time.time()
    if override is not None:
        return (override, THEMES[override]) if override in THEMES else (None, None)
    t = time.localtime(now)
    personal = _try(lambda: _personal_theme(t))
    if personal:
        return personal, THEMES[personal]
    for name, th in THEMES.items():
        if th["season"] and th["season"](t):
            return name, th
    # PERSONAL themes are date-driven like seasonal ones; they just carry no season
    # predicate because their date isn't in this file. Without this they'd fall into the
    # random pool and your birthday theme would show up on a wet Tuesday in March.
    spontaneous = [n for n, th in THEMES.items()
                   if not th["season"] and n not in PERSONAL_THEMES]
    seed = random.Random("theme:" + time.strftime("%Y-%m-%d", t))
    if seed.random() < SPONTANEOUS_ODDS:
        return (lambda n: (n, THEMES[n]))(seed.choice(sorted(spontaneous)))
    return None, None

def theme_css(theme):
    if not theme:
        return ""
    return ":root{" + "".join(f"--{k}:{v};" for k, v in theme["vars"].items()) + "}"

BUDGET_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>{{ label }} · Countdown</title>
    {% if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{% endif %}
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --go:#3ecf7c; --wait:#f0a63a; --sleep:#7aa2ff;
        }
        *{box-sizing:border-box}
        html,body{height:100%;margin:0}
        body{
            background:radial-gradient(1200px 620px at 50% -15%, #181c24, var(--bg));
            color:var(--fg);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;
            display:flex;align-items:center;justify-content:center;
            min-height:100dvh;padding:24px;
            padding-bottom:max(24px,env(safe-area-inset-bottom));
        }
        /* Ambient "encrypted traffic through the porthole" background (canvas). */
        #bp-bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;display:block}
        .card{
            position:relative;z-index:1;overflow:hidden;
            width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);
            border-radius:20px;padding:38px 28px 30px;text-align:center;
            box-shadow:0 24px 70px rgba(0,0,0,.55);
            --accent:var(--wait);
        }
        .card.go{--accent:var(--go)} .card.wait{--accent:var(--wait)} .card.sleep{--accent:var(--sleep)}
        .card::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent)}
        .kicker{
            display:flex;align-items:center;justify-content:center;gap:8px;
            font-size:11.5px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
            color:var(--muted);margin-bottom:20px;
        }
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--accent)}
        .big{
            font-size:64px;font-weight:700;letter-spacing:-2px;line-height:1;margin:2px 0 0;
            color:var(--accent);font-variant-numeric:tabular-nums;
        }
        h1{font-size:19px;font-weight:600;margin:16px 0 0;letter-spacing:-.2px}
        p{color:var(--muted);font-size:14.5px;line-height:1.55;margin:10px auto 0;max-width:30ch}
        .actions{margin-top:28px;display:flex;flex-direction:column;gap:10px}
        button{
            width:100%;padding:15px;font-size:16px;font-weight:600;border:none;
            border-radius:12px;cursor:pointer;-webkit-tap-highlight-color:transparent;
            transition:transform .05s ease,opacity .15s ease;
        }
        button:active{transform:scale(.985)}
        .enter{background:var(--go);color:#06120b}
        .study{background:transparent;color:var(--muted);border:1px solid var(--line)}
        .study:active{opacity:.7}
        /* Promoted to primary on the cooldown screens — the productive door out. */
        .study-cta{background:var(--sleep);color:#0a1020;border:none;font-weight:600}
        .blocked{background:#1c2028;color:var(--muted);cursor:default}
        .hint{font-size:12px;color:#5f6773;margin-top:2px}
        .foot{display:block;margin-top:18px;font-size:12px;color:#5f6773;text-decoration:none}
        .foots{display:flex;gap:18px;justify-content:center;align-items:center;margin-top:18px}
        .infobtn{width:19px;height:19px;padding:0;border-radius:50%;border:1px solid var(--line);
            background:transparent;color:#5f6773;font:600 11px/1 -apple-system,Roboto,Arial,sans-serif;
            cursor:pointer;flex:none}
        .infobtn:hover{color:var(--fg);border-color:#3a4150}
        /* Explains the live traffic feed behind the page. Sits inside the card (which clips
           overflow), so it covers the content rather than escaping the rounded corners. */
        .bgpanel{position:absolute;inset:0;z-index:3;display:none;flex-direction:column;
            justify-content:flex-start;gap:9px;padding:20px 22px;text-align:left;
            overflow-y:auto;-webkit-overflow-scrolling:touch;
            background:rgba(14,17,22,.93);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
        .bgpanel.on{display:flex}
        .bgpanel h2{margin:0 0 1px;font-size:14.5px;font-weight:700;letter-spacing:-.2px;text-align:center}
        .bgpanel p{margin:0;font-size:12px;line-height:1.45;color:var(--muted);max-width:none}
        .bgpanel .key{display:flex;gap:9px;align-items:flex-start;font-size:12.5px;line-height:1.45;color:var(--fg)}
        .bgpanel .sw{width:9px;height:9px;border-radius:2px;flex:none;margin-top:4px}
        .bgpanel .sw.g{background:#4ede8c} .bgpanel .sw.r{background:#f06060}
        .bgpanel .pct{margin-left:auto;padding-left:8px;font-variant-numeric:tabular-nums;
            font-weight:700;font-size:12.5px;color:var(--fg)}
        .bgbar{display:flex;height:9px;border-radius:5px;overflow:hidden;background:#0e1116;margin:2px 0 4px}
        .bgbar i{display:block;height:100%;width:0;transition:width .4s}
        #bgBarG{background:#4ede8c} #bgBarR{background:#f06060}
        .bgpanel .fine{font-size:11px;line-height:1.4;color:var(--faint,#5f6773)}
        .bgpanel .dismiss{margin-top:auto;flex:none;background:transparent;border:1px solid var(--line);
            color:var(--muted);border-radius:9px;padding:9px;font-size:13px;width:100%;cursor:pointer}
        .foots .foot{margin-top:0}
        /* Pre-entry reflection: a why-am-I-here pause with concrete alternatives. */
        .r-q{font-size:15px;color:var(--fg);font-weight:600;margin:2px 0 14px;line-height:1.45}
        .chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
        .chip{width:auto;padding:9px 13px;font-size:13.5px;font-weight:600;border-radius:999px;
              background:#1c2028;color:var(--fg);border:1px solid var(--line)}
        .chip.sel{background:var(--accent);color:#0a1020;border-color:transparent}
        .r-list{margin-top:16px;text-align:left}
        .r-lead{font-size:13px;color:var(--muted);margin:0 0 10px}
        .r-list ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:9px}
        .r-list li{display:flex;gap:9px;align-items:flex-start;font-size:14px;color:var(--fg);line-height:1.4}
        .r-list li::before{content:"\\25CB";color:var(--accent)}
        /* Post-session check-in. Small and skippable — it must never feel like a toll. */
        .worth{margin-top:20px;padding-top:16px;border-top:1px solid var(--line)}
        .worth-q{font-size:13.5px;color:var(--muted);margin:0 0 10px}
        .worth-btns{display:flex;gap:8px}
        .worth-btns form{flex:1}
        .wbtn{width:100%;padding:11px;font-size:14px;font-weight:600;border-radius:10px;
              background:#1c2028;color:var(--fg);border:1px solid var(--line)}
        .wbtn:active{opacity:.7}
        .pass{background:var(--go);color:#06120b}
        .cont{background:transparent;color:var(--muted);border:1px solid var(--line)}
        .cont:active{opacity:.7}
    </style>
</head>
<body>
    <canvas id="bp-bg" aria-hidden="true"></canvas>
    <div class="card {{ mood }}">
        <div class="kicker"><span class="dot"></span>{% if theme_emoji %}{{ theme_emoji }} {% endif %}{{ overline }}</div>
        {% if countdown %}<div id="cd" class="big" data-secs="{{ countdown }}">·</div>
        {% elif headline %}<div class="big">{{ headline }}</div>{% endif %}
        {% if title %}<h1>{{ title }}</h1>{% endif %}
        <p>{{ message }}</p>
        <div class="actions">
            {% if can_enter and not show_reflect %}
            <form action="/budget/enter?site={{ site }}{% if next_url %}&next={{ next_url|urlencode }}{% endif %}" method="post">
                <button class="enter" type="submit">Enter {{ label }}</button>
            </form>
            {% elif can_enter %}
            <button class="enter" type="button" id="beginBtn">Enter {{ label }}</button>
            <div id="passMsg" hidden>
              <div class="kicker"><span class="dot"></span>{{ pass_line.0 }}</div>
              <h1>{{ pass_line.1 }}</h1>
              <p>{{ pass_line.2 }}</p>
            </div>
            <div id="reflect" hidden>
                <p class="r-q">{{ reflect_q }}</p>
                <div class="chips" id="chips"></div>
                <div class="r-list" id="rlist" hidden>
                    <p class="r-lead" id="rlead"></p>
                    <ul id="ritems"></ul>
                </div>
                <div class="actions" id="ractions" hidden>
                    <button class="pass" type="button" id="passBtn">I'll pass — I'm good</button>
                    <form action="/budget/enter?site={{ site }}{% if next_url %}&next={{ next_url|urlencode }}{% endif %}" method="post">
                        <input type="hidden" name="trigger" id="trigField" value="">
                        <button class="cont" type="submit">Continue to {{ label }} anyway</button>
                    </form>
                </div>
            </div>
            {% elif button_text %}
            <button class="blocked" disabled>{{ button_text }}</button>
            {% endif %}
            {% if show_study %}
            <form action="/budget/study?site={{ site }}" method="post">
                <button class="study{% if study_primary %} study-cta{% endif %}" type="submit">{% if study_primary %}Study while you wait{% else %}Study mode{% endif %}</button>
            </form>
            <div class="hint">{% if study_primary %}Turn the break into real progress — locked to the course, no scrolling.{% else %}Locked to the course playlist — no scrolling.{% endif %}</div>
            {% endif %}
        </div>
        <!-- Absolute links to the BOX, not relative ones onto the gated site: the
             dashboard deliberately no longer exists on this origin. -->
        {% if ask_worth %}
        <div class="worth">
          <p class="worth-q">That session just ended. Was it worth it?</p>
          <div class="worth-btns">
            <form action="/budget/worth" method="post"><input type="hidden" name="v" value="yes">
              <button class="wbtn" type="submit">Yes</button></form>
            <form action="/budget/worth" method="post"><input type="hidden" name="v" value="no">
              <button class="wbtn" type="submit">Not really</button></form>
          </div>
        </div>
        {% endif %}
        <div class="foots">{% if mon %}<a class="foot" href="{{ mon }}/stats?from={{ site }}">Usage stats</a><a class="foot" href="{{ mon }}/health?from={{ site }}">Pi health</a>{% endif %}<button class="infobtn" id="bgInfoBtn" type="button" aria-label="What is the moving background?">i</button></div>
        <div class="bgpanel" id="bgPanel">
            <h2>The moving background</h2>
            <p>A live picture of the web traffic passing through this box right now.</p>
            <div class="bgbar"><i id="bgBarG"></i><i id="bgBarR"></i></div>
            <div class="key"><span class="sw g"></span><span><b>Encrypted</b> (HTTPS) — scrambled, unreadable. Exactly what you want.</span><span class="pct" id="bgPctG">—</span></div>
            <div class="key"><span class="sw r"></span><span><b>In the clear</b> (DNS + plain HTTP) — readable by anyone in between.</span><span class="pct" id="bgPctR">—</span></div>
            <p class="fine" id="bgRate">measuring…</p>
            <p class="fine"><b>What's counted:</b> web traffic through this box — HTTPS (:443), HTTP (:80)
               and DNS (:53). Not other protocols, and not devices that aren't routed through the box.
               So it's your web browsing, not literally everything.</p>
            <p class="fine">The scrolling lines are a stand-in, not your actual packets — the
               <b>proportions and the volume are real</b>, the hex digits are not. Nothing is recorded
               or sent anywhere.</p>
            <button class="dismiss" id="bgInfoClose" type="button">Got it</button>
        </div>
    </div>
    {% if countdown %}
    <script>
    (function(){
        var el=document.getElementById("cd");
        // Anchor to an absolute deadline and derive the remaining time from the wall
        // clock every tick. A plain `s--` counter drifts whenever the browser throttles
        // timers (backgrounded tab, locked phone), so it lagged reality until a manual
        // refresh. Computing (deadline - now) is self-correcting; hitting zero reloads
        // to re-sync with the server's authoritative value.
        var deadline=Date.now()+parseInt(el.dataset.secs,10)*1000;
        function fmt(n){var h=Math.floor(n/3600),m=Math.floor(n%3600/60),x=n%60,p=function(v){return String(v).padStart(2,"0")};
            return h?h+":"+p(m)+":"+p(x):m+":"+p(x);}
        function tick(){ var s=Math.round((deadline-Date.now())/1000);
            if(s<=0){location.reload();return;} el.textContent=fmt(s); }
        tick(); setInterval(tick,1000);
        // Recompute immediately on return, don't wait for the next (throttled) tick.
        document.addEventListener("visibilitychange",function(){ if(!document.hidden) tick(); });
    })();
    </script>
    {% endif %}
    {% if can_enter and show_reflect %}{% raw %}
    <script>
    (function(){
        var TRIGGERS = [
          { key:"tired", label:"\\uD83D\\uDE34 Tired", lead:"Rest \\u2014 scrolling won't recharge you.",
            items:["Put the phone down, eyes closed for 10 min","Drink a glass of water","If it's late, just go to bed","Step outside for 2 min of air"] },
          { key:"bored", label:"\\uD83D\\uDE10 Bored", lead:"Boredom is a nudge, not an emergency.",
            items:["Text someone you've meant to","5 minutes on one to-do","Open that book or a saved article","Sit with it for 60s \\u2014 it passes"] },
          { key:"stressed", label:"\\uD83D\\uDE30 Stressed", lead:"A feed won't settle this.",
            items:["5 slow breaths \\u2014 in 4, out 6","Write down what's on your mind","Short walk, even around the room","Do one small thing you can control"] },
          { key:"avoiding", label:"\\uD83D\\uDE2C Avoiding something", lead:"What are you putting off?",
            items:["Name the thing you're dodging","Do just its first 2 minutes","Shrink it to one tiny step","Set a 10-min timer and start"] },
          { key:"habit", label:"\\uD83D\\uDD01 Just habit", lead:"You reached without deciding.",
            items:["Did I actually mean to open this?","Put the phone in another room","One slow breath, then choose","Do what you picked it up to avoid"] },
          { key:"need", label:"\\u2705 I actually need it", lead:"Fair enough \\u2014 be deliberate.",
            items:["Get in, get what you need, get out","Hold a rough time limit in mind","Then back to the real thing"] }
        ];
        for(var i=TRIGGERS.length-1;i>0;i--){ var j=Math.floor(Math.random()*(i+1));
          var t=TRIGGERS[i]; TRIGGERS[i]=TRIGGERS[j]; TRIGGERS[j]=t; }   // no fixed positions to memorise
        var reflect=document.getElementById("reflect"); if(!reflect) return;
        var picked="";
        var begin=document.getElementById("beginBtn"), chips=document.getElementById("chips"),
            list=document.getElementById("rlist"), lead=document.getElementById("rlead"),
            items=document.getElementById("ritems"), actions=document.getElementById("ractions"),
            pass=document.getElementById("passBtn");
        begin.addEventListener("click",function(){ begin.style.display="none"; reflect.hidden=false; });
        TRIGGERS.forEach(function(t){
            var b=document.createElement("button"); b.type="button"; b.className="chip"; b.textContent=t.label;
            b.addEventListener("click",function(){
                [].forEach.call(chips.children,function(c){ c.classList.remove("sel"); });
                b.classList.add("sel"); picked=t.key;
                var hid=document.getElementById("trigField"); if(hid) hid.value=t.key;
                lead.textContent=t.lead; items.innerHTML="";
                t.items.forEach(function(it){ var li=document.createElement("li"); li.textContent=it; items.appendChild(li); });
                list.hidden=false; actions.hidden=false;
            });
            chips.appendChild(b);
        });
        pass.addEventListener("click",function(){
            if(picked){ var fd=new FormData(); fd.append("trigger",picked);
              fetch("/budget/reflect",{method:"POST",body:fd,keepalive:true}).catch(function(){}); }
            var msg=document.getElementById("passMsg");
            document.querySelector(".card").innerHTML = msg ? msg.innerHTML : "";
        });
    })();
    </script>
    {% endraw %}{% endif %}
"""

# The ambient background block, spliced back in below.
BG_BLOCK = """    {% raw %}
    <script>
    (function(){
      var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
      var FEED=(document.querySelector('meta[name="cd-feed"]')||{}).content||"/budget/feed";
      var b=document.getElementById("bgInfoBtn"), p=document.getElementById("bgPanel"),
          x=document.getElementById("bgInfoClose");
      if(b&&p){
        var timer=null;
        function rate(n){ if(n<1024) return n+" B/s"; if(n<1048576) return (n/1024).toFixed(n<10240?1:0)+" KB/s"; return (n/1048576).toFixed(1)+" MB/s"; }
        function refresh(){
          fetch(FEED+"?t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){return r.json();})
            .then(function(d){
              var e=d.enc||0, u=d.unenc||0, tot=e+u;
              var g=document.getElementById("bgPctG"), rr=document.getElementById("bgPctR"),
                  bg=document.getElementById("bgBarG"), br=document.getElementById("bgBarR"),
                  rt=document.getElementById("bgRate");
              if(!tot){ g.textContent="—"; rr.textContent="—"; bg.style.width="0"; br.style.width="0";
                        rt.textContent="Quiet right now — nothing measurable flowing through."; return; }
              var pg=Math.round(e/tot*100);
              g.textContent=pg+"%"; rr.textContent=(100-pg)+"%";
              bg.style.width=pg+"%"; br.style.width=(100-pg)+"%";
              rt.textContent="Right now: "+rate(e)+" encrypted, "+rate(u)+" in the clear.";
            }).catch(function(){});
        }
        function open_(){ p.classList.add("on"); refresh(); clearInterval(timer); timer=setInterval(refresh,2000); }
        function close_(){ p.classList.remove("on"); clearInterval(timer); timer=null; }
        b.addEventListener("click", function(){ p.classList.contains("on") ? close_() : open_(); });
        if(x) x.addEventListener("click", close_);
        document.addEventListener("keydown", function(e){ if(e.key==="Escape") close_(); });
      }
    })();
    </script>
    <script>
    (function(){
      var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
      var FEED=(document.querySelector('meta[name="cd-feed"]')||{}).content||"/budget/feed";
      var c=document.getElementById("bp-bg"); if(!c||!c.getContext) return;
      var ctx=c.getContext("2d"), W=0, H=0, DPR=Math.min(2, window.devicePixelRatio||1);
      var reduce=window.matchMedia&&window.matchMedia("(prefers-reduced-motion:reduce)").matches;
      var HEX="0123456789abcdef";
      // Scroll speed is CONSTANT and never varies — varying it made a lull look like lag.
      // Traffic level drives DENSITY, packet length and brightness instead: a quiet link is
      // sparse and dim, a busy one fills the screen. Same rhythm, obvious difference.
      var lineH=18, buf=[], sub=0, speed=0.6, redP=0.05, tred=0.05, topFade, botFade;
      var intens=0.25, tintens=0.25;              // eased traffic level, 0..1
      var cols=1, colW=320, maxBytes=12;          // columns fill the full width (was one skinny column)
      function hx(n){ var s=""; for(var i=0;i<n;i++){ s+=HEX[(Math.random()*16)|0]; if(i&1) s+=" "; } return s.trim(); }
      // both are hex; colour is the only tell — green = encrypted (TLS), red = unencrypted (DNS/HTTP)
      function cell(){
        if(Math.random() > 0.20 + intens*0.80) return null;      // gaps when it's quiet
        var red = Math.random() < redP;
        var n = 3 + Math.round(Math.random() * (2 + intens * (maxBytes - 3)));
        return { r: red?1:0, tag: red ? (Math.random()<0.7?"DNS":"HTTP") : "TLS",
                 text: hx(n > maxBytes ? maxBytes : n) };
      }
      function newRow(){ var a=[]; for(var i=0;i<cols;i++) a.push(cell()); return a; }
      function resize(){
        W=c.clientWidth; H=c.clientHeight; c.width=W*DPR; c.height=H*DPR; ctx.setTransform(DPR,0,0,DPR,0,0);
        cols = Math.max(1, Math.round(W / 320));
        colW = W / cols;
        maxBytes = Math.max(4, Math.floor((colW - 64) / 10.8));   // 12px monospace, "aa " per byte
        var N=Math.ceil(H/lineH)+2;
        buf = [];                                    // row shape depends on cols, so rebuild
        while(buf.length<N) buf.push(newRow());
        topFade=ctx.createLinearGradient(0,0,0,64); topFade.addColorStop(0,"#070b0e"); topFade.addColorStop(1,"rgba(7,11,14,0)");
        botFade=ctx.createLinearGradient(0,H-64,0,H); botFade.addColorStop(0,"rgba(7,11,14,0)"); botFade.addColorStop(1,"#070b0e");
      }
      function draw(){
        if(!reduce){ redP+=(tred-redP)*0.05; intens+=(tintens-intens)*0.04; sub+=speed;
          while(sub>=lineH){ sub-=lineH; buf.shift(); buf.push(newRow()); } }
        ctx.fillStyle="#070b0e"; ctx.fillRect(0,0,W,H);
        ctx.font="600 12px ui-monospace,Menlo,monospace"; ctx.textBaseline="alphabetic";
        var br = 0.55 + intens*0.85;                                  // busier = brighter
        var tagA=(0.30*br).toFixed(3), redA=(0.52*br).toFixed(3), grnA=(0.42*br).toFixed(3);
        for(var i=0;i<buf.length;i++){ var row=buf[i], y=i*lineH - sub + lineH;
          for(var k=0;k<row.length;k++){ var cl=row[k]; if(!cl) continue;
            var x=k*colW;
            ctx.fillStyle="rgba(120,132,142,"+tagA+")"; ctx.fillText(cl.tag, x+12, y);
            ctx.fillStyle=cl.r? "rgba(240,96,96,"+redA+")":"rgba(78,222,140,"+grnA+")";
            ctx.fillText(cl.text, x+52, y);
          }
        }
        ctx.fillStyle=topFade; ctx.fillRect(0,0,W,64);
        ctx.fillStyle=botFade; ctx.fillRect(0,H-64,W,64);
        if(!reduce) requestAnimationFrame(draw);
      }
      function poll(){
        fetch(FEED+"?t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){return r.json();})
          .then(function(d){ if(d){ var tot=(d.enc||0)+(d.unenc||0);
            tred=tot>0? Math.max(0.015, Math.min(0.7, d.unenc/tot)) : 0.02;  // red share = the REAL exposed ratio
            tintens=Math.max(0.12, Math.min(1, Math.log(1+tot/400)/Math.log(3000)));  // fills the screen when busy
          } }).catch(function(){});
      }
      resize(); draw();
      var rzT; window.addEventListener("resize", function(){ clearTimeout(rzT); rzT=setTimeout(resize, 220); });
      if(!reduce){ poll(); setInterval(poll, 2000); }
    })();
    </script>
    {% endraw %}
"""

BUDGET_PAGE += BG_BLOCK + """</body>
</html>
"""

STATS_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Usage · Countdown</title>
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --faint:#5f6773; --grid:#232732;
            /* series colours come from the server (one per site in SITES) */
            --good:#0ca30c; --warn:#ec835a;
        }
        *{box-sizing:border-box}
        body{
            margin:0;background:var(--bg);color:var(--fg);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;
            padding:28px 16px max(28px,env(safe-area-inset-bottom));
            display:flex;justify-content:center;
        }
        .wrap{width:100%;max-width:560px}
        .kicker{
            display:flex;align-items:center;gap:8px;justify-content:center;
            font-size:11.5px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
            color:var(--muted);margin-bottom:18px;
        }
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--s1)}
        .tiles{display:flex;gap:10px;margin-bottom:12px}
        .tile{
            flex:1;background:var(--card);border:1px solid var(--line);border-radius:14px;
            padding:14px 12px;text-align:center;
        }
        .tile .v{font-size:26px;font-weight:700;letter-spacing:-.5px;line-height:1.1}
        .tile .v.down{color:var(--good)} .tile .v.up{color:var(--warn)}
        .tile .k{font-size:11px;color:var(--faint);margin-top:5px;letter-spacing:.04em;text-transform:uppercase}
        .card{
            background:var(--card);border:1px solid var(--line);border-radius:16px;
            padding:20px 16px 14px;
        }
        .card h2{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 16px;letter-spacing:.02em}
        .chart{display:flex;align-items:flex-end;gap:6px;height:150px;border-bottom:1px solid var(--grid);padding-bottom:0}
        .day{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:2px;height:100%;position:relative;border-radius:4px 4px 0 0}
        .seg{width:100%;min-height:2px}
        .day .seg:first-child{border-radius:4px 4px 0 0}
        .day .tip{
            display:none;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);
            background:#1c2028;border:1px solid var(--line);border-radius:8px;padding:8px 10px;
            font-size:12px;line-height:1.6;white-space:nowrap;z-index:5;color:var(--fg);
            box-shadow:0 8px 24px rgba(0,0,0,.5);pointer-events:none;
        }
        .day:hover .tip{display:block}
        .tip b{font-weight:600}
        .tip .d{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px}
        .xlabels{display:flex;gap:6px;margin-top:6px}
        .xlabels span{flex:1;text-align:center;font-size:10px;color:var(--faint)}
        .legend{display:flex;gap:16px;justify-content:center;margin-top:14px;font-size:12px;color:var(--muted)}
        .legend .d{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:0}
        .live{font-size:12px;color:var(--faint);text-align:center;margin-top:16px}
        .live.stale{color:var(--warn)}
        details{margin-top:14px}
        summary{font-size:12px;color:var(--faint);cursor:pointer;text-align:center;list-style:none}
        table{width:100%;border-collapse:collapse;margin-top:10px;font-size:12.5px}
        th,td{padding:5px 6px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
        th{color:var(--faint);font-weight:600;border-bottom:1px solid var(--line)}
        td:first-child,th:first-child{text-align:left}
        tr.today td{color:var(--fg)}
        .back{display:block;text-align:center;margin-top:20px;font-size:12.5px;color:var(--faint);text-decoration:none}
        /* The way out. The dashboard sits on a different origin from the gate, so this is
           the only link that crosses back — it should not look like a footnote. */
        .back.home{margin-top:22px;padding:11px;border:1px solid var(--line);border-radius:11px;
            color:var(--fg);font-size:13.5px;font-weight:600}
        .cd-n{font-size:22px;font-weight:650;color:var(--fg);font-variant-numeric:tabular-nums}
        .cd-row{color:var(--muted);font-size:13px}
        .cd-sub{color:var(--faint);font-size:12.5px;margin-top:6px}
        .cd-warn{color:var(--warn)}
        .why-row{display:flex;align-items:center;gap:10px;margin-top:9px;font-size:13px}
        .why-lab{width:9.5em;flex:none;color:var(--fg)}
        .why-bar{flex:1;height:9px;background:#0e1116;border-radius:5px;overflow:hidden}
        .why-bar i{display:block;height:100%;background:var(--s1);border-radius:5px}
        .why-n{width:5.5em;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums;font-size:12.5px}
        .why-foot{margin-top:14px;padding-top:12px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
        .why-foot b{color:var(--good);font-size:15px}
        .why-empty{color:var(--faint);font-size:12.5px;margin-top:8px;line-height:1.5}
        .worth-block{margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
        .worth-block h3{font-size:12.5px;font-weight:600;color:var(--muted);margin:0 0 12px;letter-spacing:.02em}
        .why-bar i.w-lo{background:var(--good)}     /* rarely worth it = the useful finding */
        .why-bar i.w-mid{background:var(--s3)}
        .why-bar i.w-hi{background:var(--s1)}
        .worth-foot{margin-top:12px;font-size:12.5px;color:var(--muted);line-height:1.5}
        .worth-foot b{color:var(--fg)}
        .sh-lede{font-size:12.5px;color:var(--muted);line-height:1.55;margin:0 0 14px}
        .sh-tbl{width:100%;border-collapse:collapse;font-size:13px}
        .sh-tbl th{color:var(--faint);font-weight:600;border-bottom:1px solid var(--line);
            padding:5px 6px;text-align:right;font-size:11.5px}
        .sh-tbl td{padding:6px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
        .sh-tbl th:first-child,.sh-tbl td:first-child{text-align:left}
        .sh-tbl tr.sh-tot td{color:var(--fg);font-weight:600;border-top:1px solid var(--line)}
        .sh-note{font-size:12px;color:var(--faint);line-height:1.55;margin-top:12px}
        .sh-note b{color:var(--muted)}
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>Usage · Last 14 days</div>

    <div class="tiles">
        <div class="tile"><div class="v">{{ today_min }}m</div><div class="k">Today</div></div>
        <div class="tile"><div class="v">{{ week_avg }}m</div><div class="k">7-day avg</div></div>
        <div class="tile">
            <div class="v {{ trend_cls }}">{{ trend }}</div><div class="k">vs prior week</div>
        </div>
    </div>

    <div class="card" style="margin-bottom:12px">
        <h2>Why you reach for it &mdash; last {{ why.days }} days</h2>
        {% if why.total %}
        {% for row in why.rows %}
        <div class="why-row">
            <span class="why-lab">{{ row.label }}</span>
            <span class="why-bar"><i style="width:{{ row.bar }}%"></i></span>
            <span class="why-n">{{ row.n }}&times; &middot; {{ row.pct }}%</span>
        </div>
        {% endfor %}
        <div class="why-foot">Naming it was enough to stop you <b>{{ why.passes }}</b> of {{ why.total }} times
            ({{ why.rate }}%).</div>
        {% else %}
        <div class="why-empty">Nothing yet. When the gate asks why you're reaching for a site, the
            answer you pick is recorded here &mdash; along with whether naming it was enough to stop you,
            and whether you judged the time worth it afterwards. It never leaves this box.</div>
        {% endif %}
        {% if worth.total %}
        <div class="worth-block">
            <h3>&hellip;and afterwards, was it worth it?</h3>
            {% for row in worth.rows %}
            <div class="why-row">
                <span class="why-lab">{{ row.label }}</span>
                <span class="why-bar"><i class="{{ 'w-lo' if row.pct < 34 else ('w-hi' if row.pct >= 67 else 'w-mid') }}"
                      style="width:{{ row.pct }}%"></i></span>
                <span class="why-n">{{ row.pct }}% &middot; {{ row.n }}&times;</span>
            </div>
            {% endfor %}
            <div class="worth-foot">Overall you called it worth it <b>{{ worth.rate }}%</b> of the time
                ({{ worth.yes }} of {{ worth.total }}). The pairing is the point: the reason you go in
                predicts the answer better than the site does.</div>
        </div>
        {% endif %}
    </div>

    <div class="card">
        <h2>Minutes on screen per day</h2>
        <div class="chart">
        {% for d in days %}
            <div class="day">
                {% for sr in series|reverse %}{% if d.pcts[sr.key] %}<div class="seg" style="height:{{ d.pcts[sr.key] }}%;background:{{ sr.color }}"></div>{% endif %}{% endfor %}
                <div class="tip"><b>{{ d.label_full }}</b><br>
                    {% for sr in series %}<span class="d" style="background:{{ sr.color }}"></span>{{ sr.label }} {{ d.mins[sr.key] }}m<br>
                    {% endfor %}<b>{{ d.total_min }}m total</b>
                </div>
            </div>
        {% endfor %}
        </div>
        <div class="xlabels">{% for d in days %}<span>{{ d.label }}</span>{% endfor %}</div>
        <div class="legend">
            {% for sr in series %}<span><span class="d" style="background:{{ sr.color }}"></span>{{ sr.label }}</span>{% endfor %}
        </div>
        <details>
            <summary>Table view</summary>
            <table>
                <tr><th>Day</th>{% for sr in series %}<th>{{ sr.label }}</th>{% endfor %}<th>Total</th></tr>
                {% for d in days %}
                <tr {% if loop.last %}class="today"{% endif %}>
                    <td>{{ d.label_full }}</td>{% for sr in series %}<td>{{ d.mins[sr.key] }}m</td>{% endfor %}<td>{{ d.total_min }}m</td>
                </tr>
                {% endfor %}
            </table>
        </details>
    </div>

    <div class="card">
        <h2>Cooldowns — binge clustering</h2>
        {% if cd.week_n %}
        <div class="cd-row"><span class="cd-n">{{ cd.today_n }}</span> today{% if cd.today_times %} · {{ cd.today_times|join(', ') }}{% endif %}</div>
        <div class="cd-sub">
            {% if cd.today_rapid %}<b class="cd-warn">{{ cd.today_rapid }} rapid repeat{{ 's' if cd.today_rapid != 1 else '' }}</b> today — a new cooldown within {{ cd.hours }}h of the last{% else %}No rapid repeats today (within {{ cd.hours }}h){% endif %}
        </div>
        <div class="cd-sub">7 days: {{ cd.week_n }} cooldown{{ 's' if cd.week_n != 1 else '' }}, {{ cd.week_rapid }} rapid repeat{{ 's' if cd.week_rapid != 1 else '' }}. Each rapid repeat draws a longer wall — clustering, not the daily total, is what escalates the cooldown.</div>
        {% else %}
        <div class="cd-sub">No cooldowns logged yet. Once you hit the full-bucket wall a few times, the clustering pattern shows up here.</div>
        {% endif %}
    </div>

    {% if shadow.any %}
    <div class="card">
        <h2>Experiment &mdash; measuring the same time two ways</h2>
        <div class="sh-lede">The countdown is driven by a script injected into these sites.
            That injection is the largest single risk in this project. The proxy is now also
            measuring passively &mdash; watching the requests the sites make on their own, with
            nothing injected. If the two columns track each other, the injection could go.</div>
        <table class="sh-tbl">
            <tr><th>Site</th><th>Injected timer</th><th>Passive</th><th>&times;</th></tr>
            {% for row in shadow.rows %}
            <tr><td>{{ row.label }}</td><td>{{ row.hb }}m</td><td>{{ row.sh }}m</td>
                <td>{% if row.ratio %}{{ row.ratio }}&times;{% else %}&mdash;{% endif %}</td></tr>
            {% endfor %}
            <tr class="sh-tot"><td>All</td><td>{{ shadow.hb }}m</td><td>{{ shadow.sh }}m</td>
                <td>{% if shadow.ratio %}{{ shadow.ratio }}&times;{% else %}&mdash;{% endif %}</td></tr>
        </table>
        <div class="sh-note">
            {%- if not shadow.settled %}<b>Running {{ shadow.running }} &mdash; too early to draw conclusions.</b>
            Give it a few days.{% else %}Comparing the
            {{ shadow.days }} day{{ '' if shadow.days == 1 else 's' }} the meter has been running
            ({{ shadow.running }}).{% endif %}
            Only whole hours where <i>both</i> meters were running are compared.
            Two effects pull opposite ways: background tabs and autoplay make passive read
            <b>high</b>, while <b>reading</b> makes it read <b>low</b> &mdash; a feed rendered in the
            browser sends nothing to the network while you sit and scan it, so passive sees a gap
            and discards it. A steady multiplier either way means passive plus a correction could
            replace the injected timer. A number that jumps around means it can't.</div>
    </div>
    {% endif %}

    <div class="live {{ 'stale' if stale else '' }}">{{ live_line }}</div>
    <a class="back" href="/digest{{ qs }}">This week, in one screen &rarr;</a>
    <a class="back" href="/health{{ qs }}">Raspberry Pi health &rarr;</a>
    <a class="back" href="/devices{{ qs }}">Devices &rarr;</a>
    {% if back_url %}<a class="back home" href="{{ back_url }}">&larr; Back to {{ back_label }}</a>{% endif %}
</div>
</body>
</html>
"""

# Page templates are module constants that never change after import — but
# flask.render_template_string() calls jinja_env.from_string(), which compiles the
# template from scratch on EVERY request. Measured on the Pi: 46 ms for the stats page,
# 47 ms for health, 24 ms for the gate — roughly half of each request spent recompiling
# markup that is byte-identical to last time.
#
# That is not just slow, it is the shape of F12: /health is polled every 4 seconds while
# a dashboard tab is open, each poll held a worker thread, and a starved pool makes
# heartbeats fail silently — which stops time being charged while browsing continues.
# Compile once, render many.
_TEMPLATE_CACHE = {}

def render_page(source, **context):
    """Render one of the page constants, compiling it only once."""
    tpl = _TEMPLATE_CACHE.get(source)
    if tpl is None:
        tpl = _TEMPLATE_CACHE[source] = app.jinja_env.from_string(source)
    # What render_template_string does before rendering. Skipping it would silently drop
    # the context processors — ui_tok, feed_tok and mon — and the pages would render with
    # empty tokens and no dashboard links.
    app.update_template_context(context)
    return tpl.render(context)

def resolve_site(s):
    return s if s in SITES else DEFAULT_SITE

def pool(site):
    # Budget pool key. Sites sharing a "group" draw from one spent/cooldown counter
    # (keyed by the group name); otherwise each site is its own pool, keyed by site.
    return SITES[site].get("group", site)

def pool_sites(p):
    return [s for s in SITES if pool(s) == p]

def pool_max_budget(p):
    # The bucket is "full" at the largest cap among its sites — that's the amount
    # that must be spent to drain it completely (and trigger cooldown), and the
    # amount the refill restores over REFILL_FULL_SECONDS.
    return max(SITES[s]["budget_seconds"] for s in pool_sites(p))

def pool_has_active_session(p):
    for s in pool_sites(p):
        token = r.get(f"active_token:{s}")
        if token and r.get(f"session:{token}") == "active":
            return True
    return False

def in_night(now=None):
    # True during the full bedtime window (local time), handling the midnight wrap.
    h = time.localtime(now).tm_hour
    if NIGHT_START_HOUR <= NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= h < NIGHT_END_HOUR
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR

def _hours_now(now=None):
    lt = time.localtime(now)
    return lt.tm_hour + lt.tm_min / 60 + lt.tm_sec / 3600

def phase(now=None):
    # "night" (full curfew) | "winddown" (ramp in the run-up to curfew) | "day".
    if in_night(now):
        return "night"
    hours_until_night = (NIGHT_START_HOUR - _hours_now(now)) % 24
    return "winddown" if hours_until_night < WINDDOWN_SECONDS / 3600 else "day"

def effective_cap(site, now=None):
    # The site's budget cap right now. Day = its normal cap; night = the small shared
    # buffer; wind-down = a linear ramp from the day cap down to that buffer.
    ph = phase(now)
    if ph == "night":
        return NIGHT_BUDGET_SECONDS
    if ph == "winddown":
        hours_until_night = (NIGHT_START_HOUR - _hours_now(now)) % 24
        frac = max(0.0, min(1.0, hours_until_night / (WINDDOWN_SECONDS / 3600)))
        day = SITES[site]["budget_seconds"]
        return NIGHT_BUDGET_SECONDS + (day - NIGHT_BUDGET_SECONDS) * frac
    return SITES[site]["budget_seconds"]

def secs_until_hour(target_hour, now=None):
    # Seconds from now until the next occurrence of target_hour:00 local time.
    lt = time.localtime(now)
    cur = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    d = target_hour * 3600 - cur
    return d + 86400 if d <= 0 else d

def clock(secs):
    # m:ss (or h:mm:ss) for a headline time display.
    secs = int(secs)
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def apply_refill(p):
    # Slow passive refill of spent while the pool is idle, but only AFTER a grace
    # window (REGEN_DELAY) of no use — so briefly waiting can't top you back up for
    # another sip; genuinely stepping away for a while still recovers time. Skipped
    # while the pool is actively in use (real viewing time isn't discounted), during
    # cooldown (the hard wall must not leak away), and at night (the night buffer is a
    # separate, deliberately non-regenerating counter). Wind-down DOES refill: effective_cap()
    # is ramping down, so get_remaining_budget bounds you by that shrinking ceiling — you
    # regen back up toward the time-proportional cap (e.g. ~7.5 min at 10:30), never the full
    # day cap. The refill rate stays the normal one; the ramp alone does the winding-down.
    if pool_has_active_session(p) or r.get(f"cooldown:{p}") or phase() == "night":
        return
    spent = float(r.get(f"spent:{p}") or 0)
    if spent <= 0:
        return
    last = r.get(f"last_heartbeat:{p}")
    now = time.time()
    if not last:
        r.set(f"last_heartbeat:{p}", now)
        return
    # Only idle time PAST the grace window earns refill. `refilled_through` is the
    # timestamp up to which we've already credited; it advances continuously once the
    # grace has elapsed (so refill is smooth, not bursty), and is naturally superseded
    # when fresh use bumps last_heartbeat forward and resets the grace. last_heartbeat
    # itself is left untouched here — it stays the charge baseline for /heartbeat.
    grace_end = float(last) + REGEN_DELAY
    cursor = r.get(f"refilled_through:{p}")
    start = max(grace_end, float(cursor)) if cursor else grace_end
    if now <= start:
        return
    rate = pool_max_budget(p) / REFILL_FULL_SECONDS
    r.set(f"spent:{p}", max(0, spent - (now - start) * rate))
    r.set(f"refilled_through:{p}", now)

def get_spent(site):
    p = pool(site)
    apply_refill(p)
    return float(r.get(f"spent:{p}") or 0)

def night_spent(p):
    return float(r.get(f"night_spent:{p}") or 0)

def get_remaining_budget(site):
    p = pool(site)
    if phase() == "night":
        # Night has its OWN small buffer on a separate counter, independent of the
        # day's spend — a used-up day must not eat your night allowance (nor the
        # reverse). Non-regenerating; cleared at the 7am reset so each night is fresh.
        return max(0, NIGHT_BUDGET_SECONDS - night_spent(p))
    return max(0, effective_cap(site) - get_spent(site))

def get_cooldown_remaining(site):
    p = pool(site)
    cooldown_start = r.get(f"cooldown:{p}")
    if not cooldown_start:
        return 0
    duration = float(r.get(f"cooldown_secs:{p}") or COOLDOWN_SECONDS)  # escalated per-cooldown
    elapsed = time.time() - float(cooldown_start)
    remaining = duration - elapsed
    if remaining <= 0:
        # Don't restore budget outside daytime: a daytime cooldown expiring during
        # wind-down or night must NOT hand out a fresh buffer. Leave spent/cooldown
        # as-is (the tightened cap keeps you gated); the 7am reset clears everything.
        if phase() != "day":
            return 0
        # Cooldown is over — clear it AND restore the budget so the
        # next visit can enter again. Without resetting spent:{pool} the
        # /budget page would immediately re-trigger a fresh cooldown.
        r.delete(f"cooldown:{p}")
        r.delete(f"cooldown_secs:{p}")
        r.delete(f"spent:{p}")
        r.delete(f"refilled_through:{p}")
        return 0
    return remaining

def recent_cooldown_count(now):
    """How many pool cooldowns already started within the trailing RAPID_REPEAT_WINDOW
    (before `now`). This is the escalation index: 0 = a lone/spread-out cooldown (base
    duration), higher = a cluster of rapid re-binges (progressively longer wall). Scans
    today and yesterday since the window can straddle midnight.
    """
    cutoff = now - RAPID_REPEAT_WINDOW
    count = 0
    for i in (1, 0):
        key_day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for raw in r.lrange(f"cooldown_events:{key_day}", 0, -1):
            try:
                ts = float(raw.split()[0])
            except (ValueError, IndexError):
                continue
            if cutoff <= ts < now:
                count += 1
    return count

def start_cooldown(p, site, now=None):
    """Begin the pool's hard cooldown and log a timestamped event — once.

    Idempotent: if a cooldown is already running, do nothing (don't reset the
    timer, don't double-log). Duration escalates when cooldowns *cluster*: the
    event log (each entry "<epoch> <site>", per-day, self-pruning after ~100 days)
    is scanned so a rapid re-binge draws a longer wall from COOLDOWN_LADDER, while a
    spread-out day stays at the 1-hour base. The chosen duration is stored alongside
    the start so get_cooldown_remaining counts down the right amount.
    """
    if r.get(f"cooldown:{p}"):
        return
    now = now if now is not None else time.time()
    idx = min(recent_cooldown_count(now), len(COOLDOWN_LADDER) - 1)
    duration = COOLDOWN_LADDER[idx]
    r.set(f"cooldown:{p}", now)
    r.set(f"cooldown_secs:{p}", duration)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    r.rpush(f"cooldown_events:{day}", f"{now:.0f} {site}")
    r.expire(f"cooldown_events:{day}", HISTORY_TTL)

# The reflection prompt asks *why* you're reaching for the feed. That answer — and
# whether naming it actually stopped you — is the most interesting thing this system can
# know, and it used to live only in the browser and vanish. Recorded per-day, same shape
# as the other logs: "<epoch> <trigger> <action>", self-pruning after ~100 days.
REFLECT_TRIGGERS = {
    "tired":    "Tired",
    "bored":    "Bored",
    "stressed": "Stressed",
    "avoiding": "Avoiding something",
    "habit":    "Just habit",
    "need":     "Actually needed it",
}

# Anything shown identically every time becomes wallpaper — that's how Screen Time's
# "Ignore for today" ends up being tapped without reading. Two defences:
#   · it isn't shown every time (never on your first session of the day, and only on a
#     stable-but-unpredictable ~70% of the rest), so it can't become part of the routine;
#   · the wording rotates and the chips are shuffled, so there's no fixed motor pattern.
# Deliberately seeded rather than live-random, so reloading the page can't reroll it away.
REFLECT_QUESTIONS = [
    "Part of you doesn't want to scroll. What's pulling you in right now?",
    "Before you go in — what are you actually reaching for?",
    "Quick check: what's driving this one?",
    "What's underneath the urge right now?",
    "Honestly — why this, why now?",
    "Something sent you here. What was it?",
]

# ---------------------------------------------------------------------------
# What the gate says.
#
# The reflection prompt already rotates its wording and shuffles its chips, for a reason
# stated plainly in the comment above it: anything shown identically every time becomes
# wallpaper. The gate's own sentences did not rotate — and "Foreground time only, the
# clock ticks while you're looking" is the line you see more than any other in this
# product. It was the most-read, least-varied text on the screen.
#
# Same seeding rule as the prompt: keyed to the day and the session number, never
# live-random, so refreshing cannot reroll it. Stable while you are staring at it,
# different next time.
# ---------------------------------------------------------------------------
GATE_LINES = {
    "day": [
        "Foreground time only — the clock ticks while you're looking. Make it count.",
        "The timer runs while you're actually looking. Background tabs are free.",
        "This is the whole budget. There isn't a second one behind it.",
        "Time starts when you tap and stops when you look away. Spend it deliberately.",
        "Nothing refills faster because you'd like it to.",
        "You get what's on the clock. That's the arrangement you made.",
    ],
    "spent": [
        "You've used your {label} share of the bucket.{steer} It trickles back if you step away.",
        "That's your {label} allowance.{steer} Walk away for a while and some comes back.",
        "{label} is done for the moment.{steer} The bucket refills slowly, not on demand.",
    ],
    "cooldown": [
        "That was your session. It reopens when the timer hits zero.",
        "Nothing to do here but wait. That is the entire point.",
        "The timer doesn't know you're bored, and wouldn't care.",
        "Session's over. The clock is the only thing that reopens this.",
    ],
    "cooldown_escalated": [
        "Back-to-back sessions get a longer break — it reopens when the timer hits zero.",
        "You came straight back, so the wall got taller. It comes down on its own.",
        "Clustered sessions earn a longer wait. Spread out and it stays short.",
    ],
    "soft": [
        "You've hit your {label} cap a few times in a row — a short pause to break the loop.",
        "That's three quick rounds on {label}. Brief pause, then it reopens.",
        "Same site, same loop. A short breather, and it's yours again.",
    ],
    "night": [
        "A small buffer, then closed till {end} AM. No refill overnight — spend it wisely.",
        "One short buffer left before lights out. It doesn't come back until {end} AM.",
        "Night mode: a few minutes, then closed till {end} AM. Nothing refills overnight.",
    ],
    "night_closed": [
        "{label} is closed for the night. It reopens at {end} AM.",
        "It's late. This will still be here at {end} AM, and you'll be better rested.",
        "Shut till {end} AM. Nothing here improves after midnight.",
    ],
    "winddown": [
        "Your time is shrinking toward bedtime, and there's no refill now.",
        "The cap is tapering toward lights-out. What's left is what's left.",
        "Winding down — the allowance shrinks from here, and nothing tops it up.",
    ],
    "winddown_spent": [
        "Easing toward bedtime — back briefly at night mode, then closed.",
        "Done for now. A short night buffer later, then closed till morning.",
        "That's the wind-down spent. Time for something that isn't a feed.",
    ],
}

# The screen you get when the prompt talks you out of it. This is the one moment the tool
# actually worked, and it said the same three sentences every time — the most repeated
# text at the highest-value moment in the product.
PASS_LINES = [
    ("Good call", "Put it down.",
     "Part of you already knew. Close this tab and go do the thing \u2014 future-you says thanks."),
    ("Nice", "That's the one.",
     "You caught it before it caught you. That gets easier every time you do it."),
    ("Good call", "Nothing in there.",
     "You checked, and the answer was no. Go and do the thing you actually meant to."),
    ("Well played", "Saved yourself twenty minutes.",
     "That is roughly what this would have cost. Spend it on something you'd remember."),
    ("Good", "Put the phone down.",
     "The urge passes on its own in about a minute. You just proved it."),
    ("Nice one", "You didn't need it.",
     "Most of the time you don't. Knowing that is the whole trick."),
]

def pass_line(now=None):
    """Rotates like everything else, seeded so a refresh can't reroll it."""
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    entries = int(_try(lambda: r.get(f"entries:{day}"), 0) or 0)
    return random.Random(f"pass:{day}:{entries}").choice(PASS_LINES)

# Used when a data moment is picked but there's nothing interesting to say. Dry, not
# preachy — the tone the reflection chips already set.
BLUNT_LINES = [
    "Nothing new has happened in the last four minutes.",
    "The feed does not miss you.",
    "Whatever you're avoiding will still be there afterwards.",
    "Nobody has ever finished the internet.",
    "You already know roughly how this session goes.",
    "Refreshing does not create new time.",
    "This is the deciding part. The rest is drifting.",
    "You opened this on purpose. Allegedly.",
    "The interesting thing is not in there.",
    "Somewhere in this is a thing you actually wanted. Get it and leave.",
]

# How often the line is a fact about you rather than the standard copy. Rare on purpose:
# make it frequent and it becomes the new wallpaper.
MOMENT_ODDS = 1 / 6

# ...and only on states whose sentence is purely atmospheric. "spent" carries the steer
# to a site that still has time, and "cooldown_escalated" explains why the wall got
# taller — replacing those with a wisecrack would drop information the screen has nowhere
# else. On these two the number is already on screen above the text, so the sentence is
# free to be something else.
MOMENT_STATES = {"day", "cooldown"}

def _moment_visits(ctx):
    n = ctx.get("entries", 0)
    if n >= 2:
        return f"Visit number {n + 1} today."
    return None

def _moment_late_cooldowns(ctx, now):
    hours = []
    for i in range(7):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for raw in _try(lambda d=day: r.lrange(f"cooldown_events:{d}", 0, -1), []) or []:
            try:
                hours.append(time.localtime(float(raw.split()[0])).tm_hour)
            except (ValueError, IndexError):
                continue
    if len(hours) >= 3 and sum(1 for h in hours if h >= 20) >= len(hours) - 1:
        return f"{len(hours)} cooldowns this week, nearly all after 8pm."
    if len(hours) >= 3:
        return f"{len(hours)} times you've hit the wall this week."
    return None

def _moment_worth(ctx, now):
    w = worth_summary(30, now)
    rows = [x for x in w["rows"] if x["n"] >= 3 and x["key"] in REFLECT_TRIGGERS]
    if not rows:
        return None
    worst = min(rows, key=lambda x: x["pct"])
    if worst["pct"] <= 34:
        return (f"When you've come here {worst['label'].lower()}, you said afterwards it "
                f"was worth it {worst['pct']}% of the time.")
    return None

def _moment_over_usual(ctx, now):
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    sites = list(SITES)
    vals = _mget_floats(_day_keys("usage", 8, now, sites))   # 7 prior days + today
    tot = sum(vals[7 * len(sites):])
    avg = sum(vals[:7 * len(sites)]) / 7
    if avg >= 300 and tot > avg:
        return f"You're already past a usual whole day ({int(tot // 60)}m vs {int(avg // 60)}m)."
    return None

def _moment_quiet(ctx, now):
    for i in range(0, 14):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        if _try(lambda d=day: r.llen(f"cooldown_events:{d}"), 0):
            return f"{i} days since you last hit the wall." if i >= 3 else None
    return None

# --- 4. blunt about the hour ------------------------------------------------
# Not random — true. At 2am "It's 2am" lands harder than anything a bank could produce,
# and the night states are exactly where it applies, because night mode runs 23:00-07:00.
def _time_line(state, hour, seed):
    if state in ("night", "night_closed") and 0 <= hour < 5:
        return seed.choice([
            f"It's {hour or 12}am. Nothing worth reading was posted in the last hour.",
            f"It's {hour or 12}am. Go to bed — this is the same feed it was yesterday.",
            f"{hour or 12}am. Whatever this is, it isn't rest.",
        ])
    if state == "winddown" and hour >= 22:
        return seed.choice([
            "It's nearly bedtime and the allowance knows it.",
            "Late enough that tomorrow-you has an opinion about this.",
        ])
    return None

def gate_line(state, now=None, **ctx):
    """The sentence under the headline. Rotates; occasionally says something true."""
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    # A caller-supplied session number must drive the seed too, not just the moments —
    # otherwise the override is half-honoured and the whole function is untestable.
    stored = int(_try(lambda: r.get(f"entries:{day}"), 0) or 0)
    entries = int(ctx.get("entries", stored))
    ctx["entries"] = entries
    seed = random.Random(f"gate:{day}:{entries}:{state}")
    # The hour, when the hour is the point.
    hour = time.localtime(now).tm_hour
    timed = _time_line(state, hour, seed)
    if timed and seed.random() < 0.5:
        return timed
    # A seasonal theme gets a say on the most-seen screen.
    if state == "day":
        _, th = active_theme(now)
        if th and th.get("lines") and seed.random() < 0.34:
            return seed.choice(th["lines"])
    if state in MOMENT_STATES and seed.random() < MOMENT_ODDS:
        moments = [_moment_visits, _moment_late_cooldowns, _moment_worth,
                   _moment_over_usual, _moment_quiet]
        seed.shuffle(moments)
        for fn in moments:
            line = _try(lambda f=fn: f(ctx) if f is _moment_visits else f(ctx, now))
            if line:
                return line
        return seed.choice(BLUNT_LINES)
    return seed.choice(GATE_LINES[state]).format(**ctx)

def reflect_decision(now=None):
    """(show_it, which_question). Skips your first entry of the day, then appears
    unpredictably — the point is that it can't be anticipated and tapped through."""
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    entries = int(r.get(f"entries:{day}") or 0)
    seed = random.Random(f"{day}:{entries}")
    question = REFLECT_QUESTIONS[seed.randrange(len(REFLECT_QUESTIONS))]
    return (entries >= 1 and seed.random() < 0.7), question

def log_reflection(trigger, action, now=None):
    if trigger not in REFLECT_TRIGGERS or action not in ("pass", "enter"):
        return
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    r.rpush(f"reflect:{day}", f"{now:.0f} {trigger} {action}")
    r.expire(f"reflect:{day}", HISTORY_TTL)

def reflection_summary(days=30, now=None):
    """Per-trigger counts and how often naming it was enough to stop you."""
    now = now if now is not None else time.time()
    rows, passes, total = {}, 0, 0
    for entries in _mlrange(_day_keys("reflect", days, now)):
        for raw in entries:
            parts = raw.split()
            if len(parts) < 3 or parts[1] not in REFLECT_TRIGGERS:
                continue
            t, a = parts[1], parts[2]
            d = rows.setdefault(t, {"key": t, "label": REFLECT_TRIGGERS[t], "n": 0, "passed": 0})
            d["n"] += 1
            total += 1
            if a == "pass":
                d["passed"] += 1
                passes += 1
    out = sorted(rows.values(), key=lambda d: -d["n"])
    top = out[0]["n"] if out else 0
    for d in out:
        d["pct"] = round(100 * d["n"] / total) if total else 0
        d["bar"] = round(100 * d["n"] / top) if top else 0
    return {"rows": out, "total": total, "passes": passes,
            "rate": round(100 * passes / total) if total else 0, "days": days}

# The reflection prompt asks why you are reaching for the feed. It never asks how it went.
# That is the more interesting half: "when I was tired it was never worth it, when I
# actually needed it it usually was" is the sentence the whole feature is reaching for,
# and a pre-session trigger alone cannot produce it. Queued when a session ends, asked
# once on the next gate screen — you are already stopped, so it costs nothing.
WORTH_TTL = 2 * 3600

def queue_worth_prompt(site, now=None):
    now = now if now is not None else time.time()
    trigger = r.get(f"session_trigger:{pool(site)}") or "-"
    r.setex("pending_worth", WORTH_TTL, f"{now:.0f} {site} {trigger}")

def log_worth(verdict, now=None):
    """Record the verdict against the trigger that opened the session. Returns False if
    there was nothing pending, so a double-tap or a stale form can't invent data."""
    if verdict not in ("yes", "no"):
        return False
    pending = r.get("pending_worth")
    if not pending:
        return False
    r.delete("pending_worth")
    parts = pending.split()
    trigger = parts[2] if len(parts) > 2 else "-"
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    r.rpush(f"worth:{day}", f"{now:.0f} {trigger} {verdict}")
    r.expire(f"worth:{day}", HISTORY_TTL)
    return True

def worth_summary(days=30, now=None):
    """Per-trigger: how often the time was judged worth it afterwards."""
    now = now if now is not None else time.time()
    rows, yes, total = {}, 0, 0
    for entries in _mlrange(_day_keys("worth", days, now)):
        for raw in entries:
            parts = raw.split()
            if len(parts) < 3:
                continue
            t, v = parts[1], parts[2]
            d = rows.setdefault(t, {"key": t, "label": REFLECT_TRIGGERS.get(t, "Not asked"),
                                    "n": 0, "yes": 0})
            d["n"] += 1
            total += 1
            if v == "yes":
                d["yes"] += 1
                yes += 1
    out = sorted(rows.values(), key=lambda d: -d["n"])
    for d in out:
        d["pct"] = round(100 * d["yes"] / d["n"]) if d["n"] else 0
    return {"rows": out, "total": total, "yes": yes,
            "rate": round(100 * yes / total) if total else 0, "days": days}

def log_soft_pause(site, now=None):
    """Log a per-site SOFT pause: a site hit its own cap while the shared bucket still had
    room, so the session ended with NO hard cooldown. Feeds the cluster brake below
    (recent_soft_pause_count reads this log). Each entry "<epoch> <site>"; per-day key,
    self-prunes after ~100 days."""
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    r.rpush(f"soft_pauses:{day}", f"{now:.0f} {site}")
    r.expire(f"soft_pauses:{day}", HISTORY_TTL)

def recent_soft_pause_count(site, now):
    """How many soft pauses for `site` fell inside the trailing CLUSTER_WINDOW (through now).
    Scans today + yesterday since the window can straddle midnight."""
    cutoff = now - CLUSTER_WINDOW
    count = 0
    for i in (1, 0):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for raw in r.lrange(f"soft_pauses:{day}", 0, -1):
            parts = raw.split()
            try:
                ts = float(parts[0])
            except (ValueError, IndexError):
                continue
            # No upper bound: entries are stored rounded (f"{now:.0f}"), so the just-logged
            # pause can round to now+1 — an `<= now` check would drop it ~half the time.
            if len(parts) > 1 and parts[1] == site and ts >= cutoff:
                count += 1
    return count

def maybe_cluster_cooldown(site, now):
    """Called right after logging a soft pause. If it's the CLUSTER_THRESHOLD-th cap-hit for
    this site within the rolling window, set a short, site-specific cooldown to break the
    loop (auto-expires via TTL). Returns True if it fired."""
    if recent_soft_pause_count(site, now) >= CLUSTER_THRESHOLD:
        r.setex(f"soft_cd:{site}", CLUSTER_COOLDOWN_SECONDS, f"{now:.0f}")
        return True
    return False

def get_soft_cd_remaining(site):
    """Seconds left on a site's cluster cooldown (0 if none). TTL-backed, so it just melts away."""
    ttl = r.ttl(f"soft_cd:{site}")
    return ttl if ttl and ttl > 0 else 0

def _safe_next(site, nxt):
    """Validate a return-URL: http(s), no embedded credentials, host on the SAME
    gated site (home's registrable domain or a subdomain). Returns the URL if safe,
    else "".

    Hardened against parser-differential open redirects — cases where urlparse and
    the browser disagree on the host, e.g. "https://evil.com\\@reddit.com/" parses
    as host=reddit.com in Python (so a naive check allows it) while the browser
    reads "\\" as "/" and navigates to evil.com. We reject the characters that drive
    those differentials (backslashes, whitespace, control chars) and any userinfo
    "@" in the authority, then require a same-site host.
    """
    if not nxt:
        return ""
    if any(c in nxt for c in "\\ \t\r\n") or any(ord(c) < 0x20 or ord(c) == 0x7f for c in nxt):
        return ""
    try:
        u = urlparse(nxt)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https"):
        return ""
    if u.username is not None or u.password is not None or "@" in (u.netloc or ""):
        return ""
    host = u.hostname
    if not host:
        return ""
    # Which domains count as "same site" for the return URL. News is a category, so
    # any host in NEWS_DOMAINS (or a subdomain) is valid — that's how Enter returns you
    # to the specific article you opened. Other sites match their home's domain.
    if site == "news":
        domains = NEWS_DOMAINS
    else:
        home_host = urlparse(SITES[site]["home"]).hostname or ""
        domains = [".".join(home_host.split(".")[-2:])] if home_host else []   # www.reddit.com -> reddit.com
    return nxt if any(host == d or host.endswith("." + d) for d in domains) else ""

def render_gate(site, label, *, overline, message, title="", mood="wait",
                can_enter=False, button_text="", headline="",
                countdown=0, show_study=False, study_primary=False, refresh=0, next_url="",
                show_reflect=False, reflect_q="", ask_worth=False):
    # One template, many states. `overline` is the uppercase kicker; `countdown` (secs)
    # renders a live ticking timer that reloads at zero; `headline` renders a big static
    # time; `mood` picks the accent colour (go/wait/sleep). `next_url`, when set, makes
    # the Enter button return to the original link instead of the site home.
    # `study_primary` promotes the Study button to the main CTA — used on the cooldown
    # screens, turning the enforced break into a one-tap redirect to the course.
    return render_page(BUDGET_PAGE, pass_line=_try(pass_line, PASS_LINES[0]),
        site=site, label=label, overline=overline, title=title, message=message, mood=mood,
        can_enter=can_enter, button_text=button_text, headline=headline,
        countdown=int(countdown), show_study=show_study, study_primary=study_primary,
        refresh=refresh, next_url=next_url,
        show_reflect=show_reflect, reflect_q=reflect_q, ask_worth=ask_worth)

@app.route('/budget')
def budget_page():
    site = resolve_site(request.args.get("site"))
    label = SITES[site]["label"]
    # Only on the screens you land on *because* a session ended — never on the way in.
    ask_worth = bool(_try(lambda: r.get("pending_worth")))

    p = pool(site)
    study_ok = (site == "youtube" and bool(STUDY_PLAYLISTS))
    ph = phase()
    # The addon passes the original URL the user was heading to, so Enter can return
    # there instead of the site home. Validated in /enter (must be on the same site).
    nxt = _safe_next(site, request.args.get("next", ""))

    # Night / wind-down own the gate (before any leftover daytime cooldown). Refill is
    # off in both; night is a small fixed buffer, wind-down a shrinking one. No cooldown
    # machinery here (Tailscale-off still escapes), and study mode stays available.
    if ph in ("night", "winddown"):
        remaining = get_remaining_budget(site)   # night-aware (own buffer) vs winddown ramp
        if ph == "night":
            if remaining <= 0:
                return render_gate(site, label, overline=f"{label} · Bedtime", mood="sleep",
                    countdown=secs_until_hour(NIGHT_END_HOUR), show_study=study_ok,
                    title="Get some sleep",
                    message=gate_line("night_closed", label=label, end=NIGHT_END_HOUR))
            nshow, nq = reflect_decision()
            return render_gate(site, label, overline=f"{label} · Night mode", mood="sleep",
                headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
                show_reflect=nshow, reflect_q=nq,
                message=gate_line("night", label=label, end=NIGHT_END_HOUR))
        # wind-down
        if remaining <= 0:
            return render_gate(site, label, overline=f"{label} · Winding down", mood="wait",
                countdown=secs_until_hour(NIGHT_START_HOUR), show_study=study_ok,
                title="Paused for now",
                message=gate_line("winddown_spent", label=label))
        wshow, wq = reflect_decision()
        return render_gate(site, label, overline=f"{label} · Winding down", mood="wait",
            headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
            show_reflect=wshow, reflect_q=wq,
            message=gate_line("winddown", label=label))

    # Daytime.
    cooldown_remaining = get_cooldown_remaining(site)
    if cooldown_remaining > 0:
        escalated = float(r.get(f"cooldown_secs:{p}") or COOLDOWN_SECONDS) > COOLDOWN_SECONDS
        msg = gate_line("cooldown_escalated" if escalated else "cooldown", label=label)
        if study_ok:
            msg += " Put the break to work — the course is one tap away."
        return render_gate(site, label, overline=f"{label} · Cooldown", mood="wait",
            countdown=cooldown_remaining, show_study=study_ok, study_primary=study_ok,
            title="Take a break", message=msg, ask_worth=ask_worth)

    # One site looping (repeated cap-hits in a short window) -> a short site-specific breather.
    soft_cd = get_soft_cd_remaining(site)
    if soft_cd > 0:
        return render_gate(site, label, overline=f"{label} · Short break", mood="wait",
            countdown=soft_cd, show_study=study_ok, study_primary=study_ok,
            title="Take a breather",
            message=gate_line("soft", label=label))

    spent = get_spent(site)
    remaining = max(0, SITES[site]["budget_seconds"] - spent)

    # Whole bucket drained -> start the hard cooldown.
    if spent >= pool_max_budget(p):
        start_cooldown(p, site)
        msg = gate_line("cooldown", label=label)
        if study_ok:
            msg += " Or turn the break into progress: the course is one tap away."
        return render_gate(site, label, overline=f"{label} · Time's up", mood="wait",
            countdown=get_cooldown_remaining(site), show_study=study_ok, study_primary=study_ok,
            title="Whole bucket spent", message=msg, ask_worth=ask_worth)

    # This site's slice used up, but the bucket still has time for a bigger-cap site.
    if remaining <= 0:
        others = [SITES[s]["label"] for s in pool_sites(p)
                  if s != site and get_remaining_budget(s) > 0]
        steer = f" Still time on {' & '.join(others)}." if others else ""
        return render_gate(site, label, overline=f"{label} · Spent", mood="wait",
            title=f"{label} is done for now", button_text=f"{label} used up",
            show_study=study_ok, refresh=15, ask_worth=ask_worth,
            message=gate_line("spent", label=label, steer=steer))

    # Enter.
    show_reflect, reflect_q = reflect_decision()
    return render_gate(site, label, overline=f"{label} · Time left", mood="go",
        headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
        show_reflect=show_reflect, reflect_q=reflect_q,
        message=gate_line("day", label=label))

@app.route('/reflect', methods=['POST'])
def reflect():
    # Fired when the reflection prompt talks you out of it. (The other outcome is
    # recorded by /enter, which the "continue anyway" button posts to.)
    log_reflection(request.form.get("trigger", ""), "pass")
    return jsonify({"status": "ok"})

@app.route('/worth', methods=['POST'])
def worth():
    site = resolve_site(request.args.get("site"))
    _try(lambda: log_worth(request.form.get("v", "")))
    return redirect(f'/budget?site={site}')

@app.route('/enter', methods=['POST'])
def enter():
    site = resolve_site(request.args.get("site"))
    remaining = get_remaining_budget(site)
    cooldown = get_cooldown_remaining(site)

    if remaining <= 0 or cooldown > 0 or get_soft_cd_remaining(site) > 0:
        return redirect(f'/budget?site={site}')

    trigger = request.form.get("trigger", "")
    log_reflection(trigger, "enter")
    # Carried through the session so the "was that worth it?" answer can be attributed
    # to the reason you gave for starting. Without the join, both halves are just tallies.
    r.setex(f"session_trigger:{pool(site)}", 6 * 3600, trigger or "-")
    day = time.strftime("%Y-%m-%d")
    r.incr(f"entries:{day}")          # drives "skip the prompt on your first session"
    r.expire(f"entries:{day}", 7 * 86400)

    token = str(uuid.uuid4())
    r.setex(f"session:{token}", SESSION_IDLE_TTL, "active")
    r.set(f"active_token:{site}", token)
    r.set(f"last_heartbeat:{pool(site)}", time.time())

    # Return to the original link the user clicked (validated same-site), else home.
    return redirect(_safe_next(site, request.args.get("next", "")) or SITES[site]["home"])

@app.route('/study', methods=['POST'])
def study():
    site = resolve_site(request.args.get("site"))
    # Study mode is YouTube-only and deliberately bypasses budget AND cooldown —
    # the lock-to-playlist enforcement (in the proxy + injected JS) is what keeps
    # it honest, so there's no time accounting here.
    if site != "youtube" or not STUDY_PLAYLISTS:
        return redirect(f'/budget?site={site}')
    # Study mode (locked to the course playlist) stays available at all hours — including
    # wind-down and overnight — since it's the productive escape, not a doomscroll path.

    token = str(uuid.uuid4())
    r.setex(f"session:{token}", SESSION_IDLE_TTL, "study")
    r.set(f"active_token:{site}", token)
    r.set("last_study_beat", time.time())   # baseline so the first heartbeat gap counts

    return redirect(f"https://www.youtube.com/playlist?list={STUDY_PLAYLISTS[0]}")

@app.route('/exit', methods=['POST', 'GET'])
def exit_session():
    # Ends the current session (study or budgeted) and returns to the gate.
    # Used by the in-page "Exit study mode" button; clearing the session is what
    # lets the next navigation fall through to the budget gate.
    site = resolve_site(request.args.get("site"))
    token = r.get(f"active_token:{site}")
    if token:
        r.delete(f"session:{token}")
    r.delete(f"active_token:{site}")
    return redirect(SITES[site]["home"])

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    site = resolve_site(request.args.get("site"))
    token = r.get(f"active_token:{site}")
    if not token:
        return jsonify({"status": "blocked"}), 403

    mode = r.get(f"session:{token}")
    if not mode:
        return jsonify({"status": "blocked"}), 403  # session idled out

    # Refresh the idle TTL, preserving the session mode ("active" or "study").
    r.setex(f"session:{token}", SESSION_IDLE_TTL, mode)

    # Study mode is free and always available: keep the session alive, never charge/cool.
    # We still LOG the foreground seconds (separately from budgeted usage) so "am I
    # actually studying?" is measurable — same visibility-gated, gap-capped accounting
    # as usage, but it never touches spent/cooldown.
    if mode == "study":
        now = time.time()
        last = r.get("last_study_beat")
        if last:
            gap = now - float(last)
            if gap <= HEARTBEAT_MAX_GAP:
                day = time.strftime("%Y-%m-%d")
                r.incrbyfloat(f"study_usage:{day}", gap)
                r.expire(f"study_usage:{day}", HISTORY_TTL)
                r.set("last_study_charge", now)
        r.set("last_study_beat", now)
        return jsonify({"status": "study"})

    p = pool(site)
    last = r.get(f"last_heartbeat:{p}")
    now = time.time()
    if last:
        gap = now - float(last)
        if gap <= HEARTBEAT_MAX_GAP:
            ph = phase()
            # Usage history: per-day, per-site seconds actually charged. Never cleared
            # by resets/cooldowns (it's history, not budget state); self-prunes after
            # ~100 days. last_charge doubles as a liveness marker for /stats — if it
            # goes stale for days, the heartbeat pipeline probably broke (fails open).
            day = time.strftime("%Y-%m-%d")
            r.incrbyfloat(f"usage:{day}:{site}", gap)
            r.expire(f"usage:{day}:{site}", HISTORY_TTL)
            # Same figure bucketed by hour, purely so the shadow-meter experiment can
            # compare like with like. Daily totals cannot be aligned with a meter that
            # started at 15:49 — the first comparison divided 15 hours of heartbeat by
            # three hours of passive and produced 0.22x, which meant nothing at all.
            hour_key = f"usage_hour:{day}:{time.strftime('%H')}:{site}"
            r.incrbyfloat(hour_key, gap)
            r.expire(hour_key, 14 * 86400)
            r.set("last_charge", now)
            if ph == "night":
                # Charge the independent night buffer, not the day bucket. No cooldown
                # at night; just end the session when the small buffer is spent.
                if r.incrbyfloat(f"night_spent:{p}", gap) >= NIGHT_BUDGET_SECONDS:
                    r.delete(f"active_token:{site}")
                    r.delete(f"session:{token}")
                    queue_worth_prompt(site, now)
                    return jsonify({"status": "blocked", "remaining": 0}), 403
            else:
                spent = r.incrbyfloat(f"spent:{p}", gap)
                if ph == "day":
                    # Whole bucket drained -> hard 1-hour cooldown for the pool.
                    if spent >= pool_max_budget(p):
                        r.delete(f"active_token:{site}")
                        r.delete(f"session:{token}")
                        queue_worth_prompt(site, now)
                        start_cooldown(p, site, now)
                        return jsonify({"status": "blocked", "remaining": 0}), 403
                    # This site's slice is used up but the bucket isn't -> end just this
                    # site's session, no cooldown; a bigger-cap site can still be used.
                    if spent >= SITES[site]["budget_seconds"]:
                        r.delete(f"active_token:{site}")
                        r.delete(f"session:{token}")
                        queue_worth_prompt(site, now)
                        log_soft_pause(site, now)
                        maybe_cluster_cooldown(site, now)   # short brake if this site is looping
                        return jsonify({"status": "blocked", "remaining": 0}), 403
                # Wind-down: shrinking cap on the day bucket, no cooldown.
                elif spent >= effective_cap(site):
                    r.delete(f"active_token:{site}")
                    r.delete(f"session:{token}")
                    queue_worth_prompt(site, now)
                    return jsonify({"status": "blocked", "remaining": 0}), 403

    r.set(f"last_heartbeat:{p}", now)
    remaining = get_remaining_budget(site)
    return jsonify({"status": "ok", "remaining": int(remaining), "phase": phase()})

@app.route('/remaining')
def remaining():
    site = request.args.get("site")
    if site:
        site = resolve_site(site)
        return jsonify({
            "site": site,
            "remaining": int(get_remaining_budget(site)),
            "cooldown": int(get_cooldown_remaining(site))
        })
    return jsonify({
        s: {
            "remaining": int(get_remaining_budget(s)),
            "cooldown": int(get_cooldown_remaining(s))
        }
        for s in SITES
    })

DIGEST_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>This week · Countdown</title>
    <style>
        :root{--bg:#0b0d10;--card:#14171d;--line:#232732;--fg:#f4f6f8;--muted:#8b93a0;
              --faint:#5f6773;--good:#0ca30c;--warn:#ec835a;--accent:#3987e5}
        *{box-sizing:border-box}
        body{margin:0;background:var(--bg);color:var(--fg);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;padding:28px 16px max(28px,env(safe-area-inset-bottom));
            display:flex;justify-content:center}
        .wrap{width:100%;max-width:560px}
        .kicker{display:flex;align-items:center;gap:8px;justify-content:center;font-size:11.5px;
            font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--accent)}
        .range{text-align:center;font-size:12px;color:var(--faint);margin-bottom:18px}
        .lede{background:var(--card);border:1px solid var(--line);border-radius:16px;
            padding:22px 18px;text-align:center;margin-bottom:12px}
        .lede .big{font-size:40px;font-weight:700;letter-spacing:-1px;line-height:1}
        .lede .sub{font-size:13px;color:var(--muted);margin-top:8px;line-height:1.5}
        .lede .delta{font-weight:700}
        .delta.down{color:var(--good)} .delta.up{color:var(--warn)}
        .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
            padding:18px 16px;margin-bottom:12px}
        .card h2{font-size:12.5px;font-weight:600;color:var(--muted);margin:0 0 14px;letter-spacing:.02em}
        .row{display:flex;align-items:center;gap:10px;font-size:13.5px;margin-top:9px}
        .row .k{flex:1}
        .row .v{color:var(--muted);font-variant-numeric:tabular-nums}
        .bar{flex:2;height:8px;background:#0e1116;border-radius:4px;overflow:hidden}
        .bar i{display:block;height:100%;border-radius:4px}
        .note{font-size:12.5px;color:var(--muted);line-height:1.55;margin:12px 0 0}
        .note b{color:var(--fg)}
        .empty{font-size:12.5px;color:var(--faint);line-height:1.5}
        .back{display:block;text-align:center;margin-top:20px;font-size:12.5px;color:var(--faint);text-decoration:none}
        /* The way out. The dashboard sits on a different origin from the gate, so this is
           the only link that crosses back — it should not look like a footnote. */
        .back.home{margin-top:22px;padding:11px;border:1px solid var(--line);border-radius:11px;
            color:var(--fg);font-size:13.5px;font-weight:600}
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>This week</div>
    <div class="range">{{ d.range }}</div>

    <div class="lede">
        <div class="big">{{ d.total_min }}m</div>
        <div class="sub">on gated sites &mdash; about <b>{{ d.per_day_min }}m</b> a day.
        {% if d.delta_pct is not none %}
          <span class="delta {{ 'down' if d.delta_pct < 0 else 'up' }}">
          {{ '&darr;' | safe if d.delta_pct < 0 else '&uarr;' | safe }}{{ d.delta_abs }}%</span>
          vs the week before.
        {% else %}Not enough history yet to compare.{% endif %}
        </div>
    </div>

    <div class="card">
        <h2>Where it went</h2>
        {% for s in d.sites %}
        <div class="row">
            <span class="k">{{ s.label }}</span>
            <span class="bar"><i style="width:{{ s.pct }}%;background:{{ s.color }}"></i></span>
            <span class="v">{{ s.min }}m</span>
        </div>
        {% else %}
        <div class="empty">Nothing charged this week.</div>
        {% endfor %}
    </div>

    <div class="card">
        <h2>The binge signal</h2>
        <div class="note">
        {% if d.cooldowns %}
            <b>{{ d.cooldowns }}</b> cooldown{{ '' if d.cooldowns == 1 else 's' }}, of which
            <b>{{ d.rapid }}</b> came within {{ d.rapid_hours }}h of the last.
            {% if d.rapid %}That clustering &mdash; not the daily total &mdash; is what draws a longer wall.
            {% else %}Spread out rather than clustered, so every wall stayed at the base hour.{% endif %}
            {% if d.busiest_day %}<br>Heaviest day: <b>{{ d.busiest_day }}</b> ({{ d.busiest_min }}m).{% endif %}
        {% else %}
            No cooldowns this week &mdash; you stopped before the bucket ran dry every time.
        {% endif %}
        </div>
    </div>

    <div class="card">
        <h2>What you said</h2>
        {% if d.why.total %}
        <div class="note">Asked why you were reaching for it <b>{{ d.why.total }}</b> times.
            Naming it was enough to stop you <b>{{ d.why.passes }}</b> of those ({{ d.why.rate }}%).
            {% if d.top_trigger %}Most common reason: <b>{{ d.top_trigger }}</b>.{% endif %}
        </div>
        {% else %}
        <div class="empty">The reflection prompt didn't come up this week.</div>
        {% endif %}
        {% if d.worth.total %}
        <div class="note">Afterwards you called it worth it <b>{{ d.worth.rate }}%</b> of the time.
            {% if d.worst_trigger %}Least worth it when you went in
            <b>{{ d.worst_trigger }}</b> &mdash; {{ d.worst_pct }}%.{% endif %}
        </div>
        {% endif %}
    </div>

    <a class="back" href="/wrapped{{ qs }}">Your year, in review &rarr;</a>
    <a class="back" href="/stats{{ qs }}">Full usage stats &rarr;</a>
    <a class="back" href="/health{{ qs }}">Pi health</a>
    {% if back_url %}<a class="back home" href="{{ back_url }}">&larr; Back to {{ back_label }}</a>{% endif %}
</div>
</body>
</html>
"""

def wrapped_data(now=None):
    """The year, answering one question: did this work?

    A year-in-review that only celebrates volume would be telling you how much you
    doomscrolled with a party hat on. The number that matters is the trend — first
    quarter of the data against the last — and the reflection prompt's hit rate, because
    those are the two things that say whether the tool changed anything.

    Degrades to whatever exists: retention is a rolling 400 days and the box may only
    have weeks of it, so the page says how much it is working from rather than implying
    a full year.
    """
    now = now if now is not None else time.time()
    sites = list(SITES)
    vals = _mget_floats(_day_keys("usage", 365, now, sites))       # one MGET per 512 keys
    days, per_site, daily = [], {s: 0.0 for s in SITES}, []
    for i in range(365):
        key_day = time.strftime("%Y-%m-%d", time.localtime(now - (364 - i) * 86400))
        row = vals[i * len(sites):(i + 1) * len(sites)]
        for site, v in zip(sites, row):
            per_site[site] += v
        tot = sum(row)
        if tot:
            days.append(key_day)
            daily.append((key_day, tot))

    total = sum(t for _, t in daily)
    n = len(daily)
    if not n:
        return {"any": False}

    # Trend: the first quarter of the days that have data against the last quarter.
    # Comparing halves hides a late improvement; quarters are blunt but honest, and the
    # page says outright when there is too little to mean anything.
    q = max(1, n // 4)
    early = sum(t for _, t in daily[:q]) / q
    late = sum(t for _, t in daily[-q:]) / q
    trend_pct = round((late - early) / early * 100) if early > 0 else None

    busiest = max(daily, key=lambda x: x[1])
    quietest = min(daily, key=lambda x: x[1])

    months = {}
    for d, t in daily:
        months.setdefault(d[:7], 0.0)
        months[d[:7]] += t
    top_month = max(months.items(), key=lambda kv: kv[1]) if months else None

    cds = sum(len(x) for x in _mlrange(_day_keys("cooldown_events", 365, now)))

    why = reflection_summary(365, now)
    worth = worth_summary(365, now)
    worst = min((x for x in worth["rows"] if x["n"] >= 3), key=lambda x: x["pct"], default=None)
    best = max((x for x in worth["rows"] if x["n"] >= 3), key=lambda x: x["pct"], default=None)

    sites = sorted(({"label": SITES[s]["label"], "min": int(per_site[s] // 60),
                     "pct": round(100 * per_site[s] / total) if total else 0,
                     "color": series_color(i)}
                    for i, s in enumerate(SITES) if per_site[s] >= 60),
                   key=lambda x: -x["min"])

    return {
        "any": True, "days": n, "enough": n >= 60,
        "span": f"{days[0]} to {days[-1]}",
        "hours": round(total / 3600, 1),
        "full_days": round(total / 86400, 1),
        "avg_min": int(total / n // 60),
        "sites": sites,
        "trend_pct": trend_pct, "early_min": int(early // 60), "late_min": int(late // 60),
        "busiest_day": time.strftime("%A %-d %b", time.strptime(busiest[0], "%Y-%m-%d")),
        "busiest_min": int(busiest[1] // 60),
        "quietest_day": time.strftime("%A %-d %b", time.strptime(quietest[0], "%Y-%m-%d")),
        "quietest_min": int(quietest[1] // 60),
        "top_month": time.strftime("%B", time.strptime(top_month[0], "%Y-%m")) if top_month else "",
        "top_month_hours": round(top_month[1] / 3600, 1) if top_month else 0,
        "cooldowns": cds,
        "why": why, "worth": worth,
        "worst_trigger": worst["label"] if worst else "", "worst_pct": worst["pct"] if worst else 0,
        "best_trigger": best["label"] if best else "", "best_pct": best["pct"] if best else 0,
    }

WRAPPED_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Your year · Countdown</title>
    <style>
        :root{--bg:#0b0d10;--card:#14171d;--line:#232732;--fg:#f4f6f8;--muted:#8b93a0;
              --faint:#5f6773;--good:#0ca30c;--warn:#ec835a;--accent:#3987e5}
        *{box-sizing:border-box}
        body{margin:0;background:var(--bg);color:var(--fg);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;padding:28px 16px max(28px,env(safe-area-inset-bottom));
            display:flex;justify-content:center}
        .wrap{width:100%;max-width:560px}
        .kicker{display:flex;align-items:center;gap:8px;justify-content:center;font-size:11.5px;
            font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--accent)}
        .range{text-align:center;font-size:12px;color:var(--faint);margin-bottom:18px}
        .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
            padding:20px 18px;margin-bottom:12px}
        .card h2{font-size:12.5px;font-weight:600;color:var(--muted);margin:0 0 14px;letter-spacing:.02em}
        .hero{text-align:center;padding:26px 18px}
        .hero .big{font-size:46px;font-weight:700;letter-spacing:-1.5px;line-height:1}
        .hero .sub{font-size:13.5px;color:var(--muted);margin-top:10px;line-height:1.55}
        .verdict{text-align:center;padding:24px 18px}
        .verdict .n{font-size:38px;font-weight:700;letter-spacing:-1px;line-height:1}
        .verdict .n.down{color:var(--good)} .verdict .n.up{color:var(--warn)}
        .verdict .lab{font-size:13px;color:var(--muted);margin-top:10px;line-height:1.55}
        .row{display:flex;align-items:center;gap:10px;font-size:13.5px;margin-top:9px}
        .row .k{flex:1}
        .row .v{color:var(--muted);font-variant-numeric:tabular-nums}
        .bar{flex:2;height:8px;background:#0e1116;border-radius:4px;overflow:hidden}
        .bar i{display:block;height:100%;border-radius:4px}
        .fact{display:flex;justify-content:space-between;gap:12px;font-size:13.5px;
            padding:9px 0;border-bottom:1px solid var(--line)}
        .fact:last-child{border-bottom:none}
        .fact .k{color:var(--muted)} .fact .v{color:var(--fg);font-weight:600;text-align:right}
        .note{font-size:12.5px;color:var(--muted);line-height:1.55;margin:12px 0 0}
        .note b{color:var(--fg)}
        .thin{font-size:12px;color:var(--faint);line-height:1.5;margin-top:12px}
        .back{display:block;text-align:center;margin-top:20px;font-size:12.5px;color:var(--faint);text-decoration:none}
        .back.home{margin-top:22px;padding:11px;border:1px solid var(--line);border-radius:11px;
            color:var(--fg);font-size:13.5px;font-weight:600}
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>Your year</div>
    {% if personal_note %}<div class="card hero"><div class="sub">{{ personal_note }}</div></div>{% endif %}
    {% if not d.any %}
    <div class="card"><div class="note">No usage recorded yet. This page fills in as the
        history does &mdash; it keeps a rolling 400 days.</div></div>
    {% else %}
    <div class="range">{{ d.span }} &middot; {{ d.days }} days with any usage</div>

    <div class="card hero">
        <div class="big">{{ d.hours }}h</div>
        <div class="sub">on the sites you chose to budget &mdash; about <b>{{ d.avg_min }}m</b> on
            a day you used them at all. That's <b>{{ d.full_days }}</b> entire days.</div>
    </div>

    <div class="card verdict">
        <h2>Did it work?</h2>
        {% if d.trend_pct is none %}
        <div class="lab">Not enough history to say yet.</div>
        {% else %}
        <div class="n {{ 'down' if d.trend_pct < 0 else 'up' }}">
            {{ '&darr;' | safe if d.trend_pct < 0 else '&uarr;' | safe }}{{ d.trend_pct|abs }}%</div>
        <div class="lab">Your last stretch averaged <b>{{ d.late_min }}m</b> a day against
            <b>{{ d.early_min }}m</b> at the start.
    {% if not d.enough %}Only {{ d.days }} days of data, so treat this as a hint rather
            than a result.{% elif d.trend_pct < -10 %}That is the number this whole thing exists
            to move.{% elif d.trend_pct > 10 %}Worth knowing rather than worth hiding.{% else %}
            Roughly flat.{% endif %}</div>
        {% endif %}
    </div>

    <div class="card">
        <h2>Where the time went</h2>
        {% for s in d.sites %}
        <div class="row">
            <span class="k">{{ s.label }}</span>
            <span class="bar"><i style="width:{{ s.pct }}%;background:{{ s.color }}"></i></span>
            <span class="v">{{ s.min }}m &middot; {{ s.pct }}%</span>
        </div>
        {% endfor %}
    </div>

    <div class="card">
        <h2>Notable days</h2>
        <div class="fact"><span class="k">Heaviest</span><span class="v">{{ d.busiest_day }} &middot; {{ d.busiest_min }}m</span></div>
        <div class="fact"><span class="k">Lightest</span><span class="v">{{ d.quietest_day }} &middot; {{ d.quietest_min }}m</span></div>
        {% if d.top_month %}<div class="fact"><span class="k">Heaviest month</span><span class="v">{{ d.top_month }} &middot; {{ d.top_month_hours }}h</span></div>{% endif %}
        <div class="fact"><span class="k">Times you hit the wall</span><span class="v">{{ d.cooldowns }}</span></div>
    </div>

    {% if d.why.total %}
    <div class="card">
        <h2>What you told yourself</h2>
        <div class="note">The gate asked why you were reaching for it <b>{{ d.why.total }}</b> times.
            Naming it was enough to stop you <b>{{ d.why.passes }}</b> of those
            (<b>{{ d.why.rate }}%</b>){% if d.why.rows %}, and the reason you gave most often was
            <b>{{ d.why.rows[0].label }}</b>{% endif %}.</div>
        {% if d.worth.total %}
        <div class="note">Afterwards you judged it worth it <b>{{ d.worth.rate }}%</b> of the time.
            {% if d.worst_trigger %}Least worth it going in <b>{{ d.worst_trigger }}</b>
            ({{ d.worst_pct }}%){% if d.best_trigger and d.best_trigger != d.worst_trigger %};
            most worth it <b>{{ d.best_trigger }}</b> ({{ d.best_pct }}%){% endif %}.{% endif %}</div>
        <div class="thin">That pairing is the most useful thing on this page: the reason you go in
            predicts the answer better than the site does.</div>
        {% endif %}
    </div>
    {% endif %}
    {% endif %}

    <a class="back" href="/digest{{ qs }}">This week &rarr;</a>
    <a class="back" href="/stats{{ qs }}">Usage stats</a>
    {% if back_url %}<a class="back home" href="{{ back_url }}">&larr; Back to {{ back_label }}</a>{% endif %}
</div>
</body>
</html>
"""

def personal_note(now=None):
    """A line for the two one-day anchors. Dashboard only — the gate is served on the
    gated site's origin, and a greeting there would leak the date to any script on it."""
    now = now if now is not None else time.time()
    name, _ = active_theme(now)
    if name == "birthday":
        return "Happy birthday. Spend today somewhere that isn't a feed."
    if name == "anniversary":
        first = _try(lambda: r.get("first_run")) or ""
        yrs = max(1, time.localtime(now).tm_year - int(first[:4] or 0)) if first[:4].isdigit() else 1
        return (f"{yrs} year{'' if yrs == 1 else 's'} since you set this box up. "
                f"Whatever it has saved you, it saved quietly.")
    return ""

@app.route('/wrapped')
def wrapped():
    return render_page(WRAPPED_PAGE, d=wrapped_data(),
                       personal_note=_try(personal_note, ""))

@app.route('/digest')
def digest():
    """The week in one screen.

    Everything here already existed — daily usage, cooldown clustering, reflection
    triggers, the worth-it verdicts — and all of it was only visible if you went looking,
    which you don't while things feel fine. A tool whose whole premise is "make the
    invisible visible" shouldn't need you to remember to go and look.
    """
    now = time.time()
    sites = list(SITES)
    recent = _mget_floats(_day_keys("usage", 14, now, sites))      # both weeks, one go
    days_secs, per_site = [], {s: 0.0 for s in SITES}
    for i in range(7, 14):                                          # last 7 days
        key_day = time.strftime("%Y-%m-%d", time.localtime(now - (13 - i) * 86400))
        row = recent[i * len(sites):(i + 1) * len(sites)]
        for s, v in zip(sites, row):
            per_site[s] += v
        days_secs.append((key_day, sum(row)))
    total = sum(t for _, t in days_secs)
    prior = sum(recent[:7 * len(sites)])                            # the week before

    top = max(per_site.values()) or 1
    sites = [{"label": SITES[s]["label"], "min": int(per_site[s] // 60),
              "pct": round(100 * per_site[s] / top),
              "color": series_color(i)}
             for i, s in enumerate(SITES) if per_site[s] >= 60]
    sites.sort(key=lambda x: -x["min"])

    cd = []
    for entries in _mlrange(_day_keys("cooldown_events", 7, now)):
        for raw in entries:
            try:
                cd.append(float(raw.split()[0]))
            except (ValueError, IndexError):
                continue
    cd.sort()
    rapid = sum(1 for a, b in zip(cd, cd[1:]) if b - a <= RAPID_REPEAT_WINDOW)

    busiest = max(days_secs, key=lambda x: x[1]) if days_secs else None
    why = reflection_summary(7, now)
    worth = worth_summary(7, now)
    worst = min((row for row in worth["rows"] if row["n"] >= 2),
                key=lambda x: x["pct"], default=None)

    d = {
        "range": time.strftime("%b %-d", time.localtime(now - 6 * 86400)) + " – "
                 + time.strftime("%b %-d", time.localtime(now)),
        "total_min": int(total // 60), "per_day_min": int(total // 60 // 7),
        "delta_pct": None if prior <= 0 else round((total - prior) / prior * 100),
        "delta_abs": 0 if prior <= 0 else abs(round((total - prior) / prior * 100)),
        "sites": sites, "cooldowns": len(cd), "rapid": rapid,
        "rapid_hours": RAPID_REPEAT_WINDOW // 3600,
        # Date as well as weekday: "Tuesday" alone is ambiguous the moment the window is
        # not exactly seven days, and even inside seven it reads as "which Tuesday?".
        "busiest_day": time.strftime("%A %-d %b", time.strptime(busiest[0], "%Y-%m-%d")) if busiest and busiest[1] else "",
        "busiest_min": int(busiest[1] // 60) if busiest else 0,
        "why": why, "worth": worth,
        "top_trigger": why["rows"][0]["label"] if why["rows"] else "",
        "worst_trigger": worst["label"] if worst else "",
        "worst_pct": worst["pct"] if worst else 0,
    }
    return render_page(DIGEST_PAGE, d=d)

CPU_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>CPU · Countdown</title>
    <style>
        :root{--bg:#0b0d10;--card:#14171d;--line:#232732;--fg:#f4f6f8;--muted:#8b93a0;
              --faint:#5f6773;--go:#3ecf7c;--wait:#f0a63a;--bad:#e5484d}
        *{box-sizing:border-box}
        body{margin:0;background:var(--bg);color:var(--fg);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;padding:26px 16px max(26px,env(safe-area-inset-bottom));
            display:flex;justify-content:center}
        .wrap{width:100%;max-width:560px}
        .kicker{display:flex;align-items:center;gap:8px;justify-content:center;font-size:11.5px;
            font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--go)}
        .now{text-align:center;font-size:40px;font-weight:700;letter-spacing:-1.5px;line-height:1;margin:6px 0 2px}
        .sub{text-align:center;font-size:12.5px;color:var(--faint);margin-bottom:16px}
        .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
            padding:16px 14px;margin-bottom:12px}
        .card h2{font-size:12.5px;font-weight:600;color:var(--muted);margin:0 0 12px;letter-spacing:.02em}
        /* The chart. A real plot area with room for an axis, which the card sparkline has not. */
        .plot{display:flex;gap:8px}
        .yax{display:flex;flex-direction:column;justify-content:space-between;
            font-size:10px;color:var(--faint);font-variant-numeric:tabular-nums;
            text-align:right;width:26px;padding:1px 0}
        .cpubig{flex:1;display:block;height:190px}
        .cpubig .gl{stroke:var(--line);stroke-width:.5;vector-effect:non-scaling-stroke}
        .cpubig .gl.mid{stroke-dasharray:2 3}
        .cpubig polyline{fill:none;stroke-width:1.8;vector-effect:non-scaling-stroke;
            stroke-linejoin:round;stroke-linecap:round}
        .xax{display:flex;justify-content:space-between;font-size:10px;color:var(--faint);
            margin:5px 0 0 34px}
        table{width:100%;border-collapse:collapse;font-size:13px;margin-top:2px}
        th{color:var(--faint);font-weight:600;font-size:10.5px;text-align:right;
            padding:5px 6px;border-bottom:1px solid var(--line)}
        td{padding:7px 6px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
        th:first-child,td:first-child{text-align:left;color:var(--fg)}
        .sw{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:7px;vertical-align:3px}
        .meta{display:flex;gap:10px}
        .meta .m{flex:1;background:var(--card);border:1px solid var(--line);border-radius:14px;
            padding:13px 10px;text-align:center}
        .meta .v{font-size:20px;font-weight:700;letter-spacing:-.4px}
        .meta .k{font-size:10.5px;color:var(--faint);margin-top:4px;text-transform:uppercase;letter-spacing:.04em}
        .back{display:block;text-align:center;margin-top:18px;font-size:12.5px;color:var(--faint);text-decoration:none}
        .back.home{margin-top:20px;padding:11px;border:1px solid var(--line);border-radius:11px;
            color:var(--fg);font-size:13.5px;font-weight:600}
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>CPU</div>
    <div class="now" id="cpuNow">{{ d.cpu.pct }}%</div>
    <div class="sub">{{ d.cpu.cores }} cores &middot; last {{ window_min }} minutes &middot; updates every 4s</div>

    <div class="card">
        <div class="plot">
            <div class="yax"><span>100%</span><span>75</span><span>50</span><span>25</span><span>0</span></div>
            <svg class="cpubig" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                <line class="gl" x1="0" y1="25" x2="100" y2="25"/>
                <line class="gl mid" x1="0" y1="50" x2="100" y2="50"/>
                <line class="gl" x1="0" y1="75" x2="100" y2="75"/>
                <line class="gl" x1="0" y1="100" x2="100" y2="100"/>
                <g id="cpuBigLines">{% for pts in cpu_lines %}<polyline points="{{ pts }}" style="stroke:{{ cpu_colors[loop.index0 % cpu_colors|length] }}"/>{% endfor %}</g>
            </svg>
        </div>
        <div class="xax"><span>{{ window_min }} min ago</span><span>now</span></div>
    </div>

    <div class="card">
        <h2>Per core</h2>
        <table id="coreTable">
            <tr><th>Core</th><th>Now</th><th>Average</th><th>Peak</th></tr>
            {% for c in cpu_rows %}
            <tr><td><span class="sw" style="background:{{ c.color }}"></span>Core {{ c.core }}</td>
                <td>{{ c.now }}%</td><td>{{ c.avg }}%</td><td>{{ c.peak }}%</td></tr>
            {% endfor %}
        </table>
    </div>

    <div class="meta">
        <div class="m"><div class="v" id="load1">{{ '%.2f'|format(d.cpu.load[0]) }}</div><div class="k">load 1m</div></div>
        <div class="m"><div class="v">{{ '%.2f'|format(d.cpu.load[1]) }}</div><div class="k">load 5m</div></div>
        <div class="m"><div class="v">{% if d.temp_c is not none %}{{ d.temp_c }}&deg;{% else %}&mdash;{% endif %}</div><div class="k">temp</div></div>
    </div>

    <a class="back" href="/health{{ qs }}">&larr; Pi health</a>
    {% if back_url %}<a class="back home" href="{{ back_url }}">&larr; Back to {{ back_label }}</a>{% endif %}
</div>
{% raw %}
<script>
(function(){
  var COL=__CPU_COLORS__;
  function draw(d){
    var rows=((d.cpu&&d.cpu.hist)||[]).filter(function(r){return r&&r.length;});
    var g=document.getElementById("cpuBigLines");
    if(!g||!rows.length) return;
    if(rows.length===1) rows=rows.concat(rows);
    var nc=Math.max.apply(null, rows.map(function(r){return r.length;}));
    if(g.children.length!==nc){
      g.innerHTML="";
      for(var c=0;c<nc;c++){
        var pl=document.createElementNS("http://www.w3.org/2000/svg","polyline");
        pl.style.stroke=COL[c%COL.length]; g.appendChild(pl);
      }
    }
    for(var c=0;c<nc;c++){
      var pts=[];
      for(var i=0;i<rows.length;i++){
        var v=Math.max(0,Math.min(100, rows[i][c]||0));
        pts.push((100*i/(rows.length-1)).toFixed(2)+","+(100-v).toFixed(2));
      }
      g.children[c].setAttribute("points", pts.join(" "));
    }
    var t=document.getElementById("coreTable");
    if(t){
      for(var c=0;c<nc;c++){
        var vals=rows.map(function(r){return r[c]||0;});
        var tr=t.rows[c+1]; if(!tr) continue;
        tr.cells[1].textContent=Math.round(vals[vals.length-1])+"%";
        tr.cells[2].textContent=Math.round(vals.reduce(function(a,b){return a+b;},0)/vals.length)+"%";
        tr.cells[3].textContent=Math.round(Math.max.apply(null,vals))+"%";
      }
    }
    var n=document.getElementById("cpuNow"); if(n&&d.cpu) n.textContent=d.cpu.pct+"%";
    var l=document.getElementById("load1"); if(l&&d.cpu) l.textContent=d.cpu.load[0].toFixed(2);
  }
  function poll(){ fetch("/health?fmt=json&_="+Date.now(),{cache:"no-store"})
    .then(function(r){return r.json();}).then(draw).catch(function(){}); }
  setInterval(poll, 4000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden) poll(); });
})();
</script>
{% endraw %}
</body>
</html>
"""

@app.route('/cpu')
def cpu_detail():
    d = collect_health()
    hist = d["cpu"].get("hist", [])
    return render_page(CPU_PAGE, d=d,
        cpu_lines=_cpu_lines(hist, w=100, h=100),
        cpu_rows=_cpu_stat_rows(hist),
        cpu_colors=CPU_LINE_COLORS,
        window_min=max(1, round(CPU_HIST_LEN * 4 / 60)))

@app.route('/')
def index():
    """The dashboard's front door. Since the monitoring pages moved to the box's own
    origin, "http://<box>:5000" is an address people actually type — and it used to 404."""
    return redirect('/stats')

@app.route('/stats')
def stats():
    now = time.time()
    # Derived from SITES, not a second hand-maintained list. The old fixed list omitted
    # "news", so every minute of news reading was charged to the shared bucket — draining
    # it and triggering cooldowns — while the dashboard showed nothing. The headline
    # number was wrong by however much news you read, which for a tool whose whole job is
    # making usage visible is the worst place to be wrong. Add a site to SITES and it
    # appears here automatically.
    order = list(SITES)
    series = [{"key": s, "label": SITES[s]["label"], "color": series_color(i)}
              for i, s in enumerate(order)]

    # Last 14 local days, oldest first.
    vals = _mget_floats(_day_keys("usage", 14, now, order))
    days = []
    totals = []
    for i in range(13, -1, -1):
        t = time.localtime(now - i * 86400)
        key_day = time.strftime("%Y-%m-%d", t)
        row = vals[(13 - i) * len(order):(14 - i) * len(order)]
        secs = dict(zip(order, row))
        total = sum(secs.values())
        totals.append(total)
        days.append({
            "label": time.strftime("%-d", t) if i % 2 == 0 else "",
            "label_full": time.strftime("%a %b %-d", t),
            "secs": secs, "total": total,
        })

    # Scale segments against the biggest day (leave 0-height segments out entirely).
    max_total = max(totals) or 1
    for d in days:
        d["pcts"], d["mins"] = {}, {}
        for s in order:
            pct = d["secs"][s] / max_total * 100
            d["pcts"][s] = round(pct, 1) if pct >= 1 else 0
            d["mins"][s] = int(d["secs"][s] // 60)
        d["total_min"] = int(d["total"] // 60)

    today_min = days[-1]["total_min"]
    this7 = sum(totals[7:]) / 7
    prior7 = sum(totals[:7]) / 7
    week_avg = int(this7 // 60)
    if prior7 <= 0:
        trend, trend_cls = "—", ""
    else:
        pct = (this7 - prior7) / prior7 * 100
        if pct <= -1:   trend, trend_cls = f"▾{abs(int(pct))}%", "down"   # less = good
        elif pct >= 1:  trend, trend_cls = f"▴{int(pct)}%", "up"
        else:           trend, trend_cls = "flat", ""

    # Cooldown clustering: does a fresh cooldown tend to start soon after the last
    # one ended? That "rapid repeat" (within RAPID_REPEAT_WINDOW) is the binge
    # signal the escalating-cooldown idea targets — high daily *totals* don't imply
    # it. Gather the last 7 days of "<epoch> <site>" events, ordered.
    cd_events = []
    for entries in _mlrange(_day_keys("cooldown_events", 7, now)):
        for raw in entries:
            try:
                cd_events.append(float(raw.split()[0]))
            except (ValueError, IndexError):
                continue
    cd_events.sort()
    week_rapid = sum(1 for a, b in zip(cd_events, cd_events[1:])
                     if b - a <= RAPID_REPEAT_WINDOW)

    # Study mode (free, unbudgeted) is logged separately — this is the one metric the
    # whole thing is FOR, so surface it. Today + this-week's foreground study minutes.
    today_key = time.strftime("%Y-%m-%d", time.localtime(now))

    today_ts = sorted(t for t in cd_events
                      if time.strftime("%Y-%m-%d", time.localtime(t)) == today_key)
    cd_today_times = [time.strftime("%-I:%M%p", time.localtime(t)).lower() for t in today_ts]
    cd_today_rapid = sum(1 for a, b in zip(today_ts, today_ts[1:])
                         if b - a <= RAPID_REPEAT_WINDOW)
    cd = {
        "today_n": len(today_ts), "today_times": cd_today_times,
        "today_rapid": cd_today_rapid,
        "week_n": len(cd_events), "week_rapid": week_rapid,
        "hours": RAPID_REPEAT_WINDOW // 3600,
    }

    # Liveness: if nothing has been charged for days, either it's a clean streak or
    # the heartbeat pipeline silently broke (the system fails open — this is the alarm).
    last = r.get("last_charge")
    stale = False
    if not last:
        live_line = "No usage recorded yet."
    else:
        age = now - float(last)
        if age < 3600:        ago = f"{int(age // 60)}m ago"
        elif age < 86400:     ago = f"{int(age // 3600)}h ago"
        else:                 ago = f"{int(age // 86400)}d ago"
        if age > 3 * 86400:
            stale = True
            live_line = f"Nothing charged in {int(age // 86400)} days — clean streak, or a broken heartbeat?"
        else:
            live_line = f"Heartbeat alive — last charged {ago}."

    why = reflection_summary()
    worth = worth_summary()
    shadow = shadow_comparison()
    return render_page(STATS_PAGE, why=why, worth=worth, series=series, shadow=shadow,
        days=days, today_min=today_min, week_avg=week_avg,
        trend=trend, trend_cls=trend_cls, live_line=live_line, stale=stale, cd=cd)

def reset_day(now=None):
    """Which day's reset the current moment belongs to. Before NIGHT_END_HOUR you are
    still inside yesterday's day, so the reset that matters is yesterday's."""
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    if lt.tm_hour < NIGHT_END_HOUR:
        return time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    return time.strftime("%Y-%m-%d", lt)

def catch_up_reset(now=None):
    """Run the daily reset if the scheduled one was missed.

    The cron job only fires if the process is running at 07:00 — so a box that was off,
    or restarting, silently skipped a day. That mattered most for `night_spent`, which
    nothing else clears: a missed reset meant no night buffer that night. Called at
    startup; idempotent, because it is keyed on which reset-day was last completed.
    """
    if r.get("last_reset") != reset_day(now):
        print("[RESET] catching up a missed daily reset")
        daily_reset(now)
        return True
    return False

def shadow_comparison(days=7, now=None):
    """Heartbeat minutes vs passively-observed minutes, per site, over `days`.

    An experiment (started 2026-08-03). The heartbeat is the only reason injected
    JavaScript runs on gated sites, and that injection is the biggest single item in this
    project's threat model. addon.py now also measures the same thing passively, by
    watching the requests those sites make on their own. If the two numbers track each
    other, the injection could eventually go; if they don't, this says by how much.

    RESULT, 2026-08-10 (7 days, 332 heartbeat minutes over 42 paired hours). Both
    predictions were wrong. Passive tracks the heartbeat at 1.03x overall — reddit 1.05x,
    youtube 1.01x — with daily ratios between 0.96x and 1.08x.

    The prediction that passive would UNDERCOUNT (client-side feeds mean reading generates
    no traffic) is specifically dead: across those 42 hours there were ZERO in which the
    heartbeat charged time and passive saw nothing. The error is one-directional and
    small — passive counted 6.1 minutes across 4 hours where the heartbeat charged
    nothing, all YouTube, most likely background audio. It never goes blind; when wrong it
    over-counts, which fails toward charging you rather than gifting you time.

    Checked for circularity before believing it: a 3% agreement between two supposedly
    independent meters is more easily explained by one watching the other's traffic. It is
    not. The /budget/* handler returns before shadow_note() is reached, verified by
    sending a heartbeat POST through request() and confirming the shadow buffer stays
    empty.

    TWO THINGS THIS DOES NOT SETTLE.

    Passive cannot observe visibility, and never will — that fact lives in the browser.
    It agrees with the heartbeat because sites throttle themselves when hidden, which is
    third-party behaviour nobody here controls. If YouTube changes how a backgrounded tab
    polls, passive starts over-counting and nothing announces it.

    The injection does two jobs. It measures time AND draws the countdown, the wind-down
    bar and the last-minute warning. Passive replaces the measuring only; removing the
    script removes the on-page UI with it. That is a product decision, not a measurement
    one.

    Sample is thin: 5.5 hours of real use, and per-hour ratios spread 0.72x-1.28x even
    though daily aggregates hold within 8%.
    """
    now = now if now is not None else time.time()
    started = _try(lambda: r.get("shadow_started"))
    if not started:
        return {"rows": [], "hb": 0, "sh": 0, "ratio": None, "days": 0,
                "any": False, "settled": False, "running": ""}
    since = float(started)

    # Compare only whole hours during which BOTH meters were running. Day-granular keys
    # cannot express "the meter started at 15:49", and the first attempt at this divided
    # 15 hours of heartbeat by 3 hours of passive and reported 0.22x — an artefact that
    # looked exactly like a real finding. Start at the hour AFTER the meter came up, so
    # neither side gets credit for a partial hour.
    first = ((int(since) // 3600) + 1) * 3600
    hours_list = []
    t = first
    while t < now - 3600 and (now - t) <= days * 86400:
        hours_list.append(t)
        t += 3600
    hours_list = hours_list[-days * 24:]

    stamps = [(time.strftime("%Y-%m-%d", time.localtime(t)),
               time.strftime("%H", time.localtime(t))) for t in hours_list]
    sites = list(SITES)
    hb_all = _mget_floats([f"usage_hour:{d}:{h}:{s}" for s in sites for d, h in stamps])
    sh_all = _mget_floats([f"shadow_hour:{d}:{h}:{s}" for s in sites for d, h in stamps])
    n = len(stamps)
    rows, hb_tot, sh_tot = [], 0.0, 0.0
    for idx, site in enumerate(sites):
        hb = sum(hb_all[idx * n:(idx + 1) * n])
        sh = sum(sh_all[idx * n:(idx + 1) * n])
        hb_tot += hb
        sh_tot += sh
        if hb >= 60 or sh >= 60:
            rows.append({"label": SITES[site]["label"],
                         "hb": int(hb // 60), "sh": int(sh // 60),
                         "ratio": round(sh / hb, 2) if hb >= 300 else None})
    rows.sort(key=lambda x: -max(x["hb"], x["sh"]))
    hours = len(hours_list)          # whole hours where both meters were running
    return {"rows": rows, "hb": int(hb_tot // 60), "sh": int(sh_tot // 60),
            "ratio": round(sh_tot / hb_tot, 2) if hb_tot >= 300 else None,
            "days": max(1, hours // 24), "any": True,
            "settled": hours >= 12,          # under half a day the ratio is noise
            "running": f"{hours} compared hour{'' if hours == 1 else 's'}"}

def daily_reset(now=None):
    pools = set()
    for site in SITES:
        token = r.get(f"active_token:{site}")
        if token:
            r.delete(f"session:{token}")
        r.delete(f"active_token:{site}")
        pools.add(pool(site))
    for p in pools:                    # clear shared pools once (covers grouped sites)
        r.delete(f"spent:{p}")
        r.delete(f"night_spent:{p}")
        r.delete(f"cooldown:{p}")
        r.delete(f"cooldown_secs:{p}")
        r.delete(f"last_heartbeat:{p}")
        r.delete(f"refilled_through:{p}")
    r.set("last_reset", reset_day(now))
    print("[RESET] Daily budget reset complete")

def sample_history():
    """Fill the CPU and temperature history on a fixed cadence.

    This used to happen inside collect_health(), which meant samples were only taken
    while someone had /health open. Arrive at the page after any gap and the chart began
    from nothing -- the "last 4 minutes" were never recorded, because nobody was
    watching. A monitor that only monitors while observed is not a monitor.

    It also makes the readings honest. _cpu_stats() measures between its own calls, so
    the percentage used to cover "however long since the last HTTP request" -- a
    different window every time. Now it is a steady 4 seconds.
    """
    agg, per_core = _try(_cpu_stats, (0.0, []))
    if per_core:
        _CPU_HIST.append(list(per_core))
        _CPU_LATEST["agg"] = agg
    t = _try(_temp_c)
    if t is not None:
        _TEMP_HIST.append(t)


scheduler = BackgroundScheduler()
# 4s to match what the page polls at, so the chart's x-axis maths stays correct.
scheduler.add_job(sample_history, 'interval', seconds=4, max_instances=1,
                  coalesce=True, next_run_time=datetime.now())
# Reset at the curfew's end (7am), not midnight — a "fresh day" of budget starts when
# you wake, and this avoids handing out fresh budget in the middle of the night window.
scheduler.add_job(daily_reset, 'cron', hour=NIGHT_END_HOUR, minute=0)
scheduler.start()

# ---------------------------------------------------------------------------
# Pi health monitor: read live system metrics straight off /proc, /sys and a
# couple of quick shell-outs, and draw a line-art board whose ethernet port /
# SoC / power connector reflect real state. Every reader is wrapped in _try so
# a missing file on a non-Pi host degrades to a dash instead of a 500.
# ---------------------------------------------------------------------------

_TEMP_HIST = deque(maxlen=90)   # recent CPU temps for the sparkline (~6 min at 4s polls)
# ~4 minutes at the 4s poll. 180 samples fit the buffer but not the eye: across a
# 100-unit viewBox that put a point every 0.55 units and the lines read as noise.
# The card draws the last 30 (~2 min) — far enough below the page's window that the
# two are visibly different views, not the same picture at two sizes.
CPU_HIST_LEN = 60
CPU_CARD_SAMPLES = 30
_CPU_HIST = deque(maxlen=CPU_HIST_LEN)   # per-core CPU%, one list per sample
_CPU_LATEST = {"agg": 0.0}               # most recent aggregate %, for the headline number

# Per-core line colours. Retro/outrun on purpose — the page is near-black with a hex
# matrix behind it, so saturated pink/cyan/purple/amber belongs here in a way it wouldn't
# on white. Straight neon (#ff6ec7, #00f0ff) is far too light to read on this surface, so
# these are the same hues darkened into the legible band.
#
# Validated as a set against the card surface: lightness, chroma, contrast and
# normal-vision separation all pass. Cyan vs pink sits at 6.4 ΔE under deuteranopia,
# which is the floor band — legal ONLY with a second, non-colour cue. That is why the
# legend shows each core's live percentage next to its swatch rather than relying on
# the colour alone. Do not drop those numbers.
CPU_LINE_COLORS = ["#d9548f", "#0fa8b8", "#8f57d4", "#c08420"]
# Four is enough for the Pi. On a machine with more cores the colours repeat, and that is
# fine HERE though it is forbidden for the site chart: cores are interchangeable. Nobody
# reads "core 6 is busy" — you read the level, the spread and the trend. Colour is not
# carrying identity, so a repeat asserts nothing false. For SERIES_COLORS it would.
_NET_PREV = {}                  # iface -> (monotonic_t, rx_bytes, tx_bytes) for throughput
_CPU_PREV = {}                  # {"v": /proc/stat snapshot} — CPU% is measured between calls
_HEALTH_CACHE = {}              # {"t": monotonic, "d": payload} — collapses concurrent pollers
_FEED_PREV = {}                 # {"v": (monotonic_t, enc_bytes, unenc_bytes)} for the packet feed

def _persistent_token(key):
    tok = r.get(key)
    if not tok:
        tok = secrets.token_urlsafe(24)
        r.set(key, tok)
    return tok

def ui_token():
    """A secret the monitoring pages embed so their own polling can be told apart from a
    script on a gated site. Any page on reddit.com can call /budget/* same-origin, and
    Sec-Fetch headers can't distinguish our gate page from Reddit's own — but a foreign
    script can't read this token, because the pages carrying it can only be fetched by a
    real navigation (enforced in addon.py). Persisted so it survives a restart."""
    return _persistent_token("ui_token")

def feed_token():
    """The GATE page's token — and deliberately NOT ui_token.

    The gate renders in place of a gated site, so any script on that site can
    `fetch('/budget')` and read every byte of it, including this. That is unavoidable
    while the gate shares the site's origin, so the gate must not carry a secret worth
    stealing: this one unlocks /feed alone (two aggregate bytes-per-second numbers) and
    gives no access to /devices, /health, /stats or /remaining.
    """
    return _persistent_token("feed_token")

def back_to_gate():
    """Where "back" goes from a dashboard page.

    The dashboard lives on the box's own origin now, so it cannot know which site you
    were looking at when you tapped through — different origin, no referrer worth
    trusting. So the gate says: its links carry ?from=<site>, and that turns into a link
    back to that site, which re-renders the gate if you are still gated.

    `qs` is threaded through the links *between* dashboard pages so the trail survives
    stats -> health -> devices and you can still get out at the end.
    """
    site = _try(lambda: request.args.get("from", ""), "")
    if site not in SITES:
        return {"back_url": "", "back_label": "", "qs": ""}
    return {"back_url": SITES[site]["home"], "back_label": SITES[site]["label"],
            "qs": f"?from={site}"}

@app.context_processor
def _inject_theme():
    """?theme=<name> previews one, ?theme=off forces the standard look."""
    q = _try(lambda: request.args.get("theme"), None)
    name, th = (None, None) if q == "off" else active_theme(override=q)
    return {"theme_css": theme_css(th),
            "theme_label": th["label"] if th else "",
            "theme_emoji": th["emoji"] if th else "",
            "theme_bg": th["vars"]["bg"] if th else "#070b0e",
            "theme_name": name or ""}

@app.context_processor
def _inject_back():
    return back_to_gate()

@app.context_processor
def _inject_ui_token():
    # `mon` is the dashboard's absolute base URL. It is empty when the box has no
    # tailnet address, and the gate then hides the links rather than offering a dead one.
    return {"ui_tok": _try(ui_token, ""), "feed_tok": _try(feed_token, ""),
            "mon": _try(monitor_origin, "")}

# Swallowed exceptions, counted. Two of the twenty security findings were silent
# fail-open in the enforcement path, and they were hard to find precisely because this
# codebase had no way to say "something broke": _try() ate everything, the injected
# heartbeat eats its own fetch errors, and print() was the only logging. This does not
# change the failure behaviour — a missing /proc file on a non-Pi host should still
# degrade to a dash — it just stops the failure being invisible.
_ERRORS = Counter()          # "where:ExcType" -> count, since boot
_LAST_ERROR = {}             # {"what": str, "when": epoch}

def _note_error(exc):
    tb = traceback.extract_tb(exc.__traceback__)
    where = tb[-1].name if tb else "?"
    _ERRORS[f"{where}:{type(exc).__name__}"] += 1
    _LAST_ERROR.update(what=f"{where}: {type(exc).__name__}: {exc}"[:180], when=time.time())

# ---------------------------------------------------------------------------
# Batched reads.
#
# Every history page walks a range of days and reads one key per day per site. Done one
# at a time that is a network round-trip each: /wrapped issued 2,924 of them for a single
# render, and a profile showed two thirds of the page's time was socket wait rather than
# any computation. Nothing about that is fixed by faster code — the fix is asking for
# many keys at once.
#
# MGET for plain keys, a non-transactional pipeline for LRANGE (which has no multi-key
# form). Chunked, so a year of keys doesn't become one enormous command. Both degrade to
# empty rather than raising: a dashboard is not worth a 500.
# ---------------------------------------------------------------------------
BATCH = 512

def _mget(keys):
    out = []
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        vals = _try(lambda c=chunk: r.mget(c))
        out.extend(vals if vals is not None else [None] * len(chunk))
    return out

def _mget_floats(keys):
    return [float(v or 0) for v in _mget(keys)]

def _mlrange(keys):
    out = []
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        def run(c=chunk):
            pipe = r.pipeline(transaction=False)
            for k in c:
                pipe.lrange(k, 0, -1)
            return pipe.execute()
        vals = _try(run)
        out.extend(vals if vals is not None else [[] for _ in chunk])
    return out

def _day_keys(prefix, days, now, suffixes=None):
    """['usage:2026-08-04:reddit', ...] for `days` days back, newest last."""
    out = []
    for i in range(days - 1, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        if suffixes is None:
            out.append(f"{prefix}:{d}")
        else:
            out.extend(f"{prefix}:{d}:{sfx}" for sfx in suffixes)
    return out

def _try(fn, default=None):
    try:
        return fn()
    except Exception as exc:
        _note_error(exc)
        return default

def enforcement_status():
    """Is time actually being charged? The dashboard's honest answer.

    /stats has long carried a "last charged N ago" line, but it reads as a clean streak
    just as easily as a broken pipeline, and you only see it if you go looking. This is
    the same two clocks addon.py compares — pages loading during a live session vs. time
    actually being charged — reported as a state you can act on.
    """
    now = time.time()
    nav = _try(lambda: r.get("last_active_nav"))
    charge = _try(lambda: r.get("last_charge"))
    nav_ago = int(now - float(nav)) if nav else None
    charge_ago = int(now - float(charge)) if charge else None
    browsing = nav_ago is not None and nav_ago <= 5 * 60
    # Same comparison addon.py makes, so the banner and this card cannot disagree: the
    # passive meter's minutes against the heartbeat's over the same recent hours.
    hb, sh = _try(lambda: _meter_totals(now), (0.0, 0.0))
    judged = sh >= ENFORCEMENT_MIN_PASSIVE
    dead = judged and hb < sh * ENFORCEMENT_RATIO
    return {"ok": not dead, "browsing": browsing,
            "nav_ago": nav_ago, "charge_ago": charge_ago,
            "hb_min": round(hb / 60, 1), "passive_min": round(sh / 60, 1),
            "judged": judged}

ENFORCEMENT_WINDOW_HOURS = 2
ENFORCEMENT_MIN_PASSIVE  = 5 * 60
ENFORCEMENT_RATIO        = 0.4


def _meter_totals(now, hours=ENFORCEMENT_WINDOW_HOURS):
    """Heartbeat vs passive seconds over the last `hours` clock hours.

    Deliberately duplicated from addon.py rather than shared: these are two processes
    that must agree on a judgement, and the tests assert they reach the same verdict on
    the same data. An import across the proxy/app boundary would be the tighter coupling.
    """
    keys_h, keys_s = [], []
    for i in range(hours):
        t = time.localtime(now - i * 3600)
        day, hour = time.strftime("%Y-%m-%d", t), time.strftime("%H", t)
        for site in SITES:
            keys_h.append(f"usage_hour:{day}:{hour}:{site}")
            keys_s.append(f"shadow_hour:{day}:{hour}:{site}")
    return (sum(_mget_floats(keys_h)), sum(_mget_floats(keys_s)))


def error_summary():
    """Counts for /health. `proxy` comes from addon.py, which has its own swallowed
    exceptions on the other side of the loopback call and is otherwise unobservable."""
    proxy = 0
    try:
        proxy = int(r.get("proxy_errors") or 0)
    except Exception:
        pass                                  # never let the error reporter raise
    return {"total": sum(_ERRORS.values()), "proxy": proxy,
            "top": [{"what": k, "n": n} for k, n in _ERRORS.most_common(3)],
            "last": _LAST_ERROR.get("what", ""),
            "last_ago": int(time.time() - _LAST_ERROR["when"]) if _LAST_ERROR else None}

def _first_line(path):
    with open(path) as f:
        return f.readline().strip()

def _cpu_snap():
    cpu = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            p = line.split()
            v = list(map(int, p[1:]))
            cpu[p[0]] = (v[3] + v[4], sum(v))   # idle+iowait, total
    return cpu

def _cpu_stats():
    """Aggregate + per-core busy %, measured BETWEEN calls rather than by sleeping.

    This used to sleep 0.2s to take its own delta, which held one of waitress's few
    worker threads for the whole request — a handful of concurrent /health hits could
    starve the pool and stall the gate (and silently stop heartbeat charging). Now we
    diff against the previous snapshot, so a request never blocks; the poller's ~4s
    cadence also gives a steadier reading than a 0.2s window. First call reads 0.
    """
    a, b = _CPU_PREV.get("v"), _cpu_snap()
    _CPU_PREV["v"] = b
    def pct(name):
        if not a:
            return 0.0
        i1, t1 = a.get(name, (0, 0))
        i2, t2 = b.get(name, (0, 0))
        dt = t2 - t1
        return round(max(0.0, min(100.0, 100.0 * (1 - (i2 - i1) / dt))), 1) if dt > 0 else 0.0
    cores = sorted((k for k in b if k != "cpu" and k.startswith("cpu")),
                   key=lambda k: int(k[3:]))
    return pct("cpu"), [pct(c) for c in cores]

def _loadavg():
    return [float(x) for x in _first_line("/proc/loadavg").split()[:3]]

def _temp_c():
    return round(int(_first_line("/sys/class/thermal/thermal_zone0/temp")) / 1000.0, 1)

def _mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            info[k] = int(rest.strip().split()[0])   # kB
    total = info["MemTotal"]
    used = total - info.get("MemAvailable", info.get("MemFree", 0))
    return {"used_mb": round(used / 1024), "total_mb": round(total / 1024),
            "pct": round(100 * used / total, 1)}

def _disk(path="/"):
    """Disk use, matching what `df` reports.

    The old version computed used as total - f_bavail, which counts the ~5% of blocks
    reserved for root as if you had filled them: it showed 6.4 GB used of 30.9 GB when
    df said 4.7 GB of 29 GB. Alarming and wrong. f_bavail is what is available to YOU,
    f_bfree is what is actually free — so used is total - f_bfree, and the percentage is
    against used + available, not against the raw total. Same convention as df, so the
    dashboard and the terminal agree.
    """
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    return {"used_gb": round(used / 1e9, 1), "total_gb": round(total / 1e9, 1),
            "avail_gb": round(avail / 1e9, 1),
            "pct": round(100 * used / (used + avail)) if used + avail else 0}

def _uptime():
    secs = float(_first_line("/proc/uptime").split()[0])
    d, h, m = int(secs // 86400), int(secs % 86400 // 3600), int(secs % 3600 // 60)
    return f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")

def _iface_speed(base):
    """Link speed in Mbit, or None if the interface doesn't have one.

    Reading `speed` on a wireless or virtual interface legitimately fails with EINVAL,
    and tunnels report -1. That is "not applicable", not a fault — so it is caught here
    rather than by _try(), which would count it. A monitoring line that permanently reads
    "1 handled error" for a healthy box is worse than no line at all: it teaches you to
    ignore the number, which is the one thing it must never do.
    """
    try:
        v = int(_first_line(f"{base}/speed"))
    except (OSError, ValueError):
        return None
    return v if v >= 0 else None

def _iface(name):
    base = f"/sys/class/net/{name}"
    if not os.path.exists(base):
        return None
    state = _try(lambda: _first_line(f"{base}/operstate"), "unknown")
    speed = _iface_speed(base)
    # Real NICs report up/down honestly; tunnels (tailscale/wg) sit at "unknown"
    # while perfectly up, so treat anything but an explicit "down" as connected.
    up = state != "down" if name.startswith(("tailscale", "wg", "tun")) else state == "up"
    # Throughput: bytes/sec since the previous read (kept in _NET_PREV per interface).
    rx = _try(lambda: int(_first_line(f"{base}/statistics/rx_bytes")))
    tx = _try(lambda: int(_first_line(f"{base}/statistics/tx_bytes")))
    rx_bps = tx_bps = None
    if rx is not None and tx is not None:
        now = time.monotonic()
        prev = _NET_PREV.get(name)
        if prev and now - prev[0] >= 0.5:   # skip tiny gaps so the rate isn't a spike
            dt = now - prev[0]
            rx_bps = max(0, round((rx - prev[1]) / dt))
            tx_bps = max(0, round((tx - prev[2]) / dt))
        _NET_PREV[name] = (now, rx, tx)
    return {"up": up, "state": state, "speed": speed, "rx_bps": rx_bps, "tx_bps": tx_bps}

_PROC_PREV = {}          # pid -> (cpu ticks, monotonic) so CPU% is measured between polls

def _top_processes(n=6):
    """The few processes actually using the box, biggest first.

    Not a process list. There are 172 processes on this Pi and 127 are kernel threads —
    a full list is noise nobody opens twice. What the dashboard could not answer before
    is "what is eating the box", and six rows answers it. Read straight from /proc, so no
    subprocess and no privilege: this runs as an unprivileged account and the one sudo
    rule it has stays the one sudo rule it has.
    """
    now = time.monotonic()
    hz = os.sysconf("SC_CLK_TCK") or 100
    page = os.sysconf("SC_PAGE_SIZE") or 4096
    rows, seen = [], {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                st = f.read()
            close = st.rindex(")")                     # comm may contain spaces or parens
            name = st[st.index("(") + 1:close]
            f2 = st[close + 2:].split()
            ticks = int(f2[11]) + int(f2[12])          # utime + stime
            rss_mb = int(f2[21]) * page / 1048576
        except (OSError, ValueError, IndexError):
            continue                                    # process exited mid-read; fine
        if rss_mb < 1:
            continue                                    # kernel threads and noise
        seen[pid] = (ticks, now)
        prev = _PROC_PREV.get(pid)
        pct = 0.0
        if prev and now > prev[1]:
            pct = 100.0 * (ticks - prev[0]) / hz / (now - prev[1])
        rows.append({"name": name[:18], "mb": round(rss_mb),
                     "pct": round(max(0.0, min(100.0 * (os.cpu_count() or 1), pct)), 1)})
    _PROC_PREV.clear()
    _PROC_PREV.update(seen)
    rows.sort(key=lambda r: -r["mb"])
    return rows[:n]


def _listening_ports():
    """TCP sockets in LISTEN, and crucially WHICH address they are bound to.

    This is the F1 invariant made visible: the proxy ports must answer on the tailnet and
    loopback, never on 0.0.0.0. Parsed from /proc/net/tcp because `ss -tlnp` would only
    name the owning process as root, and a second sudo rule to prettify a status page is
    exactly the trade F6 and F7 were about. Ports are labelled from a static map instead.
    """
    known = {5000: "dashboard", 8080: "proxy · transparent", 8081: "proxy · regular",
             6379: "redis", 22: "ssh", 53: "dns"}
    out = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f2 = line.split()
            if len(f2) < 4 or f2[3] != "0A":            # 0A = LISTEN
                continue
            hexaddr, _, hexport = f2[1].rpartition(":")
            try:
                port = int(hexport, 16)
            except ValueError:
                continue
            # Detect the wildcard bind from the raw hex rather than by comparing the
            # decoded string. Structurally simpler, and it keeps the literal out of this
            # file — an invariant test forbids it, because the one legitimate reason to
            # write that address here is the one thing that must never happen.
            wildcard = set(hexaddr) == {"0"}
            if len(hexaddr) == 8:                        # IPv4, little-endian words
                addr = ".".join(str(int(hexaddr[i:i + 2], 16)) for i in (6, 4, 2, 0))
            else:
                addr = "::" if wildcard else "[v6]"
            if wildcard:
                scope, rank = "all interfaces", 0
            elif addr.startswith("127.") or addr == "::1":
                scope, rank = "loopback only", 2
            else:
                scope, rank = addr, 1
            cur = out.get(port)
            if cur is None or rank < cur["rank"]:
                out[port] = {"port": port, "scope": scope, "rank": rank,
                             "what": known.get(port, "")}
    return sorted(out.values(), key=lambda r: (r["rank"], r["port"]))


def _services(names):
    def one(s):
        return subprocess.run(["systemctl", "is-active", s],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    return {s: _try(lambda s=s: one(s), "unknown") for s in names}

def _power():
    raw = _try(lambda: subprocess.run(["vcgencmd", "get_throttled"],
              capture_output=True, text=True, timeout=2).stdout.strip(), "")
    val = _try(lambda: int(raw.split("=")[1], 16), 0) if "=" in raw else 0
    return {"ok": val == 0,
            "under_voltage_now": bool(val & 0x1),
            "throttled_now": bool(val & 0x4),
            "under_voltage_ever": bool(val & 0x10000)}

def _temp_class(t):
    # Bands set against what a Pi 4 actually does, not what feels hot to a person:
    # it soft-throttles at 80C and hard-throttles at 85C, and 40-60C is ordinary idle.
    # The old 55/70 split painted a perfectly healthy board amber, which is a false alarm.
    if t is None:
        return "off"
    return "cool" if t <= 65 else ("warm" if t <= 78 else "hot")

def _pct_class(p):
    # Shared green/amber/red for utilisation gauges (memory, and the RAM chip glow).
    return "cool" if p < 70 else ("warm" if p < 85 else "hot")

def _cpu_stat_rows(hist):
    """Per-core now / mean / peak over the window. The table view the chart needs: it
    carries identity without colour, which the palette requires (cyan and pink sit in the
    colour-vision floor band), and it beats a hover tooltip on a touchscreen."""
    rows = [r for r in hist if r]
    if not rows:
        return []
    n = max(len(r) for r in rows)
    out = []
    for c in range(n):
        vals = [r[c] for r in rows if c < len(r)]
        if not vals:
            continue
        out.append({"core": c, "color": CPU_LINE_COLORS[c % len(CPU_LINE_COLORS)],
                    "now": round(vals[-1]), "avg": round(sum(vals) / len(vals)),
                    "peak": round(max(vals))})
    return out

def _cpu_lines(hist, w=100, h=40):
    """One SVG polyline per core, oldest sample on the left.

    Bars showed the instantaneous value and nothing else — you could not see whether a
    spike was a blip or a climb. Same data, plotted against time, answers that.
    """
    pts = [row for row in hist if row]
    if not pts:
        return []
    ncores = max(len(row) for row in pts)
    if len(pts) == 1:
        pts = pts * 2
    n = len(pts)
    lines = []
    for c in range(ncores):
        coords = []
        for i, row in enumerate(pts):
            v = row[c] if c < len(row) else 0.0
            x = w * i / (n - 1)
            y = h - max(0.0, min(100.0, v)) / 100.0 * h
            coords.append(f"{x:.1f},{y:.1f}")
        lines.append(" ".join(coords))
    return lines

def _spark_points(hist, w=100, h=32, lo=30, hi=85):
    # Map a temperature history to an SVG polyline "x,y x,y ..." over a w×h box.
    pts = list(hist)[-40:]
    if not pts:
        return ""
    if len(pts) == 1:
        pts = pts * 2
    n = len(pts)
    out = []
    for i, t in enumerate(pts):
        x = w * i / (n - 1)
        y = h - max(0.0, min(1.0, (t - lo) / (hi - lo))) * h
        out.append(f"{x:.1f},{y:.1f}")
    return " ".join(out)

def boot_watch():
    """Notice that the box has rebooted, and remember it until you say it was you.

    You cannot detect the SD card being pulled: the card IS the root filesystem, so the
    moment it leaves, the code that would raise the alarm is unreadable. But nobody yanks
    a card from a running Pi — they power it down, copy it, and put it back. That leaves a
    trace: a boot this box can't account for.

    /proc/sys/kernel/random/boot_id is regenerated on every boot, so a change means the
    machine restarted — not merely that a service was restarted.

    Honest limit: anyone who takes the card can also edit this, or clear the flag. It's
    tamper-EVIDENCE for the careless, not tamper-proofing. A sticker across the SD slot is
    a better detector, and costs nothing.
    """
    bid = _try(lambda: _first_line("/proc/sys/kernel/random/boot_id"))
    if not bid:
        return None
    if r.get("last_boot_id") != bid:
        first_ever = r.get("last_boot_id") is None
        r.set("last_boot_id", bid)
        if not first_ever:                       # don't cry wolf on the very first run
            now = time.time()
            r.rpush("boot_events", f"{now:.0f}")
            r.ltrim("boot_events", -50, -1)
            r.set("unacked_boot", f"{now:.0f}")
    return r.get("unacked_boot")

@app.route('/boot-ack', methods=['POST'])
def boot_ack():
    r.delete("unacked_boot")
    return redirect('/health')

UPDATES_STATE = "/var/lib/cooldown-updates.json"
UPDATES_STALE_AFTER = 3 * 3600   # the writer runs hourly; three misses means it stopped


def _when(ts, now=None):
    """'tonight at 03:27' / 'in 40 min' / 'tomorrow at 03:27'. Answers "so when does
    this actually happen", which a bare clock time does not when it is 2am."""
    now = now if now is not None else time.time()
    d = ts - now
    if d < 0:
        return "overdue"
    if d < 5400:
        return f"in {max(1, int(d // 60))} min"
    hhmm = time.strftime("%-I:%M %p", time.localtime(ts)).lower()
    same_day = time.strftime("%Y-%m-%d", time.localtime(ts)) == time.strftime("%Y-%m-%d", time.localtime(now))
    if same_day:
        return f"today at {hhmm}"
    if d < 36 * 3600:
        return f"tonight at {hhmm}" if time.localtime(ts).tm_hour < 6 else f"tomorrow at {hhmm}"
    return f"in {int(d // 86400)} days"


def _short_ago(secs):
    """'3 min ago' / '2 h ago' / '4 days ago'. Answers "is this figure current?" at a
    glance, which is the actual question behind "does it check every day?"."""
    secs = max(0, int(secs))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


def _updates(now=None):
    """Pending-update state, written hourly by deploy/cooldown-updates.sh.

    Read from a file rather than shelling out to apt: `apt-get -s dist-upgrade` takes a
    second or two, and /health is polled every four. A worker blocked on apt is a worker
    not serving the gate.

    Returns fresh=False when the file is missing or stale, which is itself the useful
    signal -- the watchdog that reports on apt's timers can stall the same way apt's
    timers did, and a stale report must not read as a clean one.
    """
    now = now if now is not None else time.time()
    try:
        with open(UPDATES_STATE) as fh:
            d = json.load(fh)
    except Exception:
        return {"fresh": False, "pending": None, "security": None,
                "reboot_required": False, "boot_ok": True, "stuck_jobs": 0,
                "checked_ago": None}
    checked = float(d.get("checked", 0))
    return {"fresh": (now - checked) < UPDATES_STALE_AFTER,
            "pending": int(d.get("pending", 0)),
            "security": int(d.get("security", 0)),
            "reboot_required": bool(d.get("reboot_required")),
            # boot_ok defaults True so an older state file does not read as broken; the
            # writer always emits it now.
            "boot_ok": bool(d.get("boot_ok", True)),
            "stuck_jobs": int(d.get("stuck_jobs", 0)),
            "packages": (d.get("packages") or "").split(),
            "pkg_total": int(d.get("pkg_total", 0)),
            # Formatted here rather than as a Jinja filter: render_page() compiles
            # templates with from_string(), so there is no shared environment to
            # register one on.
            "checked_human": _short_ago(now - checked) if checked else "never",
            # Read from the timer, never hardcoded — see cooldown-updates.sh.
            "next_install": _when(float(d["next_install"]), now) if d.get("next_install") else "",
            "auto_reboot": d.get("auto_reboot", ""),
            "last_result": d.get("last_result", "unknown"),
            "last_run_ago": int(now - float(d["last_run"])) if d.get("last_run") else None,
            "checked_ago": int(now - checked) if checked else None}


AUDIT_STATE = "/var/lib/cooldown-audit.json"
AUDIT_STALE_AFTER = 10 * 86400   # weekly writer; ten days means it stopped


def _audit(now=None):
    """Security-invariant state, written weekly by deploy/cooldown-audit.sh.

    Reports findings rather than repairing them. A watchdog that silently deleted an
    unexpected SSH key would destroy exactly the evidence worth having.
    """
    now = now if now is not None else time.time()
    try:
        with open(AUDIT_STATE) as fh:
            d = json.load(fh)
    except Exception:
        return {"fresh": False, "findings": None, "checked_ago": None,
                "ca_mtime": None, "exposed_ports": "", "journal_persistent": True}
    checked = float(d.get("checked", 0))
    return {"fresh": (now - checked) < AUDIT_STALE_AFTER,
            "findings": int(d.get("findings", 0)),
            "ca_mtime": d.get("ca_mtime") or None,
            "ssh_keys": int(d.get("ssh_keys", 0)),
            "shell_accounts": d.get("shell_accounts", ""),
            "exposed_ports": (d.get("exposed_ports") or "").strip(),
            "tampered_files": int(d.get("tampered_files", 0)),
            "ts_days": int(d.get("ts_days", -1)),
            "ca_days": int(d.get("ca_days", 0)),
            "backup_age": int(d.get("backup_age", -1)),
            "mode": d.get("mode", "quick"),
            # -1 until the weekly full tier has run at least once. Unknown is not a pass.
            "backup_restores": int(d.get("backup_restores", -1)),
            # Read from the live rules by the audit, never hardcoded here. The previous
            # hardcoded set claimed port 22 was firewalled for however long it was not
            # (F22), and went stale again the moment the policy became default-deny.
            "fw_policy": d.get("fw_policy", "unknown"),
            "fw_contained": [int(x) for x in (d.get("fw_contained") or "").split() if x.isdigit()],
            # How long ago the weekly tier last proved it, so a carried-forward result
            # cannot masquerade as fresh indefinitely.
            "full_ago": _short_ago(now - float(d["full_checked"])) if d.get("full_checked") else "",
            "checked_human": _short_ago(now - checked) if checked else "never",
            # Defaults to True so an older state file does not read as an alarm.
            "journal_persistent": bool(d.get("journal_persistent", True)),
            "checked_ago": int(now - checked) if checked else None}


def collect_health(max_age=2.0):
    """Snapshot of the box. Cached briefly: the page polls every 4s and several tabs (or a
    hostile same-origin script) would otherwise each spawn ~5 `systemctl` subprocesses per
    hit. Serving a <=2s-old payload keeps the worker threads free for the gate itself."""
    hit = _HEALTH_CACHE.get("d")
    if hit is not None and time.monotonic() - _HEALTH_CACHE.get("t", 0) < max_age:
        return hit
    svc = _services(["cooldown-app", "cooldown-proxy", "cooldown-redirect", "redis-server", "tailscaled"])
    # Read the latest sample rather than taking one. sample_history() on the scheduler is
    # the ONLY caller of _cpu_stats(), because that function measures the delta between
    # its own calls -- a second caller landing a fraction of a second after the first
    # would diff over a near-zero interval and report noise.
    per_core = list(_CPU_HIST[-1]) if _CPU_HIST else []
    agg = _CPU_LATEST.get("agg", 0.0)
    temp = _TEMP_HIST[-1] if _TEMP_HIST else None
    out = {
        "model": _try(lambda: _first_line("/proc/device-tree/model").replace("\x00", ""), "Raspberry Pi"),
        "cpu": {"pct": agg, "per_core": per_core, "load": _try(_loadavg, [0, 0, 0]),
                "cores": os.cpu_count() or 1, "hist": [list(x) for x in _CPU_HIST]},
        "temp_c": temp,
        "temp_hist": list(_TEMP_HIST),
        "updates": _try(_updates, {"fresh": False, "pending": None, "security": None,
                                   "reboot_required": False, "checked_ago": None}),
        "audit": _try(_audit, {"fresh": False, "findings": None, "checked_ago": None,
                               "exposed_ports": "", "journal_persistent": True}),
        "mem": _try(_mem, {"used_mb": 0, "total_mb": 0, "pct": 0}),
        "disk": _try(_disk, {"used_gb": 0, "total_gb": 0, "avail_gb": 0, "pct": 0}),
        "uptime": _try(_uptime, "?"),
        "net": {n: _iface(n) for n in ("eth0", "tailscale0", "wlan0")},
        "power": _try(_power, {"ok": True}),
        "services": svc,
        "services_ok": all(v == "active" for v in svc.values()),
        "errors": error_summary(),
        "procs": _try(_top_processes, []),
        "ports": _try(_listening_ports, []),
        "enforcement": enforcement_status(),
    }
    _HEALTH_CACHE.update(t=time.monotonic(), d=out)
    return out

HEALTH_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Pi Health · Countdown</title>
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --faint:#5f6773; --go:#3ecf7c; --wait:#f0a63a; --bad:#e5484d;
        }
        *{box-sizing:border-box}
        body{
            margin:0;background:radial-gradient(1100px 560px at 50% -10%,#161a22,var(--bg));
            color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;padding:26px 16px max(26px,env(safe-area-inset-bottom));
            display:flex;justify-content:center;
        }
        .wrap{width:100%;max-width:560px}
        .kicker{display:flex;align-items:center;gap:8px;justify-content:center;font-size:11.5px;
            font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:18px}
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--go)}
        .board{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 10px 6px;margin-bottom:12px}
        .board svg{display:block;width:100%;height:auto}
        .caption{text-align:center;font-size:11.5px;color:var(--faint);margin:2px 0 6px}
        /* board line-art */
        .pcb{fill:#0c1a12;stroke:#2f5d43;stroke-width:1.5}
        .hole{fill:var(--bg);stroke:#2f5d43;stroke-width:1.2}
        .chip{fill:#161b22;stroke:#3a4150;stroke-width:1.2}
        .port{fill:#0f1319;stroke:#3a4150;stroke-width:1.2}
        .port.dark{fill:#090b0e}
        .pin{stroke:#3a4150;stroke-width:2;stroke-dasharray:2 4}
        .lbl{fill:var(--muted);font:600 9px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .lbl.r{text-anchor:end}
        .ctext{fill:#6b7686;font:700 9px ui-monospace,Menlo,monospace;text-anchor:middle}
        .led{fill:#2a2f3a}
        /* ethernet — the star: green when the link is up */
        #eth .jack{fill:#0f1319;stroke:#3a4150;stroke-width:1.5;transition:.4s}
        #eth.on .jack{fill:#123522;stroke:var(--go)}
        #eth.on .lbl{fill:var(--go)}
        #eth.on .led.link{fill:var(--go)}
        #eth.on .led.act{fill:var(--go);animation:blink 1.5s steps(1) infinite}
        @keyframes blink{50%{opacity:.2}}
        /* ethernet unplugged: red + an occasional shake */
        #eth.down .jack{fill:#2e1414;stroke:var(--bad)}
        #eth.down .lbl{fill:var(--bad)} #eth.down .led{fill:var(--bad)}
        #eth.down{animation:shake 1.8s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
        @keyframes shake{0%,86%,100%{transform:translateX(0)}89%{transform:translateX(-2.5px)}92%{transform:translateX(2.5px)}95%{transform:translateX(-1.5px)}}
        @media (prefers-reduced-motion:reduce){ #eth.on .led.act,#eth.down{animation:none} }
        /* SoC tinted by temperature, RAM by memory use */
        #soc,#ram{transition:.5s}
        #soc.cool,#ram.cool{fill:#123522;stroke:var(--go)}
        #soc.warm,#ram.warm{fill:#2a2412;stroke:var(--wait)}
        #soc.hot,#ram.hot{fill:#2e1414;stroke:var(--bad)}
        .divln{stroke:#2a2f3a;stroke-width:1}
        /* power connector */
        #pwr{fill:#3a4150;transition:.4s} #pwr.on{fill:var(--go)} #pwr.bad{fill:var(--wait)}
        /* metric cards */
        .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
        @media (max-width:460px){.grid{grid-template-columns:repeat(2,1fr)}}
        .metric{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 13px 12px}
        .mtop{display:flex;align-items:baseline;justify-content:space-between}
        .mk{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
        .mv{font-size:20px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
        .metric.cool .mv{color:var(--go)} .metric.warm .mv{color:var(--wait)} .metric.hot .mv{color:var(--bad)}
        .mbig{font-size:19px;font-weight:700;margin-top:6px;letter-spacing:-.4px}
        .bar{background:#0e1116;border-radius:6px;height:7px;overflow:hidden;margin-top:9px}
        .bar i{display:block;height:100%;width:0;background:var(--go);border-radius:6px;transition:width .5s,background .5s}
        .metric.warm .bar i{background:var(--wait)} .metric.hot .bar i{background:var(--bad)}
        .msub{font-size:11.5px;color:var(--faint);margin-top:8px}
        .cpul{display:block;width:100%;height:44px;margin:8px 0 5px}
        .cpul .gl{stroke:#232732;stroke-width:.5;vector-effect:non-scaling-stroke}
        .cpul polyline{fill:none;stroke-width:1.6;vector-effect:non-scaling-stroke;
            stroke-linejoin:round;stroke-linecap:round}
        a.cpulink{display:block;text-decoration:none;color:inherit}
    a.cpulink:hover{border-color:var(--accent)}
    a.cpulink:hover .more{opacity:1}
    .more{opacity:.55;white-space:nowrap}
    .upd-bad{color:var(--wait);font-weight:600}
    .cpukey{display:flex;flex-wrap:wrap;gap:3px 9px;font-size:10.5px;color:var(--muted);
            font-variant-numeric:tabular-nums}
        .cpukey span{display:inline-flex;align-items:center;gap:4px}
        .cpukey i{width:8px;height:2.5px;border-radius:2px;flex:none}
        .spark{display:block;width:100%;height:32px;margin-top:9px}
        .spark polyline{stroke:var(--go);stroke-width:2;fill:none;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
        .metric.warm .spark polyline{stroke:var(--wait)} .metric.hot .spark polyline{stroke:var(--bad)}
        .thru{margin-top:11px;font-size:11.5px;color:var(--faint);font-variant-numeric:tabular-nums;display:flex;gap:16px}
        .thru b{color:var(--fg);font-weight:600}
        .net{margin-top:9px;display:flex;flex-direction:column;gap:7px}
        .nrow{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--fg)}
        .nstate,.net .nstate{margin-left:auto;color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
        .ndot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
        .ndot.on{background:var(--go);box-shadow:0 0 0 3px rgba(62,207,124,.15)}
        .ndot.off{background:#3a3f4a}
        .svc{display:flex;flex-wrap:wrap;gap:7px;justify-content:center;margin-top:12px}
        .enfbad{margin-top:12px;padding:11px 13px;border-radius:11px;background:#2a1416;
            border:1px solid #7a1f1f;color:#ffd9d9;font-size:12.5px;line-height:1.5;text-align:center}
        .enfbad b{color:#fff}
        .whatsup{margin-top:14px}
        .whatsup summary{font-size:11.5px;color:var(--faint);cursor:pointer;text-align:center;
            list-style:none;padding:6px}
        .whatsup summary::-webkit-details-marker{display:none}
        .plist{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
        .plist th{color:var(--faint);font-weight:600;font-size:10.5px;text-align:right;
            padding:4px 6px;border-bottom:1px solid var(--line)}
        .plist td{padding:5px 6px;text-align:right;color:var(--muted);
            font-variant-numeric:tabular-nums}
        .plist th:first-child,.plist td:first-child{text-align:left;color:var(--fg)}
        .plist td.wide{color:var(--bad);font-weight:600}
        .plist td.dim{color:var(--faint)}      /* wide, but firewalled by design */
        .phint{font-size:11px;color:var(--faint);line-height:1.5;margin-top:7px}
        /* Status rows. Three centred fine-print paragraphs had turned into a wall nobody
       read; a fixed label column lets the eye scan down it, and the dot carries state
       so severity is visible before any of the words are. */
    .status{margin-top:18px;padding-top:13px;border-top:1px solid var(--line);
            display:flex;flex-direction:column;gap:10px}
    .srow{display:grid;grid-template-columns:7px 78px 1fr;gap:10px;
          align-items:baseline;font-size:12.5px;line-height:1.45;text-align:left}
    .sdot{width:7px;height:7px;border-radius:50%;background:var(--go);
          align-self:center;flex:none}
    .srow.warn .sdot{background:var(--wait)}
    .srow.bad  .sdot{background:var(--bad)}
    .slabel{color:var(--faint);font-size:10.5px;letter-spacing:.06em;
            text-transform:uppercase;font-weight:600}
    .sval{color:var(--muted)}
    .srow.warn .sval{color:#e8cfa4}
    .srow.bad  .sval{color:#f0c9c9}
    .sval b{color:var(--fg);font-weight:600}
    .sdet{grid-column:3;color:var(--faint);font-size:11px;line-height:1.5;
          word-break:break-word;margin-top:1px}
    @media (max-width:420px){ .srow{grid-template-columns:7px 1fr}
                              .slabel{grid-column:2}
                              .sval,.sdet{grid-column:2} }
    .errline{text-align:center;font-size:11.5px;color:var(--faint);margin-top:10px;line-height:1.5}
        .errline b{color:var(--wait)}
        .errlast{display:block;font-size:11px;color:#4a515c;word-break:break-word}
        .pill{font-size:11px;font-weight:600;padding:6px 10px;border-radius:999px;border:1px solid var(--line);
            display:flex;align-items:center;gap:6px;color:var(--muted)}
        .pill::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--faint)}
        .pill.ok{color:#cfe9d8}.pill.ok::before{background:var(--go)}
        .pill.bad{color:#f0c9c9;border-color:#3a2222}.pill.bad::before{background:var(--bad)}
        .bootwarn{background:rgba(46,20,20,.86);border:1px solid var(--bad);border-radius:14px;
            padding:15px 16px;margin-bottom:12px}
        .bw-t{font-size:14.5px;font-weight:700;color:#ffd9d9}
        .bw-p{font-size:12.5px;line-height:1.5;color:#e0b9b9;margin-top:6px}
        .bw-b{margin-top:11px;width:100%;padding:10px;border-radius:9px;border:1px solid #5c2c2c;
            background:transparent;color:#e0b9b9;font-size:13px;cursor:pointer}
        .foot{display:flex;gap:16px;justify-content:center;margin-top:20px}
        .foot a{font-size:12.5px;color:var(--faint);text-decoration:none}
        .foot a.home{color:var(--fg);font-weight:600}   /* the only link off this origin */
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>{{ d.model }}</div>
    {% if boot_alert %}
    <div class="bootwarn">
        <div class="bw-t">⚠️ This box restarted on {{ boot_alert }}</div>
        <div class="bw-p">If that wasn't you — no power cut, no reboot you asked for — then
            someone had physical access, and the SD card holds the certificate your devices
            trust. See <b>RECOVERY.md</b>.</div>
        <form action="/boot-ack" method="post">
            <button class="bw-b" type="submit">That was me — dismiss</button>
        </form>
    </div>
    {% endif %}

    <div class="board">
      <svg viewBox="0 0 380 250" role="img" aria-label="Raspberry Pi board">
        <rect class="pcb" x="18" y="34" width="344" height="182" rx="12"/>
        <circle class="hole" cx="32" cy="48" r="5"/>
        <circle class="hole" cx="32" cy="202" r="5"/>
        <circle class="hole" cx="348" cy="202" r="5"/>
        <!-- 40-pin GPIO header -->
        <rect class="port dark" x="46" y="40" width="250" height="15" rx="2"/>
        <line class="pin" x1="52" y1="44.5" x2="290" y2="44.5"/>
        <line class="pin" x1="52" y1="50.5" x2="290" y2="50.5"/>
        <!-- RAM + SoC -->
        <rect id="ram" class="chip {{ ram_class }}" x="60" y="120" width="52" height="44" rx="3"/>
        <text class="ctext" x="86" y="145">RAM</text>
        <rect id="soc" class="chip {{ tclass }}" x="150" y="108" width="72" height="66" rx="6"/>
        <text class="ctext" x="186" y="138">BCM</text>
        <text class="ctext" x="186" y="150">2711</text>
        <!-- Ethernet (the highlight) -->
        <g id="eth" class="{{ eth_state }}">
          <rect class="jack" x="300" y="52" width="68" height="46" rx="4"/>
          <rect class="port dark" x="307" y="62" width="54" height="28" rx="2"/>
          <circle class="led link" cx="307" cy="57" r="3"/>
          <circle class="led act" cx="361" cy="57" r="3"/>
          <text class="lbl r" x="294" y="70">ETH</text>
          <text class="lbl r" x="294" y="83">1 GbE</text>
        </g>
        <!-- USB (two stacked ports each) -->
        <rect class="port dark" x="300" y="110" width="62" height="28" rx="3"/>
        <line class="divln" x1="301" y1="124" x2="361" y2="124"/>
        <text class="lbl r" x="294" y="128">USB 3.0</text>
        <rect class="port dark" x="300" y="144" width="62" height="28" rx="3"/>
        <line class="divln" x1="301" y1="158" x2="361" y2="158"/>
        <text class="lbl r" x="294" y="162">USB 2.0</text>
        <!-- bottom edge: power, HDMI, AV -->
        <text class="lbl" x="83" y="206">PWR</text>
        <rect id="pwr" class="{{ pwr_class }}" x="70" y="210" width="26" height="11" rx="2"/>
        <text class="lbl" x="146" y="206">HDMI</text>
        <rect class="port dark" x="120" y="210" width="22" height="11" rx="2"/>
        <rect class="port dark" x="150" y="210" width="22" height="11" rx="2"/>
        <rect class="port dark" x="196" y="210" width="16" height="11" rx="2"/>
        <text class="lbl" x="204" y="206">AV</text>
        <!-- microSD (left edge) -->
        <rect class="port dark" x="8" y="150" width="12" height="34" rx="2"/>
        <text class="lbl" x="14" y="145">SD</text>
      </svg>
      <div class="caption">Ethernet glows green when the link is up · SoC tints with temperature</div>
    </div>

    <div class="grid">
      <a class="metric cpulink" id="cpuCard" href="/cpu{{ qs }}">
        <div class="mtop"><span class="mk">CPU</span><span class="mv" id="cpuPct">{{ d.cpu.pct }}%</span></div>
        <svg class="cpul" viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true">
          <line class="gl" x1="0" y1="10" x2="100" y2="10"/>
          <line class="gl" x1="0" y1="20" x2="100" y2="20"/>
          <line class="gl" x1="0" y1="30" x2="100" y2="30"/>
          <g id="cpuLines">{% for pts in cpu_lines %}<polyline points="{{ pts }}" style="stroke:{{ cpu_colors[loop.index0 % cpu_colors|length] }}"/>{% endfor %}</g>
        </svg>
        <!-- The live number beside each swatch is the required second cue: cyan and pink
             sit in the CVD floor band, so colour alone must not carry identity. -->
        <div class="cpukey" id="cpuKey">
          {% for c in d.cpu.per_core %}<span><i style="background:{{ cpu_colors[loop.index0 % cpu_colors|length] }}"></i><b>{{ c|round|int }}%</b></span>{% endfor %}
        </div>
        <div class="msub">load <span id="cpuLoad">{{ '%.2f'|format(d.cpu.load[0]) }}</span> · {{ d.cpu.cores }} cores · last {{ card_min }} min <span class="more">detail &rsaquo;</span></div>
      </a>
      <div class="metric {{ tclass }}" id="tempCard">
        <div class="mtop"><span class="mk">Temp</span><span class="mv" id="temp">{% if d.temp_c is not none %}{{ d.temp_c }}&deg;C{% else %}&mdash;{% endif %}</span></div>
        <svg class="spark" viewBox="0 0 100 32" preserveAspectRatio="none"><polyline id="tempLine" points="{{ spark_points }}"/></svg>
        <div class="msub">throttling <span id="throt">{{ 'none' if d.power.ok else 'ACTIVE' }}</span> &middot; limit 80&deg;C</div>
      </div>
      <div class="metric {{ mcard }}" id="memCard">
        <div class="mtop"><span class="mk">Memory</span><span class="mv" id="memPct">{{ d.mem.pct }}%</span></div>
        <div class="bar"><i id="memBar" style="width:{{ d.mem.pct }}%"></i></div>
        <div class="msub"><span id="memText">{{ d.mem.used_mb }} / {{ d.mem.total_mb }} MB</span></div>
      </div>
      <div class="metric {{ dcard }}" id="diskCard">
        <div class="mtop"><span class="mk">Disk</span><span class="mv" id="diskPct">{{ d.disk.pct }}%</span></div>
        <div class="bar"><i id="diskBar" style="width:{{ d.disk.pct }}%"></i></div>
        <div class="msub"><span id="diskText">{{ d.disk.used_gb }} / {{ d.disk.total_gb }} GB</span></div>
      </div>
      <div class="metric">
        <div class="mtop"><span class="mk">Uptime</span></div>
        <div class="mbig" id="uptime">{{ d.uptime }}</div>
        <div class="msub">since boot</div>
      </div>
      <div class="metric">
        <div class="mtop"><span class="mk">Network</span></div>
        <div class="net">
          <div class="nrow"><span class="ndot {{ 'on' if d.net.eth0 and d.net.eth0.up else 'off' }}" id="ndot_eth0"></span>Ethernet<span class="nstate" id="st_eth0">{% if d.net.eth0 and d.net.eth0.up %}Up{% if d.net.eth0.speed %} · {{ d.net.eth0.speed }}M{% endif %}{% else %}Down{% endif %}</span></div>
          <div class="nrow"><span class="ndot {{ 'on' if d.net.tailscale0 and d.net.tailscale0.up else 'off' }}" id="ndot_tailscale0"></span>Tailscale<span class="nstate" id="st_tailscale0">{% if d.net.tailscale0 and d.net.tailscale0.up %}Up{% else %}Down{% endif %}</span></div>
          <div class="nrow"><span class="ndot {{ 'on' if d.net.wlan0 and d.net.wlan0.up else 'off' }}" id="ndot_wlan0"></span>Wi-Fi<span class="nstate" id="st_wlan0">{% if d.net.wlan0 and d.net.wlan0.up %}Up{% else %}Off{% endif %}</span></div>
        </div>
        <div class="thru">eth0<span id="rx">&darr; &mdash;</span><span id="tx">&uarr; &mdash;</span></div>
      </div>
    </div>

    <div class="svc">
      {% for name, state in d.services.items() %}
      <span class="pill {{ 'ok' if state=='active' else 'bad' }}" id="svc_{{ name }}">{{ name }}</span>
      {% endfor %}
    </div>

    {% if not d.enforcement.ok %}
    <div class="enfbad">&#9888;&#65039; <b>Time is not being charged.</b> Pages are loading on a
      gated site but nothing has been counted for {{ (d.enforcement.charge_ago // 60) if d.enforcement.charge_ago else 'ever' }}m.
      The injected timer is not reporting &mdash; your budget is not draining.</div>
    {% endif %}

    <!-- Collapsed by default: on a good day none of this needs looking at, and the page
         is already dense. One tap when something feels wrong. -->
    <details class="whatsup">
      <summary>What's running</summary>
      <table class="plist">
        <tr><th>Process</th><th>Mem</th><th>CPU</th></tr>
        {% for pr in d.procs %}
        <tr><td>{{ pr.name }}</td><td>{{ pr.mb }} MB</td><td>{{ pr.pct }}%</td></tr>
        {% endfor %}
      </table>
      <div class="phint">Biggest by memory. 172 processes exist; all but a handful are kernel
        threads doing nothing.</div>
      <table class="plist">
        <tr><th>Listening</th><th>Reachable from</th></tr>
        {% for pt in d.ports %}
        <tr><td>{{ pt.port }}{% if pt.what %} · {{ pt.what }}{% endif %}</td>
            <td class="{{ 'wide' if pt.rank == 0 and pt.port not in firewalled else 'dim' if pt.rank == 0 else '' }}">{{ pt.scope }}</td></tr>
        {% endfor %}
      </table>
      {% if not fw_known %}<div class="phint"><span class="upd-bad">Firewall state unknown</span>
        &mdash; the hourly audit has not reported, so nothing below can be called contained.</div>{% endif %}
      <div class="phint"><b>Wide is not automatically wrong here.</b> mitmproxy binds every
        interface and cannot be told otherwise in transparent mode, and sshd does the same —
        both are contained by the interface-scoped firewall rules, which is what F1 actually
        fixed. What <i>would</i> be worth chasing: the dashboard (5000) going wide, since it is
        bound narrowly on purpose, or a port here you do not recognise.</div>
    </details>

    <!-- System status. Was three centred fine-print paragraphs; the information was
         all there and none of it was being read. Same facts, scannable. -->
    <div class="status">

      <!-- Swallowed errors. Silence here used to mean "fine" and "broken" equally. -->
      <div class="srow {{ 'bad' if (d.errors.total or d.errors.proxy) else 'ok' }}">
        <span class="sdot"></span><span class="slabel">Errors</span>
        <span class="sval">
          {%- if d.errors.total or d.errors.proxy -%}
          <b>{{ d.errors.total }}</b> in the app{% if d.errors.proxy %}, <b>{{ d.errors.proxy }}</b> in the proxy{% endif %} since boot
          {%- else -%}No handled errors since boot{%- endif -%}
        </span>
        {%- if d.errors.last %}<span class="sdet">Last: {{ d.errors.last }}</span>{% endif -%}
      </div>

      <!-- Patch state, and crucially HOW it gets applied. Previously answerable only by
           SSHing in, which is why nobody noticed apt's timer had been dead for ten days.
           The schedule is read from the timer, never hardcoded here. -->
      {% set u = d.updates %}
      {% set ujam = u.fresh and (not u.get('boot_ok', True) or u.get('stuck_jobs', 0)) %}
      <div class="srow {{ 'bad' if (ujam or not u.fresh or u.reboot_required) else ('warn' if u.security else ('warn' if u.pending else 'ok')) }}">
        <span class="sdot"></span><span class="slabel">Updates</span>
        <span class="sval">
          {%- if ujam -%}
          Boot never completed &mdash; scheduled jobs are queued and will not run
          {%- elif not u.fresh -%}
          Update status unknown
          {%- elif u.reboot_required -%}
          Reboot required to finish a kernel update
          {%- elif u.pending -%}
          <b>{{ u.pending }}</b> pending{% if u.security %}, <b>{{ u.security }}</b> security{% endif %}
          {%- else -%}
          Up to date{%- endif -%}
        </span>
        <span class="sdet">
          {%- if ujam -%}
          {% if u.get('stuck_jobs', 0) %}{{ u.stuck_jobs }} timer{{ '' if u.stuck_jobs == 1 else 's' }} fired without executing. {% endif %}Counts here are not to be trusted.
          {%- elif not u.fresh -%}
          The hourly check has not reported{% if u.checked_ago %} for {{ (u.checked_ago // 3600) }}h{% endif %}.
          {%- else -%}
          {% if u.pending %}Installs automatically{% if u.next_install %} {{ u.next_install }}{% endif %}{%
            if u.auto_reboot and u.reboot_required %}, reboots {{ u.auto_reboot }}{%
            elif u.auto_reboot %}; reboots {{ u.auto_reboot }} only if a kernel lands{% endif %}.
          {% else %}Checked {{ u.checked_human }}. Next install{% if u.next_install %} {{ u.next_install }}{% endif %}.{% endif %}
          {%- if u.packages %} {{ u.packages|join(', ') }}{%
            if u.pkg_total > u.packages|length %} and {{ u.pkg_total - u.packages|length }} more{% endif %}.{% endif -%}
          {%- if u.last_result and u.last_result != 'success' %} Last run: {{ u.last_result }}.{% endif -%}
          {%- endif -%}
        </span>
      </div>

      <!-- Weekly security invariants. Reports, never repairs. -->
      {% set a = d.audit %}
      <div class="srow {{ 'bad' if (not a.fresh or a.findings) else 'ok' }}">
        <span class="sdot"></span><span class="slabel">Security</span>
        <span class="sval">
          {%- if not a.fresh -%}
          Security audit has not run{% if a.checked_ago %} for {{ (a.checked_ago // 86400) }} days{% endif %}
          {%- elif a.findings -%}
          {{ a.findings }} audit finding{{ '' if a.findings == 1 else 's' }}
          {%- if a.exposed_ports %} &mdash; open to the LAN: {{ a.exposed_ports }}{% endif -%}
          {%- if a.tampered_files %} &mdash; {{ a.tampered_files }} modified packaged file(s){% endif -%}
          {%- else -%}
          Security invariants hold &mdash; CA untouched, {{ a.ssh_keys }} SSH key{{ '' if a.ssh_keys == 1 else 's' }}, no exposed ports
          {%- endif -%}
        </span>
        <span class="sdet">
          {%- if a.findings %}Details: journalctl -t budget-audit
          {%- else -%}
          Checked {{ a.get('checked_human', 'unknown') }}{%
            if a.get('ca_days', 0) %} &middot; CA valid {{ a.ca_days }}d{% endif %}{%
            if a.get('ts_days', -1) > 0 %} &middot; tailnet key {{ a.ts_days }}d{% endif %}
          {%- endif -%}
        </span>
      </div>

      <!-- Backup gets its own row: "it ran" and "it restores" are different claims. -->
      {% if a.get('backup_age', -1) >= 0 or a.get('backup_restores', -1) >= 0 %}
      <div class="srow {{ 'bad' if a.get('backup_restores', -1) == 0 else ('warn' if a.get('backup_age', 0) > 2 else 'ok') }}">
        <span class="sdot"></span><span class="slabel">Backup</span>
        <span class="sval">
          {%- if a.get('backup_restores', -1) == 0 -%}restore FAILED
          {%- elif a.get('backup_age', -1) >= 0 -%}{{ a.backup_age }}d old
          {%- else -%}unknown{%- endif -%}
        </span>
        <span class="sdet">
          {%- if a.get('backup_restores', -1) == 1 %}restore verified{% if a.get('full_ago') %} {{ a.full_ago }}{% endif %} against a scratch database
          {%- elif a.get('backup_restores', -1) == 0 %}the newest backup did not restore &mdash; journalctl -t budget-audit
          {%- else %}restore not yet verified this week{% endif -%}
        </span>
      </div>
      {% endif %}

    </div>

    <div class="foot">{% if back_url %}<a class="home" href="{{ back_url }}">&larr; {{ back_label }}</a>{% endif %}<a href="/stats{{ qs }}">Usage stats</a><a href="/devices{{ qs }}">Devices</a><a href="/health{{ qs }}">Refresh</a></div>
</div>
{% raw %}
<script>
(function(){
    var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
      var FEED=(document.querySelector('meta[name="cd-feed"]')||{}).content||"/budget/feed";
    function $(id){ return document.getElementById(id); }
    function set(id,t){ var e=$(id); if(e) e.textContent=t; }
    function width(id,p){ var e=$(id); if(e) e.style.width=Math.max(0,Math.min(100,p))+"%"; }
    function tclass(t){ return t==null?"off":(t<=65?"cool":(t<=78?"warm":"hot")); }   // 80C = throttle
    function net(o){ if(!o) return "n/a"; if(!o.up) return "Down"; return o.speed?("Up · "+o.speed+"M"):"Up"; }
    function dot(id,on){ var e=$(id); if(e) e.className="ndot "+(on?"on":"off"); }

    function pclass(p){ return p<70?"cool":(p<85?"warm":"hot"); }
    function cardcls(p){ return p<70?"":(p<85?"warm":"hot"); }
    function rate(b){ if(b==null) return "—"; if(b<1024) return b+" B/s"; if(b<1048576) return (b/1024).toFixed(b<10240?1:0)+" KB/s"; return (b/1048576).toFixed(1)+" MB/s"; }
    function spark(hist){
        var pts=(hist||[]).slice(-40); if(!pts.length) return "";
        if(pts.length===1){ pts=[pts[0],pts[0]]; }
        var n=pts.length,lo=30,hi=85,w=100,h=32,out=[];
        for(var i=0;i<n;i++){ var x=w*i/(n-1),y=h-Math.max(0,Math.min(1,(pts[i]-lo)/(hi-lo)))*h; out.push(x.toFixed(1)+","+y.toFixed(1)); }
        return out.join(" ");
    }
    function update(d){
        set("cpuPct", d.cpu.pct+"%"); set("cpuLoad", d.cpu.load[0].toFixed(2));
        var COL=__CPU_COLORS__;
        var g=$("cpuLines"), rows=((d.cpu&&d.cpu.hist)||[]).filter(function(r){return r&&r.length;}).slice(-__CPU_CARD__);
        if(g && rows.length){
            if(rows.length===1) rows=rows.concat(rows);
            var nc=Math.max.apply(null, rows.map(function(r){return r.length;}));
            if(g.children.length!==nc){
                g.innerHTML="";
                for(var c=0;c<nc;c++){
                    var pl=document.createElementNS("http://www.w3.org/2000/svg","polyline");
                    pl.style.stroke=COL[c%COL.length]; g.appendChild(pl);
                }
            }
            for(var c=0;c<nc;c++){
                var pts=[];
                for(var i=0;i<rows.length;i++){
                    var v=Math.max(0,Math.min(100, rows[i][c]||0));
                    pts.push((100*i/(rows.length-1)).toFixed(1)+","+(40-v/100*40).toFixed(1));
                }
                g.children[c].setAttribute("points", pts.join(" "));
            }
        }
        var key=$("cpuKey");
        if(key && d.cpu.per_core){
            if(key.children.length!==d.cpu.per_core.length)
                key.innerHTML=d.cpu.per_core.map(function(_,i){
                    return '<span><i style="background:'+COL[i%COL.length]+'"></i><b></b></span>'; }).join("");
            d.cpu.per_core.forEach(function(c,i){
                var b=key.children[i]&&key.children[i].querySelector("b");
                if(b) b.textContent=Math.round(c)+"%";
            });
        }
        if(d.temp_c!=null){
            set("temp", d.temp_c+"°C");
            var soc=$("soc"); if(soc) soc.setAttribute("class","chip "+tclass(d.temp_c));
            var tc=$("tempCard"); if(tc) tc.className="metric "+tclass(d.temp_c);
        }
        var line=$("tempLine"); if(line && d.temp_hist) line.setAttribute("points", spark(d.temp_hist));
        set("throt", d.power && d.power.ok ? "none" : "ACTIVE");
        set("memPct", d.mem.pct+"%"); width("memBar", d.mem.pct);
        set("memText", d.mem.used_mb+" / "+d.mem.total_mb+" MB");
        var ram=$("ram"); if(ram) ram.setAttribute("class","chip "+pclass(d.mem.pct));
        var mc=$("memCard"); if(mc) mc.className="metric "+cardcls(d.mem.pct);
        set("diskPct", d.disk.pct+"%"); width("diskBar", d.disk.pct);
        set("diskText", d.disk.used_gb+" / "+d.disk.total_gb+" GB");
        var dc=$("diskCard"); if(dc) dc.className="metric "+cardcls(d.disk.pct);
        set("uptime", d.uptime);
        var e=d.net.eth0, ts=d.net.tailscale0, wl=d.net.wlan0;
        var eth=$("eth"); if(eth) eth.setAttribute("class", e&&e.up?"on":(e?"down":"off"));
        dot("ndot_eth0", e&&e.up); set("st_eth0", net(e));
        dot("ndot_tailscale0", ts&&ts.up); set("st_tailscale0", ts&&ts.up?"Up":"Down");
        dot("ndot_wlan0", wl&&wl.up); set("st_wlan0", wl&&wl.up?"Up":"Off");
        if(e){ var rx=$("rx"), tx=$("tx"); if(rx) rx.innerHTML="↓ <b>"+rate(e.rx_bps)+"</b>"; if(tx) tx.innerHTML="↑ <b>"+rate(e.tx_bps)+"</b>"; }
        var pwr=$("pwr"); if(pwr) pwr.setAttribute("class", d.power&&d.power.ok?"on":"bad");
        Object.keys(d.services||{}).forEach(function(s){ var el=$("svc_"+s); if(el) el.className="pill "+(d.services[s]==="active"?"ok":"bad"); });
    }
    function poll(){ fetch("/health?fmt=json&t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){ return r.json(); }).then(update).catch(function(){}); }
    setInterval(poll, 4000);
    document.addEventListener("visibilitychange", function(){ if(!document.hidden) poll(); });
    poll();
})();
</script>
{% endraw %}
</body>
</html>
"""

@app.route('/health')
def health():
    d = collect_health()
    if request.args.get("fmt") == "json":
        return jsonify(d)
    eth = d["net"].get("eth0")
    eth_state = "on" if (eth and eth["up"]) else ("down" if eth else "off")
    mem_pct, disk_pct = d["mem"].get("pct", 0), d["disk"].get("pct", 0)
    # Cards stay neutral until high, then warn — but the RAM chip always glows (cool too).
    card = lambda p: "" if p < 70 else ("warm" if p < 85 else "hot")
    ts = _try(boot_watch)
    boot_alert = time.strftime("%a %-d %b, %-I:%M %p", time.localtime(float(ts))) if ts else None
    return render_page(HEALTH_PAGE,
        d=d, boot_alert=boot_alert,
        cpu_lines=_cpu_lines(d["cpu"].get("hist", [])[-CPU_CARD_SAMPLES:]),
        cpu_colors=CPU_LINE_COLORS,
        card_min=max(1, round(CPU_CARD_SAMPLES * 4 / 60)),
        # Ports the redirect script scopes to tailscale0 + loopback. Wide here is the
        # documented design, not a finding, so it must not render as an alarm.
        # Derived, not asserted. With no audit report we pass an empty set, so wildcard
        # ports render as "wide" rather than being quietly called contained -- erring
        # toward the alarming reading, as every other unknown on this page does.
        firewalled=set(_try(lambda: _audit().get("fw_contained") or [], [])),
        fw_known=bool(_try(lambda: _audit().get("fw_policy") not in (None, "", "unknown"), False)),
        tclass=_temp_class(d["temp_c"]),
        eth_state=eth_state,
        ram_class=_pct_class(mem_pct),
        mcard=card(mem_pct), dcard=card(disk_pct),
        pwr_class="on" if d["power"].get("ok") else "bad",
        spark_points=_spark_points(d["temp_hist"]))

def _acct_counters():
    """Encrypted vs unencrypted bytes seen by the Pi, from the observational TRAFFIC_ACCT
    chain (mangle table): :443 = TLS (encrypted), :80 + :53 = HTTP + DNS (plaintext).

    STRICTLY READ-ONLY. The chain is created by the root-run redirect script at boot, not
    here: an earlier version rebuilt it inline, which meant an unauthenticated GET could
    trigger privileged firewall *writes* and spawn ~10 sudo processes per request. The app
    now only ever runs this one read, which is all `sudoers.d/cooldown` permits."""
    out = subprocess.run(["sudo", "-n", "iptables", "-t", "mangle", "-nvxL", "TRAFFIC_ACCT"],
                         capture_output=True, text=True, timeout=3).stdout
    enc = unenc = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 8 or not parts[1].isdigit():
            continue
        b = int(parts[1])                       # column 2 = bytes
        if "dpt:443" in line:
            enc += b
        elif "dpt:80" in line or "dpt:53" in line:
            unenc += b
    return enc, unenc

@app.route('/feed')
def feed():
    # Encrypted vs unencrypted bytes/sec, driving the packet-feed background (green = TLS,
    # red = the plaintext DNS/HTTP that actually leaves your network in the clear).
    enc, unenc = _try(_acct_counters, (0, 0))
    now, e, u = time.monotonic(), 0, 0
    prev = _FEED_PREV.get("v")
    if prev and now - prev[0] >= 0.3:
        dt = now - prev[0]
        e = max(0, round((enc - prev[1]) / dt))
        u = max(0, round((unenc - prev[2]) / dt))
    _FEED_PREV["v"] = (now, enc, unenc)
    return jsonify({"enc": e, "unenc": u})

# ---------------------------------------------------------------------------
# Devices page: the phone + laptop as the Pi sees them over Tailscale. Everything
# comes from `tailscale status --json` (per-peer online/OS/traffic/last-seen); no
# personal identifiers are baked in — device names are read live.
# ---------------------------------------------------------------------------

_TS_PREV = {}   # tailscale IP -> (monotonic_t, rx, tx) for per-device throughput

def _ts_status():
    out = subprocess.run(["tailscale", "status", "--json"],
                         capture_output=True, text=True, timeout=4).stdout
    return json.loads(out)

def _dev_name(p):
    h = p.get("HostName") or ""
    if h and h.lower() != "localhost":
        return h
    dns = (p.get("DNSName") or "").split(".")[0]
    return dns or (p.get("TailscaleIPs") or ["device"])[0]

def _dev_kind(os_):
    return "phone" if (os_ or "").lower() in ("ios", "android", "ipados") else "computer"

def _fmt_bytes(n):
    if n is None:
        return "—"
    for unit, div in (("GB", 1073741824), ("MB", 1048576), ("KB", 1024)):
        if n >= div:
            return (f"{n/div:.1f} {unit}" if unit != "KB" else f"{n/div:.0f} {unit}")
    return f"{n} B"

def _since(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        s = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return ""
    if s < 0:
        return "just now"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return f"{int(s)}s ago"

def _device(p):
    ip = (p.get("TailscaleIPs") or [""])[0]
    rx, tx = p.get("RxBytes", 0), p.get("TxBytes", 0)     # Pi<-peer, Pi->peer
    down_bps = up_bps = None                               # device-centric: down = Pi->device
    now = time.monotonic()
    prev = _TS_PREV.get(ip)
    if prev and now - prev[0] >= 0.5:
        dt = now - prev[0]
        up_bps = max(0, round((rx - prev[1]) / dt))
        down_bps = max(0, round((tx - prev[2]) / dt))
    _TS_PREV[ip] = (now, rx, tx)
    # Online = control-plane heartbeat; Active = currently passing packets. A directly-
    # connected peer (especially same-LAN) can read Online=false while still Active with
    # traffic flowing over the WireGuard path — so count "actively passing data" as
    # connected too, else it shows the nonsensical "offline, but downloading".
    online = bool(p.get("Online") or p.get("Active"))
    return {
        "name": _dev_name(p), "os": p.get("OS", "?"), "kind": _dev_kind(p.get("OS")),
        "ip": ip, "online": online,
        "direct": bool(p.get("CurAddr")), "relay": p.get("Relay", ""),
        "down_bytes": tx, "up_bytes": rx, "down_bps": down_bps, "up_bps": up_bps,
        "down_h": _fmt_bytes(tx), "up_h": _fmt_bytes(rx),
        "last_seen": "online" if online else (_since(p.get("LastSeen", "")) or "offline"),
    }

def collect_devices():
    st = _try(_ts_status)
    if not st:
        return {"ok": False, "self": {}, "devices": []}
    devices = [_device(p) for p in st.get("Peer", {}).values()]
    devices.sort(key=lambda d: (d["kind"] != "phone", not d["online"], d["name"].lower()))
    self_ = st.get("Self", {})
    return {"ok": True,
            "self": {"name": _dev_name(self_), "ip": (self_.get("TailscaleIPs") or [""])[0]},
            "devices": devices}

DEVICES_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Devices · Countdown</title>
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --faint:#5f6773; --go:#3ecf7c; --wait:#f0a63a; --bad:#e5484d; --sleep:#7aa2ff;
        }
        *{box-sizing:border-box}
        body{margin:0;background:radial-gradient(1100px 560px at 50% -10%,#161a22,var(--bg));
            color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;padding:26px 16px max(26px,env(safe-area-inset-bottom));display:flex;justify-content:center}
        .wrap{width:100%;max-width:560px}
        .kicker{display:flex;align-items:center;gap:8px;justify-content:center;font-size:11.5px;
            font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:18px}
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--go)}
        .board{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 10px 8px;margin-bottom:12px}
        .board svg{display:block;width:100%;height:auto}
        .caption{text-align:center;font-size:11.5px;color:var(--faint);margin:4px 0 4px}
        /* line-art nodes */
        .dev .body{fill:#0f1319;stroke:#3a4150;stroke-width:1.5;transition:.4s}
        .dev.on .body{fill:#101c15;stroke:var(--go)}
        .dev .scr{fill:#0a0c10;stroke:#2a2f3a;stroke-width:1}
        .dev.on .scr{stroke:#1f5137}
        .dev .lbl{fill:var(--muted);font:600 10px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .dev.on .lbl{fill:var(--go)}
        .dev .sub{fill:var(--faint);font:600 8.5px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .hub{fill:#0c1a12;stroke:#2f5d43;stroke-width:1.5}
        .hubp{fill:#123522;stroke:var(--go);stroke-width:1.4}
        .hubl{fill:var(--go);font:700 9px ui-monospace,Menlo,monospace;text-anchor:middle}
        .gwl{fill:var(--faint);font:600 9px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .lane{stroke:#3a3f4a;stroke-width:1.6;fill:none;transition:stroke .4s,stroke-width .35s}
        .lane.dn.on{stroke:var(--go)}
        .lane.up.on{stroke:var(--sleep)}
        .lane.dn.flow{stroke-dasharray:5 7;animation:flowdn .9s linear infinite}
        .lane.up.flow{stroke-dasharray:5 7;animation:flowup .9s linear infinite}
        @keyframes flowdn{to{stroke-dashoffset:-12}}
        @keyframes flowup{to{stroke-dashoffset:12}}
        @media (prefers-reduced-motion:reduce){.lane.flow{animation:none}}
        /* cards */
        .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
        @media (max-width:460px){.grid{grid-template-columns:1fr}}
        .dcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
        .dhead{display:flex;align-items:center;gap:9px}
        .dico{width:26px;height:26px;flex:none;color:var(--muted)}
        .dcard.on .dico{color:var(--go)}
        .dname{font-size:15px;font-weight:700;letter-spacing:-.2px;word-break:break-word}
        .dos{font-size:11px;color:var(--faint);margin-top:1px;text-transform:uppercase;letter-spacing:.04em}
        .drow{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--fg);margin-top:11px}
        .drow .k{color:var(--faint)}.drow .v{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--muted)}
        .sdot{width:8px;height:8px;border-radius:50%;background:#3a3f4a;flex:none}
        .dcard.on .sdot{background:var(--go);box-shadow:0 0 0 3px rgba(62,207,124,.15)}
        .thru{margin-top:11px;padding-top:11px;border-top:1px solid var(--line);font-size:11.5px;
            color:var(--faint);font-variant-numeric:tabular-nums;display:flex;gap:16px}
        .thru b{color:var(--fg);font-weight:600}
        .empty{color:var(--faint);font-size:13px;text-align:center;padding:20px}
        .foot{display:flex;gap:16px;justify-content:center;margin-top:20px}
        .foot a{font-size:12.5px;color:var(--faint);text-decoration:none}
        .foot a.home{color:var(--fg);font-weight:600}   /* the only link off this origin */
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>Your devices · via {{ d.self.name or 'Pi' }}</div>

    <div class="board">
      <svg viewBox="0 0 380 250" role="img" aria-label="Devices connected to the Pi">
        <!-- two lanes per link: green flows DOWN to a device (its downloads), blue flows UP to the Pi -->
        <path id="ph-dn" class="lane dn {{ 'on' if phone and phone.online else '' }}" d="M176 72 L82 116"/>
        <path id="ph-up" class="lane up {{ 'on' if phone and phone.online else '' }}" d="M183 74 L89 118"/>
        <path id="lp-dn" class="lane dn {{ 'on' if laptop and laptop.online else '' }}" d="M204 72 L286 120"/>
        <path id="lp-up" class="lane up {{ 'on' if laptop and laptop.online else '' }}" d="M197 74 L279 122"/>
        <!-- Pi hub (top) -->
        <rect class="hub" x="150" y="20" width="80" height="52" rx="9"/>
        <circle class="hubp" cx="180" cy="34" r="2.5"/><circle class="hubp" cx="190" cy="34" r="2.5"/><circle class="hubp" cx="200" cy="34" r="2.5"/>
        <text class="hubl" x="190" y="52">{{ (d.self.name or 'pi')[:9] }}</text>
        <text class="gwl" x="190" y="65">gateway</text>
        <!-- phone (bottom-left) -->
        <g class="dev {{ 'on' if phone and phone.online else '' }}" id="dev-phone">
          <rect class="body" x="56" y="120" width="48" height="86" rx="11"/>
          <rect class="scr" x="62" y="132" width="36" height="58" rx="3"/>
          <rect x="72" y="125" width="16" height="3" rx="1.5" fill="#2a2f3a"/>
          <rect x="72" y="196" width="16" height="3" rx="1.5" fill="#2a2f3a"/>
          <text class="lbl" x="80" y="224">{% if phone %}{{ phone.name[:15] }}{% else %}no phone{% endif %}</text>
          <text class="sub" x="80" y="236" id="sub-phone">{% if phone %}{{ phone.last_seen }}{% else %}not connected{% endif %}</text>
        </g>
        <!-- laptop (bottom-right) -->
        <g class="dev {{ 'on' if laptop and laptop.online else '' }}" id="dev-laptop">
          <rect class="body" x="250" y="124" width="72" height="50" rx="4"/>
          <rect class="scr" x="256" y="130" width="60" height="38" rx="2"/>
          <path class="body" d="M244 174 H328 L334 186 H238 Z"/>
          <text class="lbl" x="286" y="206">{% if laptop %}{{ laptop.name[:16] }}{% else %}no laptop{% endif %}</text>
          <text class="sub" x="286" y="218" id="sub-laptop">{% if laptop %}{{ laptop.last_seen }}{% else %}not connected{% endif %}</text>
        </g>
      </svg>
      <div class="caption"><span style="color:var(--go)">&#9660; green</span> = download to a device &nbsp;·&nbsp; <span style="color:var(--sleep)">&#9650; blue</span> = upload to the Pi &nbsp;·&nbsp; dashes flow when data moves</div>
    </div>

    <div class="grid" id="cards">
      {% for x in d.devices %}
      <div class="dcard {{ 'on' if x.online else '' }}" data-ip="{{ x.ip }}">
        <div class="dhead">
          <svg class="dico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
            {% if x.kind == 'phone' %}<rect x="7" y="2" width="10" height="20" rx="2.5"/><line x1="10" y1="18.5" x2="14" y2="18.5"/>
            {% else %}<rect x="3" y="4" width="18" height="12" rx="1.5"/><line x1="1" y1="20" x2="23" y2="20"/>{% endif %}
          </svg>
          <div><div class="dname">{{ x.name }}</div><div class="dos">{{ x.os }}</div></div>
        </div>
        <div class="drow"><span class="sdot"></span><span class="k">Status</span><span class="v">{{ 'Online' if x.online else x.last_seen }}</span></div>
        <div class="drow"><span class="k">Link</span><span class="v">{{ 'Direct' if x.direct else ('Relay · ' + x.relay if x.relay else 'Relay') }}</span></div>
        <div class="drow"><span class="k">Through Pi</span><span class="v" data-total>&darr; {{ x.down_h }} &nbsp; &uarr; {{ x.up_h }}</span></div>
        <div class="thru"><span data-rate>&darr; <b>&mdash;</b></span><span data-rate2>&uarr; <b>&mdash;</b></span></div>
      </div>
      {% else %}
      <div class="empty">No devices on the tailnet right now.</div>
      {% endfor %}
    </div>

    <div class="foot">{% if back_url %}<a class="home" href="{{ back_url }}">&larr; {{ back_label }}</a>{% endif %}<a href="/health{{ qs }}">Pi health</a><a href="/stats{{ qs }}">Usage stats</a><a href="/devices{{ qs }}">Refresh</a></div>
</div>
{% raw %}
<script>
(function(){
    var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
      var FEED=(document.querySelector('meta[name="cd-feed"]')||{}).content||"/budget/feed";
    function fmtBytes(n){ if(n==null) return "—"; if(n<1024) return n+" B"; if(n<1048576) return (n/1024).toFixed(0)+" KB"; if(n<1073741824) return (n/1048576).toFixed(1)+" MB"; return (n/1073741824).toFixed(2)+" GB"; }
    function fmtRate(b){ if(b==null) return "—"; if(b<1024) return b+" B/s"; if(b<1048576) return (b/1024).toFixed(b<10240?1:0)+" KB/s"; return (b/1048576).toFixed(1)+" MB/s"; }
    function node(id, sub, dev){
        var g=document.getElementById(id); if(!g) return;
        g.setAttribute("class", "dev "+(dev&&dev.online?"on":""));
        var s=document.getElementById(sub); if(s&&dev) s.textContent=dev.last_seen;
    }
    function laneW(bps){ if(!bps||bps<=0) return 1.6; return Math.max(1.6, Math.min(5.5, 1.6+1.0*Math.log(bps/400)/Math.LN10)); }
    function lanes(prefix, dev){
        var on=dev&&dev.online;
        var dn=document.getElementById(prefix+"-dn"), up=document.getElementById(prefix+"-up");
        if(dn){ dn.setAttribute("class","lane dn"+(on?" on":"")+(on&&dev.down_bps>0?" flow":"")); dn.style.strokeWidth=laneW(on?dev.down_bps:0)+"px"; }
        if(up){ up.setAttribute("class","lane up"+(on?" on":"")+(on&&dev.up_bps>0?" flow":"")); up.style.strokeWidth=laneW(on?dev.up_bps:0)+"px"; }
    }
    function card(x){
        var el=document.querySelector('.dcard[data-ip="'+x.ip+'"]'); if(!el) return;
        el.className="dcard "+(x.online?"on":"");
        var st=el.querySelectorAll(".drow .v");
        if(st[0]) st[0].textContent = x.online?"Online":x.last_seen;
        if(st[1]) st[1].textContent = x.direct?"Direct":(x.relay?("Relay · "+x.relay):"Relay");
        var tot=el.querySelector("[data-total]"); if(tot) tot.innerHTML="↓ "+fmtBytes(x.down_bytes)+" &nbsp; ↑ "+fmtBytes(x.up_bytes);
        var r1=el.querySelector("[data-rate]"); if(r1) r1.innerHTML="↓ <b>"+(x.online?fmtRate(x.down_bps):"—")+"</b>";
        var r2=el.querySelector("[data-rate2]"); if(r2) r2.innerHTML="↑ <b>"+(x.online?fmtRate(x.up_bps):"—")+"</b>";
    }
    function update(d){
        var devs=d.devices||[];
        var phone=devs.find(function(x){return x.kind==="phone";});
        var laptop=devs.find(function(x){return x.kind==="computer";});
        node("dev-phone","sub-phone",phone); lanes("ph",phone);
        node("dev-laptop","sub-laptop",laptop); lanes("lp",laptop);
        devs.forEach(card);
    }
    function poll(){ fetch("/devices?fmt=json&t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){return r.json();}).then(update).catch(function(){}); }
    setInterval(poll, 4000);
    document.addEventListener("visibilitychange", function(){ if(!document.hidden) poll(); });
    poll();
})();
</script>
{% endraw %}
</body>
</html>
"""

# --- Shared ambient background: give EVERY page the gate's "encrypted traffic" porthole,
# and frost the panels so the drifting hex softly shows through instead of vanishing behind
# them. The animation is defined once (in BUDGET_PAGE) and reused, so there's no drift. ---
BG_CANVAS = '<canvas id="bp-bg" aria-hidden="true"></canvas>'
# Defined once, above, and shared by reference. This used to re-read it back OUT of
# BUDGET_PAGE with a regex over this module's own source, picking whichever {% raw %}
# block happened to mention "bp-bg" — which would have silently selected the wrong block
# the first time a second one mentioned it, and silently selected NOTHING (raising at
# import) if the markup were reformatted.
BG_SCRIPT = BG_BLOCK
BG_STYLE = ("html,body{background:#070b0e!important}"
            "#bp-bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;display:block}"
            ".wrap{position:relative;z-index:1}"
            ".card,.tile,.board,.metric,.dcard{background:rgba(18,21,27,0.4)!important;"
            "-webkit-backdrop-filter:blur(5px);backdrop-filter:blur(5px)}")

def _add_theme(page):
    """One extra :root block per page, appended after the page's own styles."""
    return page.replace("</head>",
        '<style id="cd-theme">{{ theme_css }}</style></head>', 1)

def _add_bg(page, full=True, token="{{ ui_tok }}", feed="/feed"):
    # `feed` differs by origin: the gate lives on the gated site and reaches the app
    # through the proxy at /budget/feed; the dashboard is served by the box itself, where
    # the route is just /feed. The background script reads it from the meta tag so one
    # copy of that script works on both.
    page = page.replace("</head>",
        # The tint iOS paints above the page. Hardcoded, it left Christmas repainting the
        # whole gate and then sitting under a cold grey bar — the one bit of chrome a
        # phone user looks at most and the only bit the theme could not reach.
        '<meta name="theme-color" content="{{ theme_bg }}">'
        f'<meta name="cd-feed" content="{feed}">'
        f'<meta name="cd-tok" content="{token}"></head>', 1)    # dark iOS bars + poll token
    page = page.replace("</style>", BG_STYLE + "</style>", 1)   # frost + canvas layer + z-index
    if full:                                                    # pages that don't already have the canvas
        page = page.replace("<body>", "<body>\n" + BG_CANVAS, 1)
        page = page.replace("</body>", BG_SCRIPT + "\n</body>", 1)
    return page

# /digest was added later and never went through this, so it was the one dashboard page
# with no ambient background and no frosted panels — visibly a different product.
BUDGET_PAGE, STATS_PAGE, HEALTH_PAGE, DEVICES_PAGE, DIGEST_PAGE, WRAPPED_PAGE = (
    _add_theme(x) for x in
    (BUDGET_PAGE, STATS_PAGE, HEALTH_PAGE, DEVICES_PAGE, DIGEST_PAGE, WRAPPED_PAGE))

DIGEST_PAGE = _add_bg(DIGEST_PAGE)
WRAPPED_PAGE = _add_bg(WRAPPED_PAGE)
STATS_PAGE = _add_bg(STATS_PAGE)
HEALTH_PAGE = (HEALTH_PAGE.replace("__CPU_COLORS__", json.dumps(CPU_LINE_COLORS))
                          .replace("__CPU_CARD__", str(CPU_CARD_SAMPLES)))
CPU_PAGE = _add_bg(_add_theme(CPU_PAGE.replace("__CPU_COLORS__", json.dumps(CPU_LINE_COLORS))))
HEALTH_PAGE = _add_bg(HEALTH_PAGE)
DEVICES_PAGE = _add_bg(DEVICES_PAGE)
# The gate is script-readable from the gated site (it replaces that site's page), so it
# gets the narrow feed-only token — never ui_tok. See feed_token().
BUDGET_PAGE = _add_bg(BUDGET_PAGE, full=False, token="{{ feed_tok }}", feed="/budget/feed")

@app.route('/devices')
def devices():
    d = collect_devices()
    if request.args.get("fmt") == "json":
        return jsonify(d)
    phone = next((x for x in d["devices"] if x["kind"] == "phone"), None)
    laptop = next((x for x in d["devices"] if x["kind"] == "computer"), None)
    return render_page(DEVICES_PAGE, d=d, phone=phone, laptop=laptop)

if __name__ == '__main__':
    # Production WSGI server (waitress) instead of the Werkzeug dev server: more
    # robust for 24/7 operation, no dev-server warning.
    from waitress import serve

    # Close a reset the cron job missed while the box was off. Deliberately here and not
    # at module scope: this DELETES budget state, and `import app` happens in tests, in
    # backup tooling and in a REPL — none of which should reset your day.
    try:
        catch_up_reset()
    except Exception as e:
        print(f"[RESET] catch-up failed: {e}")

    # Loopback is the critical listener — the addon reaches the gate there, so the
    # gate must never depend on the tailnet being up. The tailnet address is added as
    # a SECOND listener purely so the dashboard is reachable from your phone.
    #
    # tailscaled may still be coming up when this starts, so wait briefly for the
    # interface to get an address. If it never does we serve loopback only: the gate
    # keeps working and the gate page simply omits the dashboard links.
    # COOLDOWN_LISTEN overrides the whole calculation, for environments with no
    # tailscale0 to find. In Docker that's "0.0.0.0:5000" — safe there only because the
    # compose file publishes it as 127.0.0.1:5000:5000, so the container boundary plays
    # the part the firewall plays on the Pi. Don't set it on a bare host.
    override = os.environ.get("COOLDOWN_LISTEN")
    ts_ip = None
    if not override:
        for _ in range(20):
            ts_ip = _iface_ipv4(TAILNET_IFACE)
            if ts_ip:
                break
            time.sleep(1)

    listen = [override] if override else [f"127.0.0.1:{MONITOR_PORT}"]
    if override:
        print(f"[MONITOR] listening on {override} (COOLDOWN_LISTEN)")
    elif ts_ip:
        listen.append(f"{ts_ip}:{MONITOR_PORT}")
        print(f"[MONITOR] dashboard on http://{ts_ip}:{MONITOR_PORT} ({TAILNET_IFACE})")
    else:
        print(f"[MONITOR] no address on {TAILNET_IFACE} — loopback only; "
              f"the gate works, the dashboard links are hidden")

    # Anniversary anchor. Backfilled from the oldest day of usage history so an existing
    # box gets its real first day rather than the day this shipped.
    def _seed_first_run():
        if r.get("first_run"):
            return
        days = sorted(k.split(":")[1] for k in r.scan_iter(match="usage:*", count=500)
                      if len(k.split(":")) == 3 and k.split(":")[1][:2] == "20")
        r.set("first_run", days[0] if days else time.strftime("%Y-%m-%d"))
        print(f"[THEME] first run recorded as {r.get('first_run')}")
    _try(_seed_first_run)

    # Publish the origin so addon.py can redirect the old /budget/* dashboard URLs to
    # wherever the pages actually live, instead of just 404ing someone's bookmark.
    _try(lambda: r.set("monitor_origin", monitor_origin(ts_ip)))

    serve(app, listen=" ".join(listen), threads=12)
