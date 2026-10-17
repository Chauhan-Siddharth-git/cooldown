from mitmproxy import http
from urllib.parse import urlsplit, parse_qs, quote
from datetime import datetime
from zoneinfo import ZoneInfo
import functools
import json
import re
import secrets
import time
import os
import sys
import redis
# Explicit, because the defaults are the finding: see the client construction below.
from redis.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff
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
    # Instagram WEB only, and only the page host: bare instagram.com would also name
    # i./edge-chat./graph., which the CA must not vouch for (RFC 5280 constraints cover
    # subdomains). Budgeted by PATH, not by host -- see free_segments(). stream_non_html:
    # buffer ONLY the HTML we inject into, as for Facebook -- Meta's graphql streams its
    # responses, and stream = False on those hangs the page.
    "instagram": {"match": ["www.instagram.com"], "stream_non_html": True},
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

# Timeouts, because these calls run on mitmproxy's event loop. Without them a Redis
# that is reachable but wedged (swapping, blocked on an AOF rewrite) hangs the proxy
# itself rather than failing a request -- and a hung proxy is every gated site hanging.
# Two seconds is far above a healthy round trip and far below a user's patience.
# Failing fast is what lets the fail-closed path above actually run.
#
# And the retry policy, which is the part that actually mattered. redis-py defaults to
# THREE retries with exponential-with-jitter backoff, so a single failing call costs ~3s
# of mostly sleeping even when the failure is an instant ECONNREFUSED. Measured with Redis
# refused: one gated request took 15.7s end to end, and 8.4s of that was _note_error's two
# writes -- the error reporter on the fail-closed path, retrying against the very Redis
# whose death it was reporting. The reporting cost more than the failure it reported.
#
# One retry with a token backoff keeps the thing retries are actually for -- a momentary
# blip, where an immediate second attempt succeeds -- and bounds the cost. Measured over
# four failing calls: 20.7s at the default, 0.039s here. A healthy Redis is unaffected
# (200 successful gets: 0.114s default, 0.112s with this).
r = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True,
                socket_timeout=2, socket_connect_timeout=2,
                retry=Retry(ExponentialWithJitterBackoff(base=0.01, cap=0.05), 1))

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
  var WARN_AT  = 60;      // show the frame when this many seconds (or fewer) remain
  var URGENT_AT = 10;     // ...and start pulsing it at this many
  var SITE = "__SITE__";
  var LABEL = SITE.charAt(0).toUpperCase() + SITE.slice(1);
  var deadline = null;    // ms timestamp when time runs out (re-anchored each ping)
  var curPhase = null;    // "day" | "winddown" | "night" from the last ping

  // A frame around the edge of the viewport, not a band across the top.
  //
  // The band was position:fixed;top:0;left:0;right:0 -- which is exactly where YouTube's
  // and Reddit's search bars are. pointer-events:none meant clicks passed through, so it
  // never blocked anything; it just made the thing you were clicking invisible, which is
  // most of the way to blocking it. A frame takes no content area at all.
  var bar = null, pill = null;
  function ensureBar() {
    if (bar) return bar;
    var css = document.createElement("style");
    css.textContent =
      '#bp-timewarn{position:fixed;inset:0;z-index:2147483647;pointer-events:none;' +
      'box-shadow:inset 0 0 0 4px #c0392b,inset 0 0 22px rgba(192,57,43,.30);' +
      'will-change:opacity;transform:translateZ(0);}' +
      // Only opacity animates, and the layer is promoted first. Animating the box-shadow
      // repaints the whole viewport every frame; the frost overlay already taught this
      // project that lesson by dropping the page to a crawl while it ran.
      '#bp-timewarn.bp-urgent{animation:bp-pulse 1s ease-in-out infinite;}' +
      '@keyframes bp-pulse{50%{opacity:.28}}' +
      '#bp-timepill{position:fixed;z-index:2147483647;pointer-events:none;' +
      'right:max(12px,env(safe-area-inset-right));' +
      'bottom:max(12px,env(safe-area-inset-bottom));' +
      'font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;' +
      'color:#fff;background:#c0392b;padding:7px 11px;border-radius:999px;' +
      'box-shadow:0 2px 10px rgba(0,0,0,.45);letter-spacing:.2px;}' +
      // Reduced motion gets a thicker ring instead of a pulse -- still an escalation,
      // just not a moving one.
      '@media (prefers-reduced-motion:reduce){#bp-timewarn.bp-urgent{animation:none;' +
      'box-shadow:inset 0 0 0 8px #c0392b,inset 0 0 26px rgba(192,57,43,.40);}}';
    (document.head || document.documentElement).appendChild(css);
    bar = document.createElement("div");
    bar.id = "bp-timewarn";
    bar.setAttribute("aria-hidden", "true");      // decorative; the pill carries the text
    document.documentElement.appendChild(bar);
    pill = document.createElement("div");
    pill.id = "bp-timepill";
    pill.setAttribute("role", "status");
    document.documentElement.appendChild(pill);
    return bar;
  }
  function showWarn(secs) {
    ensureBar();
    bar.style.display = "block";
    pill.style.display = "block";
    pill.textContent = secs + "s \\u00B7 " + LABEL;
    if (secs <= URGENT_AT) bar.classList.add("bp-urgent");
    else bar.classList.remove("bp-urgent");
  }
  function hideWarn() {
    if (!bar) return;
    bar.style.display = "none";
    bar.classList.remove("bp-urgent");
    if (pill) pill.style.display = "none";
  }

  // Wind-down gets the same treatment: it was a top band too, and a persistent one, so it
  // covered the header for the whole tapering period rather than for the last minute.
  // Calm by design -- a pill, no frame, no motion.
  var wdBar = null;
  function ensureWdBar() {
    if (wdBar) return wdBar;
    var css = document.createElement("style");
    css.textContent =
      '#bp-winddown{position:fixed;z-index:2147483646;pointer-events:none;' +
      'right:max(12px,env(safe-area-inset-right));' +
      'bottom:max(12px,env(safe-area-inset-bottom));' +
      'font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;' +
      'color:#fff;background:#b9770e;padding:7px 11px;border-radius:999px;' +
      'box-shadow:0 2px 10px rgba(0,0,0,.35);letter-spacing:.2px;}';
    (document.head || document.documentElement).appendChild(css);
    wdBar = document.createElement("div");
    wdBar.id = "bp-winddown";
    wdBar.setAttribute("role", "status");
    wdBar.textContent = "\\u23F3 Wind-down";
    document.documentElement.appendChild(wdBar);
    return wdBar;
  }
  function showWd() { ensureWdBar().style.display = "block"; }
  function hideWd() { if (wdBar) wdBar.style.display = "none"; }

  // Local 1s ticker: the red last-minute warning takes the top slot; otherwise the
  // wind-down ribbon shows whenever the last ping said we're in wind-down.
  setInterval(function () {
    // Nothing is being charged on a free path, so nothing counts down there. Without this
    // the last-minute frame followed you from the feed into a DM thread and ran to zero
    // over it, which reads as the DMs spending budget.
    if (isFree()) { hideWarn(); hideWd(); return; }
    var warn = false;
    if (deadline !== null) {
      var secs = Math.round((deadline - Date.now()) / 1000);
      if (secs > 0 && secs <= WARN_AT) { showWarn(secs); warn = true; }
    }
    if (!warn) hideWarn();
    if (!warn && curPhase === "winddown") showWd(); else hideWd();
  }, 1000);

  // Instagram only (null everywhere else, so every path there is budgeted): the
  // first-path-segments that are free of budget. Substituted from free_segments() at
  // injection time so this and the proxy's own classifier read ONE list. They must agree
  // on every path: a page this thinks is budgeted but the proxy serves for free would ping,
  // get a 403 and reload, forever.
  var FREE = __FREE__;
  function isFree() {
    if (!FREE) return false;
    // Same rule as path_is_free(): the FIRST segment, exactly, case-folded.
    // "//direct" has an empty first segment and is therefore budgeted, as in Python.
    var seg = location.pathname.split("/")[1];
    return typeof seg === "string" && seg !== "" && FREE.indexOf(seg.toLowerCase()) !== -1;
  }

  function ping() {
    if (document.visibilityState !== "visible") return;
    // Free paths (Instagram DMs, own profile, /accounts) neither charge nor refresh the
    // session. NO keep-alive here: a ping that holds the session open without charging is
    // exactly the paced-pinging hole D1 closed.
    if (isFree()) return;
    // The phone knows what zone it is in and the Pi does not -- its clock never leaves
    // Boston. Sent every ping because it costs nothing and the server ignores repeats.
    var tz = "";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
    fetch("/budget/heartbeat?site=" + SITE + "&tz=" + encodeURIComponent(tz) + "&_=" + Date.now(), { method: "POST", cache: "no-store", keepalive: true })
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

  // An SPA hop from a free path to a budgeted one (DMs -> Home) is a pushState, which the
  // proxy never sees. Ping the moment we ENTER a budgeted path: with no session that is a
  // 403 -> reload -> the proxy serves the gate; with one it is just an early ping. Hooks
  // pushState/replaceState/popstate with a 1s backstop, the way deShort() does for YouTube.
  if (FREE) {
    var wasFree = isFree();
    var onPath = function () {
      var free = isFree();
      if (wasFree && !free) ping();
      wasFree = free;
    };
    ["pushState", "replaceState"].forEach(function (fn) {
      var orig = history[fn];
      history[fn] = function () {
        var r = orig.apply(this, arguments);
        try { onPath(); } catch (e) {}      // never let our hook break the site's router
        return r;
      };
    });
    window.addEventListener("popstate", onPath);
    setInterval(onPath, 1000);
  }
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

# Instagram's home feed, cut back to the people you follow. Anchored on the TEXT a reader
# sees, not on classes: Instagram's class names are generated and change every build,
# while "Suggested for you" and "You're all caught up" are what this feature is about.
# Three things go -- suggested posts inside the feed, the suggested-accounts and Reels
# shelves between posts, and everything after the "caught up" marker, which is where
# Instagram turns the end of your friends' posts into an endless feed of strangers'. In
# its place, one line saying that is the end. Home feed only (the rest is budgeted or
# free as a whole), English UI only: other languages pass through unchanged.
INSTAGRAM_DECLUTTER = """
<script>
(function () {
  "use strict";
  if (window.__cdIgDeclutter) return; window.__cdIgDeclutter = true;
  var SHELF = /^(suggested for you|suggested posts|suggested reels|reels)$/i;
  var CAUGHT_UP = /^you(?:'|\\u2019)(?:re all|ve completely) caught up$/i;
  function hide(node) {
    if (!node || node.getAttribute("data-cd-hidden")) return;
    node.setAttribute("data-cd-hidden", "1");
    node.style.setProperty("display", "none", "important");
  }
  // What to hide for a label: the post it sits in, else the feed-level block holding it --
  // the first ancestor with a post among its siblings.
  function unitFor(el) {
    var a = el.closest("article");
    if (a) return a;
    for (var n = el, i = 0; i < 14 && n.parentElement; i++, n = n.parentElement) {
      for (var c = n.parentElement.firstElementChild; c; c = c.nextElementSibling) {
        if (c !== n && (c.tagName === "ARTICLE" || c.querySelector("article"))) return n;
      }
    }
    return null;
  }
  function pass() {
    if (location.pathname !== "/") return;
    var root = document.querySelector("main") || document.body;
    if (!root) return;
    var marker = null, list = root.querySelectorAll("span, h2, h3, h4, div[role='heading']");
    for (var i = 0; i < list.length; i++) {
      var el = list[i], txt = (el.textContent || "").trim();
      if (!txt || txt.length > 40 || el.children.length > 1) continue;
      if (!marker && CAUGHT_UP.test(txt)) marker = el;
      else if (SHELF.test(txt)) hide(unitFor(el));
    }
    if (!marker) return;
    var posts = root.querySelectorAll("article");
    for (var j = 0; j < posts.length; j++) {
      if (marker.compareDocumentPosition(posts[j]) & Node.DOCUMENT_POSITION_FOLLOWING) hide(posts[j]);
    }
    if (!document.getElementById("cd-ig-end")) {
      var block = unitFor(marker) || marker, d = document.createElement("div");
      d.id = "cd-ig-end";
      d.textContent = "That's everything from the people you follow.";
      d.style.cssText = "padding:28px 16px 96px;text-align:center;color:#8e8e8e;" +
        "font:600 14px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif";
      if (block.parentNode) block.parentNode.insertBefore(d, block.nextSibling);
    }
  }
  var queued = false;
  function soon() {
    if (queued) return;
    queued = true;
    setTimeout(function () { queued = false; pass(); }, 250);
  }
  new MutationObserver(soon).observe(document.documentElement, { childList: true, subtree: true });
  ["pushState", "replaceState"].forEach(function (fn) {
    var o = history[fn];
    history[fn] = function () { var r = o.apply(this, arguments); soon(); return r; };
  });
  window.addEventListener("popstate", soon);
  soon();
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

# --- Instagram: budget the feed, leave DMs and your own profile free ------------------
# Instagram is an SPA, so a host-level gate (what every other site gets) cannot say "DMs
# yes, feed no": tapping Home from a DM thread is a pushState the proxy never sees. The
# split is by PATH, in two cooperating halves -- request() gates budgeted navigations, and
# the injected script (HEARTBEAT_SCRIPT, FREE) skips pings on free paths and pings at once
# on entering a budgeted one. Both read free_segments(), so they cannot drift.
#
# An ALLOW-LIST of free first path segments, the Facebook overlay's model: whatever
# Instagram adds later is budgeted by default, which is the direction to fail.
INSTAGRAM_FREE_BASE = ("direct", "accounts")     # DMs (/direct/...), login/settings/activity

# Your own handle, from Environment=INSTAGRAM_SELF= in the unit, so this file stays
# byte-identical across the two repos. Unset or malformed frees nothing (fail closed).
INSTAGRAM_SELF = os.environ.get("INSTAGRAM_SELF", "")
_HANDLE_RE = re.compile(r"[a-z0-9._]{1,30}")     # Instagram's own handle alphabet


@functools.lru_cache(maxsize=8)
def _instagram_free(handle):
    own = handle.strip().strip("/").lstrip("@").lower()
    return INSTAGRAM_FREE_BASE + ((own,) if _HANDLE_RE.fullmatch(own) else ())


def instagram_free_segments():
    """First path segments that cost no budget: DMs, /accounts, and your own profile.

    Cached on the handle. This runs for every Instagram request, sub-requests included,
    and the answer changes only when the unit does -- so normalising and regex-checking
    the same string on the proxy's hot path was work with one possible result.
    """
    return _instagram_free(INSTAGRAM_SELF)


def free_segments(site):
    """The first path segments of `site` that cost no budget, or None when every path is
    budgeted -- which is every site but Instagram.

    The ONE place a hook asks whether a site is budgeted by path: request() lets free
    traffic past the gate and both meters, response() injects without a session and hands
    the script this list. Five call sites each naming "instagram" was the shape the next
    such site would have had to find by hand.
    """
    return instagram_free_segments() if site == "instagram" else None


def path_is_free(site, path):
    """Is this request path free of budget on `site`? Segment-exact and case-insensitive
    on the FIRST segment: /direct/inbox/ is free, /directory/ is not, and /mine/followers/
    is free while /minex/ is not (F4's class -- a prefix test where a segment test was
    meant).

    Deliberately not urlsplit(): "//direct/x" parses as host "direct" with path "/x", and
    "//evil/direct" as path "/direct" -- both would be classified by something other than
    what the browser sent. The first segment of "//direct" is empty, hence budgeted.
    """
    segs = free_segments(site)
    if not segs:
        return False
    p = (path or "").split("?", 1)[0].split("#", 1)[0]
    if not p.startswith("/"):
        return False
    seg = p.split("/")[1].lower()
    return seg != "" and seg in segs


def _is_navigation(flow):
    return (flow.request.headers.get("Sec-Fetch-Mode", "") == "navigate"
            or flow.request.headers.get("Sec-Fetch-Dest", "") == "document")


def request_is_free(site, flow):
    """Does this request belong to a free part of `site`? A navigation is judged by its own
    path; a sub-request (API, graphql) carries no meaning in its own path, so it takes its
    Referer's -- the page that made it. That is what lets the DMs' API calls through with
    no session while the feed's 503 exactly as every other site's do.

    No usable Referer (absent, or not this site) falls back to the request's own path,
    which for an API call is budgeted. That is the closed direction for both consumers: the
    gate refuses it, and the shadow meter counts it -- a missing header must neither open
    the feed nor hide a measurement. If DMs ever break at zero budget, look here first:
    it means the browser stopped sending a same-origin Referer.
    """
    if free_segments(site) is None:
        return False
    if not _is_navigation(flow):
        ref = flow.request.headers.get("Referer", "")
        if ref:
            parts = urlsplit(ref)
            if host_matches(parts.hostname or "", SITES[site]["match"]):
                return path_is_free(site, parts.path or "/")
    return path_is_free(site, flow.request.path)


# The switch compares the two meters against each other rather than guessing from page
# loads. The heartbeat and the passive shadow meter measure the same quantity by
# independent means; only one of them can be blocked from inside the page, so a large
# disagreement identifies which. Seven days of paired data put them at 1.03x of each
# other (the readout in the shadow meter's header, below), so the threshold sits nowhere
# near normal noise.
ENFORCEMENT_WINDOW_HOURS = 2          # how far back to compare
ENFORCEMENT_MIN_PASSIVE  = 5 * 60     # need this much observed use before judging at all
ENFORCEMENT_RATIO        = 0.4        # heartbeat below this share of passive = dead
ENFORCEMENT_NAV_WINDOW   = 5 * 60     # still recorded, for the /health "browsing" flag


def _try_redis(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# The clocks app.py calls local_acct() and local_policy(), for this process. The two
# cannot share code -- mitmdump loads this file alone -- so they share the KEYS instead:
# tz_accounting and tz_policy, the same BUDGET_HOME_TZ default, the same fallback to the
# box's own clock when nothing is set. A day key written here and read there has to name
# the same day, and before 2026-10-16 every key this file wrote used the box's clock
# while app.py's readers used yours (PLAN.md, Known gaps). Cached like app.py's, behind
# one MGET: _shadow_day() runs on every request to a gated host.
HOME_TZ = os.environ.get("BUDGET_HOME_TZ", "")
_ZONES = {"at": -1e9, "acct": "", "policy": ""}
_ZONES_TTL = 5.0                    # app.py's TTL: an adoption lands within seconds


def _zones_bust():
    _ZONES["at"] = -1e9


def _zones():
    mono = time.monotonic()
    if mono - _ZONES["at"] > _ZONES_TTL:
        vals = _try_redis(lambda: r.mget("tz_accounting", "tz_policy")) or [None, None]
        _ZONES["acct"] = vals[0] or HOME_TZ
        _ZONES["policy"] = vals[1] or HOME_TZ
        _ZONES["at"] = mono
    return _ZONES


def _lt(tzname, now):
    if not tzname:
        return time.localtime(now)       # nothing configured: the box's own clock
    try:
        return datetime.fromtimestamp(now, ZoneInfo(tzname)).timetuple()
    except Exception:
        return time.localtime(now)       # an unknown zone degrades, never raises


def local_acct(now):
    """Wall clock the books are kept in -- for anything that becomes a date key."""
    return _lt(_zones()["acct"], now)


def local_policy(now):
    """Wall clock where you are -- for anything that compares an hour of the day."""
    return _lt(_zones()["policy"], now)


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
        t = local_acct(now - i * 3600)
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
# SHADOW METER — the dead-man's switch's second clock. It began on 2026-08-03 as an
# experiment asking whether passive measurement could replace the injected heartbeat.
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
# The answer, 2026-08-10 (7 days, 42 paired hours): passive tracks the heartbeat at
# 1.03x and never went blind -- not one hour where the heartbeat charged and this saw
# nothing. It reads high when a background tab or autoplay keeps requesting, and it
# cannot see visibility at all, so the injection stays (it also draws the countdown).
# What it is good at is noticing a LARGE disagreement, so enforcement_looks_dead() keeps
# it as the second clock. The /stats card that showed the two side by side was retired
# 2026-10-16; the audit's shadow_silent finding now watches that this keeps recording.
# Removing it blinds the switch -- CLAUDE.md says what has to go with it.
#
# Nothing here can affect the budget: it writes only shadow_hour:* keys, and every
# Redis call is wrapped so a measurement can never break the proxy.
# ---------------------------------------------------------------------------
SHADOW_MAX_GAP = 30.0        # same cap as the heartbeat: bigger gaps are idle, not use
SHADOW_FLUSH_AFTER = 10.0    # accrue in memory, write once per 10 charged seconds
_SHADOW = {}                 # site -> {"last": epoch, "pending": secs, "day": "Y-m-d"}


def _shadow_day(now):
    return time.strftime("%Y-%m-%d", local_acct(now))


def _shadow_flush(site, st):
    if st["pending"] <= 0:
        return
    def write():
        # Hourly, as the heartbeat records (usage_hour), because the switch compares the
        # two over the same clock hours. The daily shadow_usage total and shadow_started
        # existed only for the retired /stats comparison and are no longer written.
        # The hour comes from the same accrual time as st['day'], never a bare
        # strftime('%H'): that read the wall clock, so the midnight flush banked
        # yesterday's last seconds under hour 00 of yesterday's date, and any caller
        # passing its own `now` got a key that matched neither clock. In the accounting
        # zone, as _meter_totals reads it and as app.py writes usage_hour.
        hk = (f"shadow_hour:{st['day']}:"
              f"{time.strftime('%H', local_acct(st['last']))}:{site}")
        r.incrbyfloat(hk, st["pending"])
        r.expire(hk, 14 * 86400)
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
    print(f"[ERROR] {where}: {exc}", flush=True)


# Hardening headers for any HTML this proxy synthesises on a gated origin. One dict so the
# /budget path and the in-place gate cannot drift apart -- they did, and only the endpoint
# set had a test.
_GATE_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _is_cross_origin(flow):
    """Is this request driven from another site? Deliberately identical to app.py's
    _cross_origin_to_guarded_route() predicate.

    F25's lesson was that a control belongs to the path, not to the door somebody
    remembered -- and the two doors then disagreed anyway. This one tested exactly
    `Sec-Fetch-Site == "cross-site"`, which is narrower than the box's check in two ways
    that both fail OPEN:

      · A client sending no Sec-Fetch-* headers at all made the comparison False, so the
        request was forwarded. app.py could not back it up either, because
        _forwarded_headers passes Content-Type and nothing else -- Flask saw neither
        signal and allowed by design. Both doors open at once, for that client.
      · `Sec-Fetch-Site: same-site` was allowed here and refused at the box.

    Modern Chrome, Firefox and Safari 16.4+ always send the header, so the practical
    exposure was older iOS and in-app webviews on the tailnet -- narrow, not empty.

    The suite already knew: CROSS_SITE_SHAPES defines a "no-sec-fetch-headers" shape with
    the comment "a browser with no Sec-Fetch support carries only Origin", and those shapes
    were parametrised over the box door only. The proxy-door test hardcoded the single
    shape the code handled. Tests written against the implementation agree with it.
    """
    fetch_site = flow.request.headers.get("Sec-Fetch-Site")
    if fetch_site:
        # "none" is a user-initiated navigation (typed, bookmarked) -- a page cannot
        # produce it. Anything else foreign ("cross-site", "same-site") is refused.
        return fetch_site not in ("same-origin", "none")
    origin = flow.request.headers.get("Origin")
    if origin:
        # A sandboxed iframe sends Origin: null, which parses to "" and is refused.
        # host_header carries the port when there is one, matching Flask's request.host.
        host = flow.request.host_header or flow.request.pretty_host
        return urlsplit(origin).netloc != host
    return False



# --- blunt block for things that cannot be gated -------------------------------------
# A native app has nowhere to inject a heartbeat, so there is no budget, no countdown and
# no gate page for it -- only on or off. PLAN.md called this years ago: "if usage ever
# shifts to the native apps, the only option there is blunt DNS-level time-windowing".
#
# HOW THE BLOCK IS ENFORCED. Two mechanisms, because one of them turned out to depend on
# the client's cooperation and the client is not ours.
#
#   1. The certificate. These hosts are in --allow-hosts, so the proxy intercepts them,
#      and they are deliberately absent from the CA's name constraints, so the leaf it
#      mints is one the CA may not sign. A client that validates properly hangs up.
#   2. A 403 in request(). Reaching that code means the client trusted the forged
#      certificate anyway -- precisely where mechanism 1 does nothing.
#
# This was originally written with only mechanism 1, and with the confident note that it
# was "a far stronger control than anything the proxy could do by itself: short of
# untrusting the CA, the phone will not complete the handshake". That was wrong about the
# case it was written for. The zombsroyale iOS app completed the handshake, because a
# user-installed CA is one an app may honour and this one does. The proxy logged a block
# and decrypted the game. The lesson generalises past this host: a control that runs on
# the other side of the boundary is a control you do not have, and the log line that says
# otherwise is the dangerous part -- not the gap, but the false report of no gap.
#
# The CLOCK still lives in this hook. Outside the blocked window it sets
# ignore_connection and the traffic passes through unread -- no decryption attempted,
# which matters: this host is only decrypted in order to be broken, and outside the
# window there is no reason to look at it. Inside the window it lets both mechanisms run.
BLOCKED_HOSTS = ["zombsroyale.io", "4chan.org", "4channel.org", "4cdn.org"]

# A diagnostic, not a control. Passthrough connections log only an IP -- the proxy never
# learns their hostname because it never intercepts them -- so "does the app actually talk
# to the host I blocked?" is unanswerable from the journal. Anything whose SNI contains one
# of these substrings gets logged, blocked or not.
#
# Narrow on purpose. Logging every SNI would answer the question too and would also write
# a complete browsing history of every device to the journal, which is a much larger thing
# to do to yourself than the question deserves. Empty this list once the answer is known.
#
# It is empty because the answer IS known: the app talks to `mason.zombsroyale.io`. A
# subdomain, which host_matches() and the --allow-hosts regex both already cover, so the
# blocklist needed no second entry. Repopulate this only to ask the same question about
# some other host.
WATCH_SNI = []

# All day, on purpose. The first window here was 09:00-21:00 -- picked from an assumption
# about when a phone game gets played, never from evidence. The probe above recorded the
# real session at 23:57, in the gap the window deliberately left open, so the block was
# simultaneously correct and useless: the hostname matched, the clock said no, and the
# connection passed through with nothing in the journal to say why. Keep both hours here
# rather than special-casing "always" -- the window machinery is still what runs, and a
# 0-24 window exercises the same path a narrower one would.
BLOCK_FROM_HOUR = 0          # inclusive, local time
BLOCK_TO_HOUR = 24           # exclusive


def window_label():
    """How the blocked window should read in a log line."""
    if BLOCK_FROM_HOUR == 0 and BLOCK_TO_HOUR == 24:
        return "all day"
    return f"{BLOCK_FROM_HOUR:02d}:00-{BLOCK_TO_HOUR:02d}:00 local"


def blocked_now(host, now=None):
    """Is this host inside its blocked window right now?"""
    if not host_matches(host, BLOCKED_HOSTS):
        return False
    h = local_policy(now if now is not None else time.time()).tm_hour
    if BLOCK_FROM_HOUR <= BLOCK_TO_HOUR:
        return BLOCK_FROM_HOUR <= h < BLOCK_TO_HOUR
    return h >= BLOCK_FROM_HOUR or h < BLOCK_TO_HOUR      # windows that wrap midnight


def session_mode(site):
    """Return the active session's mode ('active'), or None if there's no live session
    for this site.

    Still a lookup rather than a boolean, with study mode gone and 'active' the only
    value: F03 was a fail-OPEN on exactly this read, and collapsing it to a truth test
    would make a second mode, if one is ever added, silently behave like the first."""
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
# /study and /exit left with study mode on 2026-09-16. /exit is gone because its ONLY
# caller was the "Exit study mode" button inside the lock injection -- nothing else ever
# linked to it, and a state-changing endpoint on the gated origin that nothing calls is
# surface with no benefit. That takes this set from 8 endpoints to 6; CLAUDE.md called the
# cap of 8 "fully spent", and it is now a quarter empty.
STATE_CHANGING = ("/enter", "/heartbeat", "/reflect")
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


# --- who is actually routing through this box ----------------------------------------
# Tailscale knows whether your phone is ONLINE; only this process knows whether its
# traffic is coming THROUGH here. The gap between the two is the largest hole in the
# design (PLAN.md, Known gaps): deselect the exit node and every gate is gone, silently.
# app.py's check_bypass() compares them. client_connected fires for every connection,
# including the ones --allow-hosts passes through unread, so this sees all of it. It
# keeps the latest time per address in memory and writes at most once a minute, because
# this is the proxy's hottest path (F12).
CLIENT_SEEN_FLUSH = 60.0
_CLIENT_SEEN = {}                    # client address -> epoch of its latest connection
_CLIENT_SEEN_AT = [0.0]              # when that map was last written


def note_client(addr, now):
    if not addr:
        return
    if addr.startswith("::ffff:"):
        addr = addr[7:]                  # IPv4-mapped: the tailnet address app.py knows
    _CLIENT_SEEN[addr] = now
    if now - _CLIENT_SEEN_AT[0] >= CLIENT_SEEN_FLUSH:
        snap = {a: f"{t:.0f}" for a, t in _CLIENT_SEEN.items()}
        if _try_redis(lambda: r.hset("client_last_seen", mapping=snap), "failed") != "failed":
            _CLIENT_SEEN_AT[0] = now


class BudgetAddon:
    def tls_clienthello(self, data):
        """Decide, from the SNI alone, whether to let this connection past unread.

        Runs before any decryption, so nothing here depends on the CA being able to vouch
        for the host -- which matters, because for a blocked host it deliberately cannot.

        Only ever WIDENS what passes through. Setting ignore_connection lets traffic by
        untouched; leaving it alone falls back to mitmproxy's own --allow-hosts decision.
        There is no code path here that intercepts something it otherwise would not, which
        is the property to preserve if this list ever grows.
        """
        try:
            sni = data.client_hello.sni or ""
            if not sni:
                return
            low = sni.lower()
            if any(w in low for w in WATCH_SNI):
                print(f"[WATCH] sni={sni}", flush=True)
            if not host_matches(sni, BLOCKED_HOSTS):
                return
            if blocked_now(sni):
                # Say so explicitly. Otherwise the journal shows a handshake failure that
                # is indistinguishable from a real trust problem -- and this project has
                # already lost an evening to exactly that confusion.
                # flush=True, and that is not incidental. mitmdump's stdout is a pipe
                # into the journal, so print() is block-buffered and a line every few
                # minutes can sit unwritten for a long time. The first version of this
                # hook logged nothing visible while the block itself worked perfectly --
                # a diagnostic that was itself undiagnosable, which is the joke this
                # project keeps writing. _note_error's print has the same flaw.
                # "intercepting", not "refused". The earlier wording claimed the
                # handshake had been rejected, which this hook cannot know and which
                # was false for the app that prompted the block -- it trusted our CA
                # and played on. What actually happens here is that we decline to pass
                # the connection through; whether it dies at the handshake or at the
                # 403 in request() depends on the client, and only the latter is ours.
                print(f"[BLOCK] intercepting {sni} ({window_label()})", flush=True)
            else:
                data.ignore_connection = True     # outside the window: pass through unread
        except Exception as e:
            # Never let a decoration on the policy break the proxy for everything else.
            _note_error("tls_clienthello", e)

    def client_connected(self, client):
        """Record that a device's traffic reached the box -- see note_client()."""
        try:
            note_client((client.peername or ("",))[0], time.time())
        except Exception as e:
            _note_error("client_connected", e)

    def running(self):
        """Mark where this proxy's error count started.

        proxy_errors is a Redis counter with no TTL that nothing has ever reset, so it is
        a LIFETIME total -- and /health printed it as "since boot", which was simply
        false. Worse, a lifetime total cannot answer the only question worth asking of an
        error count: is it still going up? Five errors accumulated over two months and
        five in the last hour render identically.

        Recording the value at startup lets /health show both: how many since this proxy
        came up (the actionable number) and the lifetime total (the context). Wrapped,
        because a failure here must not stop the proxy starting -- if the base is missing
        the page says so rather than guessing.
        """
        _try_redis(lambda: r.set("proxy_errors_base", r.get("proxy_errors") or 0))

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
        # IGNORED_HOSTS is consulted HERE too, not only in request(). It was enforced at
        # one hook and both response hooks keyed off site_for_host() alone -- and
        # api.puzzmo.com / cdn.puzzmo.com are subdomains of a gated domain, so
        # site_for_host matches them. Their responses were therefore buffered
        # (stream = False), CSP-amended and script-injected despite being listed as
        # exempt. F18 fixed how this list MATCHES; this is where it is asked.
        if host_matches(host, IGNORED_HOSTS):
            return
        site = site_for_host(host)
        if site:
            # Instagram: like Facebook below, buffer ONLY the HTML document we inject into.
            # Meta's graphql streams its responses; stream = False on those hangs the page.
            if SITES[site].get("stream_non_html") and \
               "text/html" not in flow.response.headers.get("content-type", ""):
                flow.response.stream = True
                return
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

        # Refuse blocked hosts HERE, in plaintext, rather than trusting the client to
        # reject the certificate we mint for them.
        #
        # The original design leaned entirely on the handshake failing: the CA carries
        # name constraints that do not permit these domains, so the leaf is unusable and
        # the client hangs up. That is a real control and it still runs -- but it is the
        # CLIENT'S control, and the client is not ours. The zombsroyale iOS app accepts
        # the forged certificate without complaint: it does not pin, and it honours a
        # user-installed CA. So the proxy dutifully logged "[BLOCK] refused by policy"
        # while decrypting and forwarding /api/config, the Socket.IO gateway, and every
        # round of gameplay. The log line asserted an outcome it never checked.
        #
        # Reaching this code at all therefore means the client trusted us, which is
        # exactly the case the certificate mechanism cannot cover. The two are
        # complementary and both are needed: pinning clients die at the handshake,
        # credulous ones die here.
        if blocked_now(host):
            print(f"[BLOCK] {host}{path[:60]} refused in-band ({window_label()})",
                  flush=True)
            flow.response = http.Response.make(
                403, b'{"error":"blocked by policy"}',
                {"Content-Type": "application/json", "Cache-Control": "no-store"})
            return

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

            # CSRF: the mutating endpoints (/enter, /heartbeat, /reflect) are
            # driven same-origin from the gate page / injected script. A forged request
            # from another site the user is visiting must be refused, so a malicious page
            # can't drive the budget state (or the return redirect).
            # The method check still covers GET, and deliberately so. It was written for
            # /exit, which was GET-reachable and is now gone -- but the rule is about the
            # SET, not that member: any future endpoint that changes state via GET is
            # covered the day it is added, rather than the day someone remembers this.
            if (flow.request.method != "GET" or sub in STATE_CHANGING) and \
               _is_cross_origin(flow):
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

        # Free paths (Instagram's DMs, your own profile, /accounts) pass before anything
        # that meters or gates them, and that ordering is the whole design:
        #   · no gate, session or not -- DMs work at zero budget, and login is never gated;
        #   · no shadow note -- the heartbeat does not charge them, so passive use with no
        #     heartbeat to match is exactly what enforcement_looks_dead() alarms on;
        #   · no navigation mark -- /health would read "browsing, nothing charged";
        #   · no regular-profile block -- "DMs are free" holds on every profile.
        # Sub-requests are judged by the page that made them (request_is_free), so the
        # FEED's API calls still get the 503 below with no session. That is what holds the
        # gate when the injected script does not run -- a service worker serving a cached
        # shell, say -- rather than leaving it to the script alone.
        if request_is_free(site, flow):
            return

        # SHADOW METER — the dead-man's switch's second clock (see shadow_note).
        # Observational: it never reads or writes budget state. Removing this line blinds
        # enforcement_looks_dead(); CLAUDE.md says what has to go with it.
        shadow_note(site, time.time())

        user_agent = flow.request.headers.get("User-Agent", "")
        is_regular_profile = "regular-profile" in user_agent

        # Regular (desktop) profile — flat block.
        if is_regular_profile:
            flow.metadata["cooldown_synth"] = True
            flow.response = http.Response.make(
                200,
                b"""<html><body style='font-family:sans-serif;text-align:center;margin-top:20vh;background:#1a1a1a;color:white'>
                <h1>Blocked </h1>
                <p>Use your budgeted profile if you really need it.</p>
                </body></html>""",
                {"Content-Type": "text/html"}
            )
            return

        # FAIL CLOSED. This is the read the gate is decided on, and it was the one
        # Redis call in this file not wrapped -- every other goes through _try_redis
        # (the docstring even states the principle, for the shadow meter). session_mode
        # is two bare r.get()s, so a dead Redis raised straight out of this hook, into
        # mitmproxy's addon wrapper, which logs and forwards the flow. Redis unhealthy
        # therefore meant every gated site open, silently, for as long as it lasted --
        # and invisibly, because _note_error is downstream of here so proxy_errors never
        # moved and /health stayed green. enforcement_looks_dead() cannot catch it
        # either; it reads Redis to decide.
        #
        # Which failures actually reach here, measured on a live deployment 2026-08-15
        # rather than assumed. NOT a Redis restart: if the app unit has
        # Requires=redis-server.service and the proxy Requires= the app, a restart cycles
        # all three together and there is never a live proxy with a dead Redis. What
        # reaches here is Redis alive but ERRORING: maxclients, an OOM it survives, or the
        # socket timeout below firing under load. F12/F20's failure -- the clock stops
        # while browsing continues -- by a narrower route than first thought, but a real one.
        #
        # Treating the failure as "no session" serves the gate. That is the safe
        # direction: a tool that blocks Reddit while its state store is broken still
        # works; one that opens it does not.
        try:
            mode = session_mode(site)
        except Exception as exc:
            _note_error("request/session_mode", exc)
            mode = None
        is_navigation = _is_navigation(flow)

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
            # response() runs for this synthesized page too (mitmproxy fires it for a
            # response set in request()), and for Instagram it injects regardless of
            # session -- so say what this is, or the gate would carry a heartbeat.
            flow.metadata["cooldown_synth"] = True
            try:
                # Pass the original URL so the gate's Enter button can return the user
                # to the link they clicked, not just the site home.
                nxt = quote(flow.request.pretty_url, safe="")
                resp = req.get(f"http://127.0.0.1:5000/budget?site={site}&next={nxt}", timeout=2)
                # Check the STATUS, not just that a response came back. Flask returning
                # 500 -- which is what it does when Redis is unreachable -- was being
                # relabelled 200 and its raw Werkzeug error page served as the gate, on a
                # gated origin. Safe, in that the real site is still not reached, but it
                # shows a stack-trace-shaped page where a sentence belongs, and calls it
                # success. The fallback below already says the right thing.
                if resp.status_code != 200:
                    raise RuntimeError(f"budget page returned {resp.status_code}")
                # SAME headers as the /budget path fifty lines up. This branch rebuilt the
                # response with only a Content-Type, so Flask's after_request hardening
                # (X-Frame-Options, frame-ancestors, Referrer-Policy) was attached and then
                # thrown away, and no Cache-Control was added.
                #
                # Framing is the cost. Because the proxy synthesises this response, the real
                # site's own X-Frame-Options and CSP never arrive either, so
                # <iframe src="https://www.reddit.com/"> renders your gate on any page once
                # the pool is drained -- an iframe sends Sec-Fetch-Mode: navigate, which this
                # path counts as a navigation. Cross-origin, so the attacker cannot read it;
                # they can clickjack Enter (which posts same-origin, so the CSRF check
                # correctly does not fire) and use it as an out-of-budget oracle.
                # The gate also carries feed_token in <meta name="cd-tok">, and app.py's
                # comment claims "the token-bearing gate is served through it and is still
                # no-store (addon.py)" -- which was false here specifically.
                flow.response = http.Response.make(
                    200, resp.content, _GATE_HEADERS
                )
            except Exception:
                flow.response = http.Response.make(
                    200, b"Budget server unreachable", _GATE_HEADERS
                )
        else:
            flow.response = http.Response.make(503, b"", {})
        return

    def response(self, flow: http.HTTPFlow):
        # Inject the visibility-aware heartbeat into pages of a budgeted site (so only
        # foreground time is charged), OR the frosted "tap to reveal" overlay into
        # Facebook (no budget — decrypted for injection only).
        host = flow.request.pretty_host
        if host_matches(host, IGNORED_HOSTS):
            return                      # exempt: never inject into assets or APIs
        site = site_for_host(host)
        ov = None if site else overlay_for_host(host)
        if not site and not ov:
            return
        # EXACT match, like the request-side check. startswith("/budget") also swallowed a
        # genuine page at /budget-something on a gated site -- served with no heartbeat, so
        # uncharged -- and several news domains do run a /budget section. The request side
        # deliberately requires an exact match; these two disagreeing is the whole bug.
        if flow.request.path == "/budget" or flow.request.path.startswith("/budget/"):
            return  # don't inject into the budget/enter pages themselves

        if "text/html" not in flow.response.headers.get("content-type", ""):
            return
        if flow.metadata.get("cooldown_synth"):
            return                      # our own gate / block page, not the site's
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
            # Only inject during a live session.
            # Wrapped for the same reason as the request-side read, but quieter: the
            # outcome on failure (no injection) is identical to the outcome this branch
            # already produces for "no session", and request() has by then both failed
            # closed and logged. A second log line per response would only add noise.
            mode = _try_redis(lambda: session_mode(site))
            # A site budgeted by path is injected on every page, session or not: its free
            # pages carry no session, and one first loaded on /direct/ with no guard would
            # make the SPA hop to Home free. The script is what notices that hop.
            segs = free_segments(site)
            if mode is None and segs is None:
                return
            injection = HEARTBEAT_SCRIPT.replace("__SITE__", site)
            # null, not [], leaves every path budgeted.
            injection = injection.replace(
                "__FREE__", json.dumps(list(segs)) if segs is not None else "null")
            # Prepended, not appended, and outside the nonce rewrite below: it must be
            # the first thing in the injection and must not depend on scripts running.
            if _try_redis(enforcement_looks_dead, False):
                injection = ENFORCEMENT_WARNING + injection
            # Instagram registers a service worker too, which would serve pages past the
            # injection -- and here that is a hole in the DM-to-feed guard, not just a
            # missed charge.
            if site in ("youtube", "reddit", "instagram"):
                injection += SW_KILL
            if site == "youtube":
                injection += YOUTUBE_DECLUTTER
            if site == "instagram":
                injection += INSTAGRAM_DECLUTTER
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
