from mitmproxy import http
from urllib.parse import urlsplit, parse_qs, quote
import json
import redis
import requests as req

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
STUDY_PLAYLISTS = ["REPLACE_WITH_YOUR_PLAYLIST_ID"]  # allow-listed YouTube playlist IDs for Study mode; [] disables it

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

# Injected into real pages of a gated site. Pings the budget server only while the
# tab is actually visible, so only foreground viewing time is charged. The site is
# baked in at injection time (__SITE__) so the server charges the right budget. A
# 403 means the budget is spent / cooldown started -> reload to land on the gate.
HEARTBEAT_SCRIPT = """
<script>
(function () {
  var INTERVAL = 10000;  // ms between pings while the tab is visible
  function ping() {
    if (document.visibilityState !== "visible") return;
    fetch("/budget/heartbeat?site=__SITE__&_=" + Date.now(), { method: "POST", cache: "no-store", keepalive: true })
      .then(function (res) { if (res.status === 403) window.location.reload(); })
      .catch(function () {});
  }
  setInterval(ping, INTERVAL);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") ping();
  });
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
        # Inject the visibility-aware heartbeat into real pages of a gated site so
        # that only foreground viewing time is charged against that site's budget.
        host = flow.request.pretty_host
        site = site_for_host(host)
        if not site:
            return
        if flow.request.path.startswith("/budget"):
            return  # don't inject into the budget/enter pages themselves

        # Only inject during a live session (budgeted or study).
        mode = session_mode(site)
        if mode is None:
            return

        if "text/html" not in flow.response.headers.get("content-type", ""):
            return
        try:
            body = flow.response.get_text(strict=False)
        except Exception:
            return
        if not body:
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
