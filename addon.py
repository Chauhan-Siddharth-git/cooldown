from mitmproxy import http
from urllib.parse import urlsplit, parse_qs, quote
import json
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
STUDY_PLAYLISTS = ["PLG49S3nxzAnl4QDVqK-hOnoqcSKEIDDuv"]  # Professor Messer SY0-701

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

def overlay_for_host(host):
    """An overlay-inject site (Facebook) for this host, else None."""
    host = (host or "").rsplit(":", 1)[0].lower()
    for cfg in OVERLAY_SITES.values():
        if any(host == m or host.endswith("." + m) for m in cfg["match"]):
            return cfg
    return None

def site_for_host(host):
    # Suffix match on the registrable domain, NOT a substring: "reddit.com" must
    # match reddit.com and *.reddit.com, but never evil-reddit.com or
    # reddit.com.attacker.io (which a substring check would gate — and decrypt).
    host = (host or "").rsplit(":", 1)[0].lower()   # drop any :port
    for site, cfg in SITES.items():
        if any(host == m or host.endswith("." + m) for m in cfg["match"]):
            return site
    return None

def session_mode(site):
    """Return the active session's mode ('active' or 'study'), or None if there's
    no live session for this site."""
    token = r.get(f"active_token:{site}")
    if not token:
        return None
    return r.get(f"session:{token}")  # None if the session key has expired

def study_url_allowed(path):
    """True only for /watch and /playlist URLs carrying an allowlisted playlist."""
    parts = urlsplit(path)
    if parts.path not in ("/watch", "/playlist"):
        return False
    lists = parse_qs(parts.query).get("list", [])
    return any(l in STUDY_PLAYLISTS for l in lists)

class BudgetAddon:
    def responseheaders(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        if site_for_host(host):
            flow.response.stream = False
            if "content-security-policy" in flow.response.headers:
                del flow.response.headers["content-security-policy"]
            if "content-security-policy-report-only" in flow.response.headers:
                del flow.response.headers["content-security-policy-report-only"]
        elif overlay_for_host(host):
            # Facebook: buffer + CSP-strip ONLY the HTML document we inject into; STREAM
            # everything else (its realtime / long-poll / WebSocket traffic) so buffering
            # a never-ending response can't hang the page.
            if "text/html" in flow.response.headers.get("content-type", ""):
                flow.response.stream = False
                if "content-security-policy" in flow.response.headers:
                    del flow.response.headers["content-security-policy"]
                if "content-security-policy-report-only" in flow.response.headers:
                    del flow.response.headers["content-security-policy-report-only"]
            else:
                flow.response.stream = True

    def request(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        path = flow.request.path

        # Serve budget pages from any gated host under its /budget path. The query
        # string (which carries ?site=) is preserved so Flask charges the right site.
        if path.startswith("/budget") and site_for_host(host):
            # CSRF: the mutating endpoints (/enter, /study, /exit, /heartbeat) are
            # POSTed same-origin from the gate page / injected script. A forged POST
            # from another site the user is visiting is "cross-site" — reject it so a
            # malicious page can't drive the budget state (or the return redirect).
            if flow.request.method == "POST" and \
               flow.request.headers.get("Sec-Fetch-Site") == "cross-site":
                flow.response = http.Response.make(
                    403, b"cross-site request blocked",
                    {"Content-Type": "text/plain; charset=utf-8"})
                return
            parts = urlsplit(path)
            sub = parts.path[len("/budget"):]          # "" | "/heartbeat" | "/enter"
            flask_path = sub if sub else "/budget"
            if parts.query:
                flask_path += "?" + parts.query

            try:
                if flow.request.method == "POST":
                    resp = req.post(f"http://127.0.0.1:5000{flask_path}", timeout=2, allow_redirects=False)
                else:
                    resp = req.get(f"http://127.0.0.1:5000{flask_path}", timeout=2, allow_redirects=False)

                if resp.status_code == 302:
                    location = resp.headers.get("Location", f"https://{host}/budget")
                    # Rewrite Flask's internal address back to the real site the
                    # browser is on, so relative redirects (e.g. /budget) land right.
                    location = location.replace("http://127.0.0.1:5000", f"https://{host}")
                    flow.response = http.Response.make(302, b"", {"Location": location})
                else:
                    flow.response = http.Response.make(
                        resp.status_code,
                        resp.content,
                        {"Content-Type": "text/html; charset=utf-8"}
                    )
            except Exception as e:
                print(f"[DEBUG] Budget handler error: {e}")
                flow.response = http.Response.make(500, b"Budget server error")
            return

        if any(ignored in host for ignored in IGNORED_HOSTS):
            return

        site = site_for_host(host)
        if not site:
            return

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
        except Exception:
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
            if site in ("youtube", "reddit"):
                injection += SW_KILL
            if site == "youtube":
                injection += YOUTUBE_DECLUTTER
                if mode == "study":
                    injection += STUDY_LOCK.replace("__PLAYLISTS__", json.dumps(STUDY_PLAYLISTS))
        # Inject before </body> when present; mobile YouTube ships NO </body>, so fall
        # back to </html> (which it does have), then to appending at the very end.
        if "</body>" in body:
            flow.response.text = body.replace("</body>", injection + "</body>", 1)
        elif "</html>" in body:
            flow.response.text = body.replace("</html>", injection + "</html>", 1)
        else:
            flow.response.text = body + injection

addons = [BudgetAddon()]
