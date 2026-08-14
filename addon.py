from mitmproxy import http
from urllib.parse import urlsplit, parse_qs, quote
import json
import secrets
import time
import os
import sys
import redis
import requests as req

# mitmdump loads this file by path; make sure its own directory is importable so the
# shared news_domains list resolves regardless of the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from news_domains import NEWS_DOMAINS

# Each gated site: substrings that identify its hosts, and the canonical host the
# proxy should rewrite Flask redirects back to. Keep this aligned with SITES in
# app.py — same site names, since all Redis state is keyed by them.
SITES = {
    "reddit":  {"match": ["reddit.com"]},
    "youtube": {"match": ["youtube.com"]},
    # Spotify WEB PLAYER only (open.spotify.com). Narrow on purpose: the api/auth/
    # streaming hosts (api.spotify.com, spclient…, *.scdn.co) are left untouched so
    # playback + login keep working; the gate lands on the open.spotify.com page.
    "spotify": {"match": ["open.spotify.com"]},
    "puzzmo":  {"match": ["puzzmo.com"]},
    # News category — the whole NEWS_DOMAINS list gates to one shared-bucket "news"
    # site. Gets the plain heartbeat (charges time); no YouTube declutter / SW-kill.
    "news":    {"match": NEWS_DOMAINS},
}

# Hosts that belong to a gated site but only serve static assets / media. We let
# these through untouched so we don't choke on (or gate) images and video streams.
# Matched by SUFFIX, not substring — see host_matches(). A substring test here was the
# same bug F4 fixed in site_for_host, left behind in the exemption list: "redd.it" is
# contained in "redd.it.evil.example", and an exemption that over-matches un-gates a
# site rather than over-gating one, which is the direction that actually costs you.
IGNORED_HOSTS = [
    "redditmedia.com", "redditstatic.com", "redd.it",
    "ytimg.com", "ggpht.com", "googlevideo.com",
    # Puzzmo's API + asset subdomains match the "puzzmo.com" gate substring, so let
    # them through untouched — only the www/apex page should get the gate.
    "api.puzzmo.com", "cdn.puzzmo.com",
]

# YouTube "study mode" allowlist — must match STUDY_PLAYLISTS in app.py. A study
# session is free but LOCKED to these playlists: only /watch and /playlist URLs
# carrying an allowlisted list= are permitted; everything else (search, home feed,
# Shorts, other channels) bounces back to the course.
STUDY_PLAYLISTS = []   # OFF by default; must match app.py. See the note there.

r = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)

# Injected into real pages of a gated site. Pings the budget server only while the
# tab is actually visible, so only foreground viewing time is charged. The site is
# baked in at injection time (__SITE__) so the server charges the right budget. A
# 403 means the budget is spent / cooldown started -> reload to land on the gate.
# The heartbeat response carries `remaining` seconds; when it drops to WARN_AT or
# fewer, a flashing bar counts down the last minute so the cutoff isn't a surprise.
HEARTBEAT_SCRIPT = """
<script>
(function () {
  var INTERVAL = 10000;   // ms between pings while the tab is visible
  var WARN_AT  = 60;      // flash the warning when this many seconds (or fewer) remain
  var SITE = "__SITE__";
  var LABEL = SITE.charAt(0).toUpperCase() + SITE.slice(1);
  var deadline = null;    // ms timestamp when time runs out (re-anchored each ping)
  var curPhase = null;    // "day" | "winddown" | "night" from the last ping

  var bar = null;
  function ensureBar() {
    if (bar) return bar;
    var css = document.createElement("style");
    css.textContent =
      '#bp-timewarn{position:fixed;top:0;left:0;right:0;z-index:2147483647;pointer-events:none;' +
      'font:600 15px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;' +
      'color:#fff;text-align:center;padding:10px 16px;padding-top:max(10px,env(safe-area-inset-top));' +
      'background:#c0392b;box-shadow:0 2px 12px rgba(0,0,0,.45);letter-spacing:.2px;' +
      'animation:bp-flash 1s steps(1) infinite;}' +
      '@keyframes bp-flash{50%{background:#e74c3c}}' +
      '@media (prefers-reduced-motion:reduce){#bp-timewarn{animation:none;background:#c0392b}}';
    (document.head || document.documentElement).appendChild(css);
    bar = document.createElement("div");
    bar.id = "bp-timewarn";
    bar.setAttribute("role", "status");
    document.documentElement.appendChild(bar);
    return bar;
  }
  function showWarn(secs) {
    var el = ensureBar();
    el.style.display = "block";
    el.textContent = "\\u23F1\\uFE0F " + secs + "s left on " + LABEL + " \\u2014 wrap it up";
  }
  function hideWarn() { if (bar) bar.style.display = "none"; }

  // A calmer, persistent ribbon while the pool is in wind-down, so the shrinking cap
  // isn't a surprise. Yields the top slot to the red last-minute warning when that fires.
  var wdBar = null;
  function ensureWdBar() {
    if (wdBar) return wdBar;
    var css = document.createElement("style");
    css.textContent =
      '#bp-winddown{position:fixed;top:0;left:0;right:0;z-index:2147483646;pointer-events:none;' +
      'font:600 14px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;' +
      'color:#fff;text-align:center;padding:8px 16px;padding-top:max(8px,env(safe-area-inset-top));' +
      'background:#b9770e;box-shadow:0 2px 10px rgba(0,0,0,.35);letter-spacing:.2px;}';
    (document.head || document.documentElement).appendChild(css);
    wdBar = document.createElement("div");
    wdBar.id = "bp-winddown";
    wdBar.setAttribute("role", "status");
    wdBar.textContent = "\\u23F3 Wind-down \\u2014 your time is tapering toward bedtime";
    document.documentElement.appendChild(wdBar);
    return wdBar;
  }
  function showWd() { ensureWdBar().style.display = "block"; }
  function hideWd() { if (wdBar) wdBar.style.display = "none"; }

  // Local 1s ticker: the red last-minute warning takes the top slot; otherwise the
  // wind-down ribbon shows whenever the last ping said we're in wind-down.
  setInterval(function () {
    var warn = false;
    if (deadline !== null) {
      var secs = Math.round((deadline - Date.now()) / 1000);
      if (secs > 0 && secs <= WARN_AT) { showWarn(secs); warn = true; }
    }
    if (!warn) hideWarn();
    if (!warn && curPhase === "winddown") showWd(); else hideWd();
  }, 1000);

  function ping() {
    if (document.visibilityState !== "visible") return;
    fetch("/budget/heartbeat?site=" + SITE + "&_=" + Date.now(), { method: "POST", cache: "no-store", keepalive: true })
      .then(function (res) {
        if (res.status === 403) { window.location.reload(); return null; }
        return res.json();
      })
      .then(function (data) {
        if (data && typeof data.remaining === "number") {
          deadline = Date.now() + data.remaining * 1000;   // re-anchor to the server's truth
        }
        if (data && data.phase) curPhase = data.phase;
      })
      .catch(function () {});
  }
  setInterval(ping, INTERVAL);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") ping();
  });
  ping();   // fire once on load so the warning shows promptly if you re-enter near the cap
})();
</script>
"""

# Injected on YouTube/Reddit: kill the service worker. On SPA sites (especially mobile
# YouTube) the SW serves pages from its own cache — bypassing our injection — and can
# intercept the heartbeat fetch, so budget time never gets charged. Unregister any SW,
# block re-registration, and clear its caches so everything goes through the network.
SW_KILL = """
<script>
(function () {
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      navigator.serviceWorker.getRegistrations().then(function (rs) {
        rs.forEach(function (r) { r.unregister(); });
      }).catch(function () {});
      try { navigator.serviceWorker.register = function () { return Promise.reject(new Error("sw disabled")); }; } catch (e) {}
    }
    if (window.caches && caches.keys) {
      caches.keys().then(function (ks) { ks.forEach(function (k) { caches.delete(k); }); }).catch(function () {});
    }
  } catch (e) {}
})();
</script>
"""

# Injected on YouTube (in addition to the heartbeat) to kill the trance vectors:
# Shorts, the homepage recommendation feed, and autoplay — while leaving search and
# Subscriptions intact. CSS handles the layout (and re-applies to SPA-rendered nodes
# automatically); the JS handles the URL-based Shorts redirect, which is the most
# layout-independent part and keeps working even when YouTube reshuffles its DOM.
# NOTE: these selectors track YouTube's current markup and may need refreshing over
# time; the /shorts redirect is the durable backbone. :has() needs Safari 15.4+.
YOUTUBE_DECLUTTER = """
<style id="bp-yt-declutter">
/* Shorts shelves on home / subscriptions / search (desktop ytd- + mobile ytm-) */
ytd-reel-shelf-renderer,
ytd-rich-shelf-renderer[is-shorts],
ytm-reel-shelf-renderer,
ytm-rich-shelf-renderer[is-shorts],
grid-shelf-view-model { display: none !important; }

/* Shorts entry points in the side guide / mini guide (desktop) */
ytd-guide-entry-renderer:has(a[title="Shorts"]),
ytd-mini-guide-entry-renderer:has(a[title="Shorts"]),
ytd-guide-entry-renderer:has(a[href^="/shorts"]),
ytd-mini-guide-entry-renderer:has(a[href^="/shorts"]) { display: none !important; }

/* Shorts tab in the mobile bottom pivot bar */
ytm-pivot-bar-item-renderer:has(a[href^="/shorts"]),
ytm-pivot-bar-item-renderer:has(.pivot-shorts) { display: none !important; }

/* Homepage recommendation feed — scoped to home only; search & subs untouched */
ytd-browse[page-subtype="home"] ytd-rich-grid-renderer,
ytm-browse[page-subtype="home"] ytm-rich-grid-renderer { display: none !important; }

/* Watch page: autoplay toggle + the recommended / "up next" rabbit hole */
.ytp-autonav-toggle-button-container,
ytd-watch-next-secondary-results-renderer { display: none !important; }
</style>
<script>
(function () {
  // Layout-independent Shorts killer: rewrite the swipe-feed Short into a normal
  // single video on /watch, which has no infinite swipe. Runs across YouTube's
  // SPA navigations, not just full page loads.
  function deShort() {
    var m = location.pathname.match(/^\\/shorts\\/([^/?#]+)/);
    if (m) location.replace("/watch?v=" + m[1]);
  }
  ["pushState", "replaceState"].forEach(function (fn) {
    var orig = history[fn];
    history[fn] = function () { var r = orig.apply(this, arguments); deShort(); return r; };
  });
  window.addEventListener("popstate", deShort);
  setInterval(deShort, 1000);  // backstop for navigations we didn't intercept
  deShort();

  // Gentle nudge in place of the hidden home feed.
  function nudge() {
    if (location.pathname !== "/" || document.getElementById("bp-yt-nudge")) return;
    var anchor = document.querySelector("ytd-rich-grid-renderer, ytm-rich-grid-renderer");
    if (!anchor || !anchor.parentNode) return;
    var d = document.createElement("div");
    d.id = "bp-yt-nudge";
    d.textContent = "Home feed hidden \\u2014 search or open Subscriptions for what you came for.";
    d.style.cssText = "padding:24px;margin:16px;border-radius:8px;background:#222;color:#bbb;font-family:sans-serif;text-align:center;font-size:15px";
    anchor.parentNode.insertBefore(d, anchor);
  }
  setInterval(nudge, 1000);
})();
</script>
"""

# Injected during a YouTube study session (on top of the heartbeat + declutter).
# The proxy bounces off-course *full navigations*, but most YouTube navigation is
# client-side (SPA) and never reaches the proxy — so this JS enforces the same
# playlist allowlist on in-page navigation, and hides the search box to remove the
# temptation. __PLAYLISTS__ is replaced with the allowlist at injection time.
STUDY_LOCK = """
<style id="bp-yt-studylock">
#search, #search-form, ytd-searchbox, .ytSearchboxComponentHost,
ytm-searchbox, .searchbox { display: none !important; }
#bp-yt-exit {
  position: fixed; top: 10px; right: 10px; z-index: 99999; border: none;
  background: #3ea6ff; color: #0a0a0a; padding: 8px 12px; border-radius: 6px;
  font-family: sans-serif; font-size: 13px; font-weight: 600; cursor: pointer;
}
</style>
<script>
(function () {
  var ALLOWED = __PLAYLISTS__;
  var HOME = "/playlist?list=" + ALLOWED[0];
  var exiting = false;
  function allowed() {
    var p = location.pathname;
    if (p !== "/watch" && p !== "/playlist") return false;
    var list = new URLSearchParams(location.search).get("list");
    return !!list && ALLOWED.indexOf(list) !== -1;
  }
  function ensureExitButton() {
    if (!document.body || document.getElementById("bp-yt-exit")) return;
    var b = document.createElement("button");
    b.id = "bp-yt-exit";
    b.textContent = "Exit study mode";
    // Full navigation (not SPA) to the exit endpoint, which clears the session
    // and bounces to the gate. The flag stops enforce() racing the navigation.
    b.onclick = function () { exiting = true; window.location.assign("/budget/exit?site=youtube"); };
    document.body.appendChild(b);
  }
  function enforce() {
    if (exiting || location.pathname.indexOf("/budget") === 0) return;
    if (!allowed()) location.replace(HOME);
  }
  ["pushState", "replaceState"].forEach(function (fn) {
    var orig = history[fn];
    history[fn] = function () { var r = orig.apply(this, arguments); enforce(); return r; };
  });
  window.addEventListener("popstate", enforce);
  setInterval(function () { enforce(); ensureExitButton(); }, 500);
  enforce(); ensureExitButton();
})();
</script>
"""

FACEBOOK_OVERLAY = """
<script>
(function () {
  "use strict";
  if (window.__cdFbBlock) return; window.__cdFbBlock = true;

  // Allow-list model: block EVERY Facebook page except the few the roommate search needs,
  // so profiles, Watch, search and any Messenger→profile hop are covered without having to
  // recognise each doomscroll surface. Login/checkpoint and logged-out pages are never touched.
  var ALLOW = ["/marketplace", "/groups", "/messages", "/notifications",
               "/settings", "/friends", "/bookmarks", "/me"];

  function selfId() { var m = document.cookie.match(/(?:^|;)\\s*c_user=(\\d+)/); return m ? m[1] : null; }
  function loggedIn() { return !!selfId() || !!document.querySelector('[role="tablist"],[role="banner"]'); }
  function authSurface() {
    if (document.querySelector('input[type="password"]')) return true;   // login / re-auth / checkpoint
    return /^\\/(login|checkpoint|recover|reg|two_step|two_factor|security|help|privacy|policies|terms|legal|confirmemail|device)/.test(location.pathname);
  }
  function allowed() {
    var p = location.pathname;
    for (var i = 0; i < ALLOW.length; i++) { if (p === ALLOW[i] || p.indexOf(ALLOW[i] + "/") === 0) return true; }
    if (p === "/profile.php") {                    // your own profile only, never anyone else's
      var id = new URLSearchParams(location.search).get("id"), s = selfId();
      return !id || (s && id === s);
    }
    return false;
  }
  function shouldBlock() {
    if (authSurface() || !loggedIn()) return false;   // never wall login / logged-out
    return !allowed();                                 // home + everything off the allow-list
  }

  function ensureStyle() {
    if (document.getElementById("cd-fb-style")) return;
    var s = document.createElement("style");
    s.id = "cd-fb-style";
    s.textContent = [
      // Desktop: clip the feed column and block interaction under the cover.
      ".cd-fb-lock{position:relative!important;max-height:calc(100vh - 60px)!important;overflow:hidden!important;}",
      ".cd-fb-lock > *:not(#cd-fb-cover){pointer-events:none!important;}",
      // Mobile: freeze the page so the feed can't scroll.
      "html.cd-fb-noscroll, html.cd-fb-noscroll > body { overflow:hidden !important; }",
      // The cover (solid, theme-aware). Desktop = absolute inside the column; mobile = fixed.
      "#cd-fb-cover{z-index:2147483000;display:flex;flex-direction:column;align-items:center;",
      "justify-content:center;gap:8px;padding:24px;text-align:center;background:#f0f2f5;}",
      "#cd-fb-cover.desk{position:absolute;inset:0;}",
      "#cd-fb-cover.mob{position:fixed;left:0;right:0;}",
      "#cd-fb-cover .cd-t{margin:0;font:700 22px/1.25 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1c1e21;}",
      "#cd-fb-cover .cd-p{margin:0;font:400 14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#65676b;max-width:320px;}",
      "@media (prefers-color-scheme:dark){#cd-fb-cover{background:#18191a;}#cd-fb-cover .cd-t{color:#e4e6eb;}#cd-fb-cover .cd-p{color:#b0b3b8;}}"
    ].join("");
    (document.head || document.documentElement).appendChild(s);
  }

  function coverIn(parent, cls) {
    var c = document.getElementById("cd-fb-cover");
    if (c && c.parentNode !== parent) { c.remove(); c = null; }
    if (!c) {
      c = document.createElement("div");
      c.id = "cd-fb-cover";
      var t = document.createElement("div"); t.className = "cd-t"; t.textContent = "This page is off";
      var p = document.createElement("div"); p.className = "cd-p";
      p.textContent = "Facebook here is Marketplace, Groups, Messages and your own profile — the feed and the rest stay closed.";
      c.appendChild(t); c.appendChild(p);
      parent.appendChild(c);
    }
    c.className = cls;
    return c;
  }

  function lock() {
    ensureStyle();
    var tablist = document.querySelector('[role="tablist"]');
    var main = document.querySelector('[role="main"]');
    var mobile = window.innerWidth < 900;

    if (mobile && tablist) {
      // MOBILE: leave the tab bar exposed (Marketplace/Groups/Messages), cover the rest.
      if (!document.documentElement.classList.contains("cd-fb-noscroll")) window.scrollTo(0, 0);
      var r = tablist.getBoundingClientRect();
      var c = coverIn(document.documentElement, "mob");
      if (r.top < window.innerHeight / 2) {          // tab bar at top → cover below it
        c.style.top = Math.max(0, r.bottom) + "px"; c.style.bottom = "0";
      } else {                                        // tab bar at bottom → cover above it
        c.style.top = "0"; c.style.bottom = (window.innerHeight - r.top) + "px";
      }
      document.documentElement.classList.add("cd-fb-noscroll");
      if (main) main.classList.remove("cd-fb-lock");
    } else if (!mobile && main) {
      // DESKTOP: cover just the content column; the top bar + rail stay live to navigate.
      document.documentElement.classList.remove("cd-fb-noscroll");
      main.classList.add("cd-fb-lock");
      coverIn(main, "desk");
    } else {
      // No nav to preserve (mobile without a tab bar, or a bare page) → cover it all.
      if (main) main.classList.remove("cd-fb-lock");
      document.documentElement.classList.add("cd-fb-noscroll");
      var f = coverIn(document.documentElement, "mob");
      f.style.top = "0"; f.style.bottom = "0";
    }
  }

  function unlock() {
    var c = document.getElementById("cd-fb-cover");
    if (c) c.remove();
    document.documentElement.classList.remove("cd-fb-noscroll");
    var m = document.querySelector('[role="main"]');
    if (m) m.classList.remove("cd-fb-lock");
  }

  function tick() { if (shouldBlock()) lock(); else unlock(); }

  ["pushState", "replaceState"].forEach(function (fn) {
    var o = history[fn];
    history[fn] = function () { var r = o.apply(this, arguments); setTimeout(tick, 60); return r; };
  });
  window.addEventListener("popstate", tick);
  window.addEventListener("resize", function () { if (shouldBlock()) lock(); });
  document.addEventListener("DOMContentLoaded", tick);
  setInterval(tick, 700);
  tick();
})();
</script>
"""

# Overlay-inject sites: decrypted for INJECTION ONLY (no budget, no gate) — cover the
# Facebook home feed with a frosted "tap to reveal" panel. Facebook only survives the
# proxy with HTTP/2 enabled (--set http2=true in the unit); over HTTP/1.1 its bootstrap
# hangs. Only the HTML doc is buffered/CSP-stripped; the rest streams (realtime traffic).
# Match ONLY Facebook's web-page hosts, never bare facebook.com. Messenger's realtime
# hosts (edge-chat/graph/gateway.facebook.com) pin their cert; decrypting them breaks the
# Messenger app. They stay tunneled (out of --allow-hosts) — we only inject the HTML doc.
OVERLAY_SITES = {
    "facebook": {
        "match": ["www.facebook.com", "web.facebook.com", "m.facebook.com", "mbasic.facebook.com"],
        "inject": FACEBOOK_OVERLAY,
    },
}

def host_matches(host, patterns):
    """True if `host` is one of `patterns` or a subdomain of one. The single place
    host matching is defined, so no caller can quietly reintroduce a substring test."""
    host = (host or "").rsplit(":", 1)[0].lower()          # drop any :port
    return any(host == m or host.endswith("." + m) for m in patterns)

def overlay_for_host(host):
    """An overlay-inject site (Facebook) for this host, else None."""
    for cfg in OVERLAY_SITES.values():
        if host_matches(host, cfg["match"]):
            return cfg
    return None

def site_for_host(host):
    # Suffix match on the registrable domain, NOT a substring: "reddit.com" must
    # match reddit.com and *.reddit.com, but never evil-reddit.com or
    # reddit.com.attacker.io (which a substring check would gate — and decrypt).
    for site, cfg in SITES.items():
        if host_matches(host, cfg["match"]):
            return site
    return None

# The switch compares the two meters against each other rather than guessing from page
# loads. The heartbeat and the passive shadow meter measure the same quantity by
# independent means; only one of them can be blocked from inside the page, so a large
# disagreement identifies which. Seven days of paired data put them at 1.03x of each
# other (see shadow_comparison), so the threshold below sits nowhere near normal noise.
ENFORCEMENT_WINDOW_HOURS = 2          # how far back to compare
ENFORCEMENT_MIN_PASSIVE  = 5 * 60     # need this much observed use before judging at all
ENFORCEMENT_RATIO        = 0.4        # heartbeat below this share of passive = dead
ENFORCEMENT_NAV_WINDOW   = 5 * 60     # still recorded, for the /health "browsing" flag


def _try_redis(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _note_navigation(now):
    """Record this page load. Informational only now — the switch no longer reads it.

    nav_burst_start used to live here, to stop the old switch warning on the first page
    of every session. The ratio test below cannot have that bug: at the start of a
    session both meters read zero, and zero is not a disagreement. The workaround went
    away with the design that needed it.
    """
    r.set("last_active_nav", now)


def _meter_totals(now, hours=ENFORCEMENT_WINDOW_HOURS):
    """(heartbeat_seconds, passive_seconds) over the last `hours` whole clock hours.

    Two MGETs rather than one round trip per key: this runs on every HTML response from a
    gated site, and the old version's two GETs should not become forty.
    """
    keys_h, keys_s = [], []
    for i in range(hours):
        t = time.localtime(now - i * 3600)
        day, hour = time.strftime("%Y-%m-%d", t), time.strftime("%H", t)
        for site in SITES:
            keys_h.append(f"usage_hour:{day}:{hour}:{site}")
            keys_s.append(f"shadow_hour:{day}:{hour}:{site}")

    def total(keys):
        return sum(float(v) for v in (r.mget(keys) or []) if v)

    return total(keys_h), total(keys_s)


def enforcement_looks_dead():
    """True when the passive meter sees real use that the heartbeat is not charging for.

    The failure this catches is the one the injected script cannot report, because the
    injected script is what is broken: a CSP that blocks it (F20), a service worker that
    swallows the fetch, a network path that eats the POST. In every case the sites keep
    working, the budget stops draining, and nothing anywhere says so.

    The passive meter is a poor stopwatch — it cannot see tab visibility, and its
    per-hour agreement with the heartbeat scatters 0.72x-1.28x — but that does not matter
    here. Nothing is charged from it and nothing is blocked by it. It is only ever asked
    whether the other clock has stopped, and for that a loose threshold is plenty.

    Returns False whenever it cannot tell, including when the shadow meter is absent.
    Deleting the shadow meter therefore does not break this; it makes it blind.
    """
    now = time.time()
    totals = _try_redis(lambda: _meter_totals(now))
    if totals is None:
        return False
    hb, sh = totals
    if sh < ENFORCEMENT_MIN_PASSIVE:
        return False        # too little observed use to draw any conclusion
    return hb < sh * ENFORCEMENT_RATIO


# Deliberately script-free: inline styles, no JS. The condition it reports is *the
# injected JavaScript not running*, so a warning that needs JavaScript would be silent in
# exactly the case it exists for. A <div> with a style attribute survives a CSP that
# blocks scripts, because the CSP we amend is the site's own and we only added a nonce.
ENFORCEMENT_WARNING = (
    '<div role="alert" style="position:fixed;top:0;left:0;right:0;z-index:2147483647;'
    'background:#7a1f1f;color:#fff;padding:10px 16px;'
    'padding-top:max(10px,env(safe-area-inset-top));text-align:center;'
    'font:600 14px/1.4 -apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Arial,sans-serif;'
    'box-shadow:0 2px 12px rgba(0,0,0,.45)">'
    '\u26A0\uFE0F Cooldown isn\'t counting this time \u2014 the timer script isn\'t reporting. '
    'Your budget is not draining. Check the box.</div>'
)


# ---------------------------------------------------------------------------
# SHADOW METER — an experiment, running in parallel with the injected heartbeat.
#
# The heartbeat is the only reason foreign JavaScript runs on Reddit, YouTube and the
# rest, and that injection is the single largest item in this project's threat model
# (see "code running inside other websites" in SECURITY.md). This measures the same
# thing *passively*: the sites already talk to their own servers constantly while you
# use them, and every one of those requests passes through this proxy. No injection
# needed to watch them.
#
# It is deliberately site-agnostic — no hardcoded telemetry endpoints, because those
# change without warning and would fail silently, which is the direction that has bitten
# this project twice. Any request to a gated host counts as a sign of life, and time is
# accrued between consecutive signs using the same gap cap the heartbeat uses, so the two
# numbers are measured the same way and can be compared directly.
#
# Known bias, and the reason this runs in shadow rather than in production: a background
# tab, an autoplaying video and a prefetching SPA all keep making requests when you are
# not looking. The heartbeat excludes those by design. So the shadow number should read
# HIGH, and *how much* higher is exactly what a week of parallel running is for.
#
# Nothing here can affect the budget: it writes only shadow_usage:* keys, and every
# Redis call is wrapped so a measurement can never break the proxy.
# ---------------------------------------------------------------------------
SHADOW_MAX_GAP = 30.0        # same cap as the heartbeat: bigger gaps are idle, not use
SHADOW_FLUSH_AFTER = 10.0    # accrue in memory, write once per 10 charged seconds
_SHADOW = {}                 # site -> {"last": epoch, "pending": secs, "day": "Y-m-d"}


def _shadow_day(now):
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _shadow_flush(site, st):
    if st["pending"] <= 0:
        return
    def write():
        k = f"shadow_usage:{st['day']}:{site}"
        r.incrbyfloat(k, st["pending"])
        r.expire(k, 100 * 86400)
        # Hourly too — the heartbeat records the same way, and only hour-aligned buckets
        # let the two be compared over the exact window both were running.
        hk = f"shadow_hour:{st['day']}:{time.strftime('%H')}:{site}"
        r.incrbyfloat(hk, st["pending"])
        r.expire(hk, 14 * 86400)
        r.setnx("shadow_started", f"{time.time():.0f}")
    if _try_redis(write, "failed") != "failed":
        st["pending"] = 0.0


def shadow_note(site, now):
    """Record one sign of life for `site` and accrue the gap since the previous one.

    Called on every request to a gated host. Kept in process memory and flushed in
    batches, because this is the proxy's hot path and a Redis round-trip per request
    would be a real cost — the thing this whole area of the code keeps getting wrong is
    making the request path expensive (F12).
    """
    st = _SHADOW.setdefault(site, {"last": 0.0, "pending": 0.0, "day": _shadow_day(now)})
    day = _shadow_day(now)
    if day != st["day"]:                       # midnight: bank yesterday before rolling
        _shadow_flush(site, st)
        st["day"], st["last"] = day, 0.0
    if st["last"]:
        gap = now - st["last"]
        if 0 < gap <= SHADOW_MAX_GAP:
            st["pending"] += gap
    st["last"] = now
    if st["pending"] >= SHADOW_FLUSH_AFTER:
        _shadow_flush(site, st)


def _note_error(where, exc):
    """Publish swallowed proxy errors so /health can show them.

    The proxy runs as its own service, its stdout goes to the journal, and nothing ever
    reads it — so an exception here was strictly invisible. This is the counterpart to
    app.py's _ERRORS: a failure on either side of the loopback call now shows up in the
    same place. Never raises; a broken error reporter must not break the proxy.
    """
    try:
        r.incr("proxy_errors")
        r.setex("proxy_last_error", 7 * 86400,
                f"{int(__import__('time').time())} {where}: {type(exc).__name__}: {exc}"[:200])
    except Exception:
        pass
    print(f"[ERROR] {where}: {exc}")


def session_mode(site):
    """Return the active session's mode ('active' or 'study'), or None if there's
    no live session for this site."""
    token = r.get(f"active_token:{site}")
    if not token:
        return None
    return r.get(f"session:{token}")  # None if the session key has expired

# Every Flask route the proxy is willing to forward to, as it appears under /budget.
# A closed set, because the alternative is string-concatenating an attacker-controlled
# path onto "http://127.0.0.1:5000" — see the userinfo note in request().
#
# These are the endpoints that have to live on the GATED site's origin, and only those:
# the gate replaces the site's own page, the session endpoints are driven from it, and
# /feed backs the gate's animated background. Everything data-rich has moved off.
STATE_CHANGING = ("/enter", "/study", "/exit", "/heartbeat", "/reflect", "/worth")
BUDGET_ENDPOINTS = frozenset(("", "/feed") + STATE_CHANGING)

# Moved to the box's own origin — see the long note in app.py. A script on a gated site
# is now cross-origin with these, so the browser refuses to hand it the response; that
# replaces the header checks that could never quite cover window.open. Listed here only
# so an old bookmark gets a redirect instead of a bare 404.
MOVED_TO_BOX = ("/", "/stats", "/health", "/devices", "/digest", "/wrapped", "/cpu",
                "/remaining", "/boot-ack")


def _forwarded_headers(flow):
    """The few request headers Flask actually needs. The body is forwarded so form
    fields (the reflection prompt's `trigger`) reach the app; everything else is
    deliberately dropped so nothing from the page can influence the internal call."""
    ct = flow.request.headers.get("Content-Type")
    return {"Content-Type": ct} if ct else {}


def study_url_allowed(path):
    """True only for /watch and /playlist URLs carrying an allowlisted playlist."""
    parts = urlsplit(path)
    if parts.path not in ("/watch", "/playlist"):
        return False
    lists = parse_qs(parts.query).get("list", [])
    return any(l in STUDY_PLAYLISTS for l in lists)

class BudgetAddon:
    @staticmethod
    def _feed_token_ok(token: str) -> bool:
        """/feed is the one endpoint still served on the gated origin that returns
        anything at all, so it keeps a token check.

        The gate carries `feed_token` and a script on the gated site can read the gate,
        so this is a speed bump rather than a secret — which is fine, because /feed is
        two aggregate bytes-per-second numbers and nothing else. Everything that was
        worth protecting now lives on the box's origin, where the browser does the
        enforcing. `ui_token` is still accepted so the dashboard's own poller works.
        """
        if not token:
            return False
        return any(v and secrets.compare_digest(token, v)
                   for v in (r.get("feed_token"), r.get("ui_token")))

    @staticmethod
    def _csp_with_nonce(policy: str, nonce: str) -> str:
        """Return `policy` amended so our nonced script may run — or unchanged if it
        already could. Everything else in the policy is preserved.

        The subtlety: a browser IGNORES 'unsafe-inline' as soon as a nonce or hash is
        present. So blindly adding one to a policy that relies on 'unsafe-inline' would
        switch off the site's OWN inline scripts and break the page. Hence:

          · nothing constrains script elements at all -> untouched
          · effective directive allows 'unsafe-inline' with no nonce/hash -> our inline
            script is already permitted; untouched (and adding a nonce would break theirs)
          · otherwise -> add 'nonce-...' to the effective directive, synthesising
            script-src from default-src if the policy only had the fallback

        Which directive is "effective" matters. A browser resolves an inline <script>
        ELEMENT against script-src-elem first, then script-src, then default-src — so a
        site that sets `script-src-elem 'self'` alongside `script-src 'unsafe-inline'`
        blocks our script while this function, looking only at script-src, concludes it
        is already allowed and leaves the policy alone. The failure is silent and it
        fails OPEN: no heartbeat means no time charged, while browsing continues. Same
        class as F12, reached a different way.
        """
        directives = []
        for part in policy.split(";"):
            part = part.strip()
            if part:
                bits = part.split()
                directives.append([bits[0].lower(), bits[1:]])
        names = [d[0] for d in directives]

        target = next((n for n in ("script-src-elem", "script-src", "default-src")
                       if n in names), None)
        if target is None:
            return policy                                   # nothing constrains scripts
        values = directives[names.index(target)][1]
        lowered = [v.lower() for v in values]
        # 'strict-dynamic' belongs in this test, and its absence was a silent fail-open.
        # CSP3 6.7.3.2 ("Does a source list allow all inline behavior for type?"): when the
        # type is script and the list contains 'strict-dynamic', the algorithm returns "Does
        # Not Allow" BEFORE it ever reaches the 'unsafe-inline' branch. The spec spells out
        # the exact case -- "'unsafe-inline' 'strict-dynamic'" is listed as a source list
        # that does not allow inline script. So a policy of
        #     script-src 'unsafe-inline' 'strict-dynamic'
        # was read here as "our script already runs" and returned unchanged, and the
        # heartbeat would be blocked with nothing reporting it: no heartbeat means no time
        # charged while browsing continues. F20's class, one step further out.
        #
        # Adding a nonce is the right repair rather than a workaround: 8.2 states that
        # under 'strict-dynamic' the host, scheme, 'self' and 'unsafe-inline' expressions
        # are ignored for script while nonce and hash expressions are HONOURED. So the
        # nonce is precisely what still works, and it cannot break the site's own scripts
        # here -- 'unsafe-inline' was already being ignored for them too.
        #
        # Checked against the CSP3 spec text, not from memory: the review that raised this
        # flagged its own reasoning as unverified, and the fix is one token in a list,
        # which is exactly the kind of change that gets made before the diagnosis is.
        already = any(v.startswith("'nonce-") or v.startswith("'sha") for v in lowered)
        inline_ignored = "'strict-dynamic'" in lowered
        if "'unsafe-inline'" in lowered and not already and not inline_ignored:
            return policy                                   # our script already runs

        src = f"'nonce-{nonce}'"
        if target == "default-src":
            # Only default-src existed: give scripts their own directive so we don't
            # loosen styles, images or anything else that was falling back to it.
            directives.append(["script-src", values + [src]])
        else:
            # script-src-elem or script-src — amend whichever the browser will actually
            # consult for our <script> element.
            directives[names.index(target)][1].append(src)
        return "; ".join(" ".join([name] + vals) for name, vals in directives)

    def _csp_nonce(self, flow: http.HTTPFlow) -> str:
        nonce = flow.metadata.get("cooldown_nonce")
        if not nonce:
            nonce = secrets.token_urlsafe(16)
            flow.metadata["cooldown_nonce"] = nonce
        return nonce

    def _strip_csp(self, flow: http.HTTPFlow):
        """Drop the site's Content-Security-Policy — but ONLY on the HTML documents we
        actually inject into.
        CSP is what would normally stop our heartbeat script running, so on those
        documents it has to go. Everywhere else (JSON, JS, CSS, images) it is left alone:
        stripping it there bought nothing, because we never inject into those responses,
        and it needlessly widened the window in which an XSS bug *on the site* could
        matter. Note this only ever removes CSP — HSTS, X-Frame-Options,
        X-Content-Type-Options and Referrer-Policy are never touched.
        """
        if "text/html" not in flow.response.headers.get("content-type", ""):
            return
        policies = flow.response.headers.get_all("content-security-policy")
        if policies:
            nonce = self._csp_nonce(flow)
            amended = [self._csp_with_nonce(p, nonce) for p in policies]
            flow.response.headers.set_all("content-security-policy", amended)
        # Report-only doesn't block anything, but it WOULD post a violation report about
        # our injected script back to the site. Drop it rather than tell them.
        if "content-security-policy-report-only" in flow.response.headers:
            del flow.response.headers["content-security-policy-report-only"]

    def responseheaders(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        if site_for_host(host):
            flow.response.stream = False
            self._strip_csp(flow)
        elif overlay_for_host(host):
            # Facebook: buffer ONLY the HTML document we inject into; STREAM everything
            # else (its realtime / long-poll / WebSocket traffic) so buffering a
            # never-ending response can't hang the page.
            if "text/html" in flow.response.headers.get("content-type", ""):
                flow.response.stream = False
                self._strip_csp(flow)
            else:
                flow.response.stream = True

    def request(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        path = flow.request.path

        # Serve budget pages from any gated host under its /budget path. The query
        # string (which carries ?site=) is preserved so Flask charges the right site.
        # Match the path EXACTLY (not startswith): "/budgeting" is the site's own page
        # and must fall through to the normal gate logic, not into this handler.
        parts = urlsplit(path)
        if (parts.path == "/budget" or parts.path.startswith("/budget/")) and site_for_host(host):
            sub = parts.path[len("/budget"):]          # "" | "/heartbeat" | "/enter"
            # The dashboard moved to the box's own origin. Redirect rather than 404 so a
            # bookmark still works — and note the redirect leaks nothing: following it
            # lands the browser cross-origin, where no CORS headers means a script gets
            # an unreadable response and a window it cannot look into.
            if sub in MOVED_TO_BOX:
                origin = r.get("monitor_origin") or ""
                # Carry the query string across. Without it "?from=reddit" is dropped and
                # the page you land on has no way back, and "?fmt=json" silently returns
                # HTML to something expecting JSON.
                target = f"{origin}{sub}" + (f"?{parts.query}" if parts.query else "")
                flow.response = (
                    http.Response.make(302, b"", {"Location": target,
                                                  "Cache-Control": "no-store"})
                    if origin else
                    http.Response.make(404, b"the dashboard now lives on the box itself",
                                       {"Content-Type": "text/plain; charset=utf-8"}))
                return
            # Only ever forward to a known endpoint. Anything else is refused HERE rather
            # than concatenated onto the base URL: "/budget@evil.com/" would otherwise
            # build "http://127.0.0.1:5000@evil.com/", where "127.0.0.1:5000" is parsed as
            # USERINFO and the fetch goes to evil.com — whose HTML we would then hand back
            # on the gated site's own origin (script execution as reddit.com), and which
            # also turns this into an SSRF probe of the box's own network.
            if sub not in BUDGET_ENDPOINTS:
                flow.response = http.Response.make(
                    404, b"no such budget endpoint",
                    {"Content-Type": "text/plain; charset=utf-8"})
                return

            # CSRF: the mutating endpoints (/enter, /study, /exit, /heartbeat, ...) are
            # driven same-origin from the gate page / injected script. A forged request
            # from another site the user is visiting is "cross-site" — reject it so a
            # malicious page can't drive the budget state (or the return redirect).
            # Covers GET too: /exit is reachable by GET (the study-mode exit button is a
            # plain navigation), so a cross-site <img src=".../budget/exit"> would
            # otherwise end the session.
            if (flow.request.method != "GET" or sub in STATE_CHANGING) and \
               flow.request.headers.get("Sec-Fetch-Site") == "cross-site":
                flow.response = http.Response.make(
                    403, b"cross-site request blocked",
                    {"Content-Type": "text/plain; charset=utf-8"})
                return

            # /feed is polled by a script (the gate's background), so it can't require a
            # navigation — it takes the token instead. A real top-level navigation is
            # also allowed so you can eyeball it by hand; "navigation" means
            # Sec-Fetch-Dest: document and ONLY that, because a same-origin <iframe>
            # sends Mode: navigate too and its parent can read contentDocument.
            if sub == "/feed":
                navigation = flow.request.headers.get("Sec-Fetch-Dest", "") == "document"
                token = parse_qs(parts.query).get("t", [""])[0]
                if not navigation and not self._feed_token_ok(token):
                    flow.response = http.Response.make(
                        403, b"not available to page scripts",
                        {"Content-Type": "text/plain; charset=utf-8"})
                    return

            flask_path = sub if sub else "/budget"
            if parts.query:
                flask_path += "?" + parts.query

            try:
                if flow.request.method == "POST":
                    resp = req.post(f"http://127.0.0.1:5000{flask_path}",
                                    data=flow.request.get_content() or None,
                                    headers=_forwarded_headers(flow),
                                    timeout=2, allow_redirects=False)
                else:
                    resp = req.get(f"http://127.0.0.1:5000{flask_path}", timeout=2, allow_redirects=False)

                if resp.status_code == 302:
                    location = resp.headers.get("Location", f"https://{host}/budget")
                    # Rewrite Flask's internal address back to the real site the
                    # browser is on, so relative redirects (e.g. /budget) land right.
                    location = location.replace("http://127.0.0.1:5000", f"https://{host}")
                    flow.response = http.Response.make(302, b"", {"Location": location})
                else:
                    # Keep Flask's own content type (jsonify says application/json) and
                    # forbid sniffing/framing: these pages must never be rendered as HTML
                    # by mistake, nor read out of an iframe by the gated site's scripts.
                    flow.response = http.Response.make(
                        resp.status_code,
                        resp.content,
                        {"Content-Type": resp.headers.get("Content-Type",
                                                          "text/html; charset=utf-8"),
                         "X-Content-Type-Options": "nosniff",
                         "X-Frame-Options": "DENY",
                         "Content-Security-Policy": "frame-ancestors 'none'",
                         "Referrer-Policy": "no-referrer",
                         "Cache-Control": "no-store"}
                    )
            except Exception as e:
                _note_error("budget_handler", e)
                flow.response = http.Response.make(500, b"Budget server error")
            return

        if host_matches(host, IGNORED_HOSTS):
            return

        site = site_for_host(host)
        if not site:
            return

        # SHADOW METER (experiment, started 2026-08-03 — see shadow_note). Purely
        # observational: it never reads or writes budget state, and removing the two
        # lines below removes the feature entirely.
        shadow_note(site, time.time())

        user_agent = flow.request.headers.get("User-Agent", "")
        is_regular_profile = "regular-profile" in user_agent

        # Regular (desktop) profile — flat block.
        if is_regular_profile:
            flow.response = http.Response.make(
                200,
                b"""<html><body style='font-family:sans-serif;text-align:center;margin-top:20vh;background:#1a1a1a;color:white'>
                <h1>Blocked </h1>
                <p>Use your budgeted profile if you really need it.</p>
                </body></html>""",
                {"Content-Type": "text/html"}
            )
            return

        mode = session_mode(site)
        fetch_mode = flow.request.headers.get("Sec-Fetch-Mode", "")
        fetch_dest = flow.request.headers.get("Sec-Fetch-Dest", "")
        is_navigation = fetch_mode == "navigate" or fetch_dest == "document"

        # Study mode: free, but locked to the course playlist. Off-course full
        # navigations bounce back to the playlist; allowed navs + all sub-requests
        # (the API calls that load the video) pass through so the page works.
        if mode == "study":
            if is_navigation and not study_url_allowed(path):
                flow.response = http.Response.make(
                    302, b"",
                    {"Location": f"https://{host}/playlist?list={STUDY_PLAYLISTS[0]}"}
                )
            return

        # Normal budget session: there's budget left AND a recently *visible* tab.
        # The injected heartbeat keeps the session alive and charges time; we just
        # let traffic through here. Background/idle traffic is free.
        if mode == "active":
            # Dead-man's switch, half one. A page load during a live session is a moment
            # the injected heartbeat SHOULD be running. Record it; app.py records the
            # last time it actually charged. The two diverging means the clock has
            # stopped while browsing continues — the tool's whole job, failing silently.
            if is_navigation:
                _try_redis(lambda: _note_navigation(time.time()))
            return

        # No session (no budget, cooldown, or idled out). Serve the budget/cooldown
        # page IN PLACE at the current real URL, rather than redirecting to a /budget
        # path — the site's SPA/service worker would turn that into its own "not
        # found" page first. Sub-requests just fail quietly.
        if is_navigation:
            try:
                # Pass the original URL so the gate's Enter button can return the user
                # to the link they clicked, not just the site home.
                nxt = quote(flow.request.pretty_url, safe="")
                resp = req.get(f"http://127.0.0.1:5000/budget?site={site}&next={nxt}", timeout=2)
                flow.response = http.Response.make(
                    200, resp.content,
                    {"Content-Type": "text/html; charset=utf-8"}
                )
            except Exception:
                flow.response = http.Response.make(
                    200, b"Budget server unreachable",
                    {"Content-Type": "text/html; charset=utf-8"}
                )
        else:
            flow.response = http.Response.make(503, b"", {})
        return

    def response(self, flow: http.HTTPFlow):
        # Inject the visibility-aware heartbeat into pages of a budgeted site (so only
        # foreground time is charged), OR the frosted "tap to reveal" overlay into
        # Facebook (no budget — decrypted for injection only).
        host = flow.request.pretty_host
        site = site_for_host(host)
        ov = None if site else overlay_for_host(host)
        if not site and not ov:
            return
        if flow.request.path.startswith("/budget"):
            return  # don't inject into the budget/enter pages themselves

        if "text/html" not in flow.response.headers.get("content-type", ""):
            return
        try:
            body = flow.response.get_text(strict=False)
        except Exception as e:
            _note_error("inject_decode", e)
            return
        if not body:
            return

        if ov:
            # Facebook overlay: no session, no heartbeat — just cover the home feed.
            # SW_KILL first: Facebook runs a service worker that serves cached pages past
            # our injection, so unregister it (needs a one-time site-data clear to break
            # the initial cached load — see notes).
            injection = SW_KILL + ov["inject"]
        else:
            # Only inject during a live session (budgeted or study).
            mode = session_mode(site)
            if mode is None:
                return
            injection = HEARTBEAT_SCRIPT.replace("__SITE__", site)
            # Prepended, not appended, and outside the nonce rewrite below: it must be
            # the first thing in the injection and must not depend on scripts running.
            if _try_redis(enforcement_looks_dead, False):
                injection = ENFORCEMENT_WARNING + injection
            if site in ("youtube", "reddit"):
                injection += SW_KILL
            if site == "youtube":
                injection += YOUTUBE_DECLUTTER
                if mode == "study":
                    injection += STUDY_LOCK.replace("__PLAYLISTS__", json.dumps(STUDY_PLAYLISTS))
        # Carry the nonce we added to the page's CSP, so the browser will run these
        # scripts under the site's own (still-enforced) policy. No nonce means either the
        # page had no CSP or its policy already allowed inline scripts — either way the
        # attribute is harmless.
        nonce = flow.metadata.get("cooldown_nonce")
        if nonce:
            injection = injection.replace("<script>", f'<script nonce="{nonce}">')

        # Inject before </body> when present; mobile YouTube ships NO </body>, so fall
        # back to </html> (which it does have), then to appending at the very end.
        if "</body>" in body:
            flow.response.text = body.replace("</body>", injection + "</body>", 1)
        elif "</html>" in body:
            flow.response.text = body.replace("</html>", injection + "</html>", 1)
        else:
            flow.response.text = body + injection

addons = [BudgetAddon()]
