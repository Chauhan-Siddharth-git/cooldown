"""The mitmproxy addon: which hosts get gated/decrypted, CSP stripping and streaming
decisions, the request gate (block / study lock / pass-through / CSRF), and what gets
injected into a page. These are the interception layer's security boundaries — getting
host matching or the study lock wrong silently un-gates a site.
"""
import os
import sys
import time

import pytest
import redis
from mitmproxy.test import tflow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import addon  # noqa: E402

# Study mode ships OFF (STUDY_PLAYLISTS empty), but the lock still exists for anyone who
# enables it — so these tests configure a playlist rather than depend on the shipped default.
STUDY_PL = "PLtest0000study0000playlist"

@pytest.fixture()
def study_on(monkeypatch):
    monkeypatch.setattr(addon, "STUDY_PLAYLISTS", [STUDY_PL])


@pytest.fixture()
def rdb(monkeypatch):
    r = redis.Redis(host="localhost", port=6379, db=15, decode_responses=True)
    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        pytest.skip("needs a local redis (tests use db 15)")
    r.flushdb()
    monkeypatch.setattr(addon, "r", r)
    yield r
    r.flushdb()


@pytest.fixture()
def session(rdb):
    """Give a site a live session in the given mode."""
    def _mk(site, mode="active"):
        rdb.set(f"active_token:{site}", "tok-" + site)
        rdb.set(f"session:tok-{site}", mode)
    return _mk


def mkflow(host, path="/", resp=True, ctype="text/html", body=None, headers=None, method="GET"):
    f = tflow.tflow(resp=resp)
    f.request.host = host
    f.request.path = path
    f.request.method = method
    for k, v in (headers or {}).items():
        f.request.headers[k] = v
    if resp:
        f.response.headers["content-type"] = ctype
        if body is not None:
            f.response.text = body
    return f


# ---------- host matching (suffix, never substring) ----------

@pytest.fixture(autouse=True)
def _no_ambient_forwarding(monkeypatch):
    """1.7: these tests used to reach whatever was really listening on 127.0.0.1:5000.

    With nothing there they got a connection error and passed; with a real service there
    they could fail, or pass for the wrong reason. A leftover harness process caused a
    failure during one review that looked exactly like a code regression, and the same
    tests would answer differently when run on the box itself.

    Forwarding is stubbed to raise the same connection error the empty case produced, so
    the outcome is identical but no longer depends on what else is running. A test that
    needs a real response monkeypatches req.get itself inside the test body, which takes
    precedence over this.
    """
    def refuse(url, *a, **k):
        raise addon.req.exceptions.ConnectionError(
            f"ambient forwarding blocked in tests: {url}. Stub addon.req.get/post in the "
            f"test if it needs a response.")
    monkeypatch.setattr(addon.req, "get", refuse)
    monkeypatch.setattr(addon.req, "post", refuse)


@pytest.mark.parametrize("host,expect", [
    ("reddit.com", "reddit"),
    ("www.reddit.com", "reddit"),
    ("old.reddit.com", "reddit"),
    ("WWW.REDDIT.COM", "reddit"),          # case-insensitive
    ("www.reddit.com:443", "reddit"),      # port stripped
    ("www.youtube.com", "youtube"),
    ("m.youtube.com", "youtube"),
    ("open.spotify.com", "spotify"),
    ("www.cnn.com", "news"),               # from news_domains
    # --- must NOT match: a substring check would gate AND decrypt these ---
    ("evil-reddit.com", None),
    ("reddit.com.attacker.io", None),
    ("notreddit.com", None),
    ("reddit.com.evil.co.uk", None),
    ("example.com", None),
    ("", None),
])
def test_site_for_host_is_suffix_match(host, expect):
    assert addon.site_for_host(host) == expect


@pytest.mark.parametrize("host,matched", [
    ("www.facebook.com", True),
    ("web.facebook.com", True),
    ("m.facebook.com", True),
    ("mbasic.facebook.com", True),
    # Bare facebook.com and Messenger's realtime hosts must NOT be decrypted: they pin
    # their cert, and intercepting them broke the Messenger app (regression guard).
    ("facebook.com", False),
    ("edge-chat.facebook.com", False),
    ("graph.facebook.com", False),
    ("gateway.facebook.com", False),
    ("messenger.com", False),
])
def test_overlay_host_matching_excludes_messenger(host, matched):
    assert (addon.overlay_for_host(host) is not None) is matched


def test_facebook_is_overlay_only_never_budgeted():
    """Facebook is decrypted for injection, but must never draw from the time budget."""
    assert addon.site_for_host("www.facebook.com") is None
    assert addon.overlay_for_host("www.facebook.com") is not None


# ---------- study mode is locked to the course ----------

@pytest.mark.parametrize("path,allowed", [
    (f"/watch?list={STUDY_PL}", True),
    (f"/playlist?list={STUDY_PL}", True),
    (f"/watch?v=abc&list={STUDY_PL}&index=2", True),
    ("/watch?v=abc", False),                       # a video with no playlist
    ("/watch?list=PLsomeotherplaylist", False),    # someone else's playlist
    ("/feed/subscriptions", False),
    ("/results?search_query=cats", False),
    ("/", False),
    ("/shorts/abc", False),
])
def test_study_url_allowed(study_on, path, allowed):
    assert addon.study_url_allowed(path) is allowed


def test_session_mode_reads_redis(rdb, session):
    assert addon.session_mode("reddit") is None
    session("reddit", "active")
    assert addon.session_mode("reddit") == "active"
    rdb.delete("session:tok-reddit")          # session expired, token left behind
    assert addon.session_mode("reddit") is None


# ---------- responseheaders: CSP stripping + streaming ----------

def test_gated_site_is_buffered_and_csp_amended():
    f = mkflow("www.reddit.com")
    f.response.headers["content-security-policy"] = "default-src 'self'"
    f.response.headers["content-security-policy-report-only"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(f)
    assert f.response.stream is False                      # must buffer to inject
    # The policy stays — amended so our script may run, not thrown away.
    assert "default-src 'self'" in f.response.headers["content-security-policy"]
    assert "'nonce-" in f.response.headers["content-security-policy"]
    assert "content-security-policy-report-only" not in f.response.headers


def test_facebook_html_buffered_but_realtime_streams():
    """Buffering Facebook's never-ending realtime responses hangs the page."""
    doc = mkflow("www.facebook.com", ctype="text/html; charset=utf-8")
    doc.response.headers["content-security-policy"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(doc)
    assert doc.response.stream is False
    assert "'nonce-" in doc.response.headers["content-security-policy"]   # amended, not dropped

    rt = mkflow("www.facebook.com", ctype="application/json")
    addon.BudgetAddon().responseheaders(rt)
    assert rt.response.stream is True


# ---------- CSP is amended, not deleted ----------

CSP = "content-security-policy"

@pytest.mark.parametrize("policy,expect_change,why", [
    ("script-src 'self'", True, "nonce added to script-src"),
    ("default-src 'self'", True, "script-src synthesised from default-src"),
    ("frame-ancestors 'none'; script-src 'self'", True, "other directives survive"),
    ("script-src 'strict-dynamic' 'nonce-xyz'", True, "strict-dynamic works with a nonce"),
    ("script-src 'self' 'unsafe-inline' 'nonce-abc'", True, "a nonce is already in play"),
    # The dangerous one: adding a nonce here would switch OFF 'unsafe-inline' and break
    # the site's own inline scripts. Our script is already allowed, so leave it be.
    ("script-src 'self' 'unsafe-inline'", False, "unsafe-inline already permits us"),
    ("img-src 'self'", False, "nothing constrains scripts"),
])
def test_csp_amendment_rules(policy, expect_change, why):
    out = addon.BudgetAddon._csp_with_nonce(policy, "TESTNONCE")
    assert (out != policy) is expect_change, why
    if expect_change:
        assert "'nonce-TESTNONCE'" in out
        # everything the site asked for is still there
        for directive in policy.split(";"):
            head = directive.strip().split()[0]
            assert head in out, f"lost {head}"


def test_csp_survives_with_a_nonce_end_to_end(rdb, session):
    """The policy stays enforced, and the injected script carries the matching nonce."""
    session("reddit", "active")
    f = mkflow("www.reddit.com", "/r/x", body="<html><body>hi</body></html>")
    f.response.headers[CSP] = "default-src 'self'; frame-ancestors 'none'"
    a = addon.BudgetAddon()
    a.responseheaders(f)
    assert CSP in f.response.headers                       # NOT deleted any more
    policy = f.response.headers[CSP]
    assert "frame-ancestors 'none'" in policy              # protection preserved
    nonce = f.metadata["cooldown_nonce"]
    assert f"'nonce-{nonce}'" in policy
    a.response(f)
    assert f'<script nonce="{nonce}">' in f.response.text  # the browser will run it


def test_report_only_csp_is_dropped(rdb):
    """It blocks nothing, but would report our injection back to the site."""
    f = mkflow("www.reddit.com", ctype="text/html")
    f.response.headers["content-security-policy-report-only"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(f)
    assert "content-security-policy-report-only" not in f.response.headers


def test_gated_site_keeps_csp_on_non_html():
    """We only inject into HTML, so everything else keeps its policy — stripping CSP
    from JSON/JS bought nothing and only widened the blast radius of a site-side XSS."""
    for ctype in ("application/json", "application/javascript", "text/css", "image/png"):
        f = mkflow("www.reddit.com", "/api/thing", ctype=ctype)
        f.response.headers["content-security-policy"] = "default-src 'self'"
        addon.BudgetAddon().responseheaders(f)
        assert f.response.headers["content-security-policy"] == "default-src 'self'", ctype


def test_other_security_headers_are_never_touched():
    """Only CSP is ever modified — transport and framing protections pass through."""
    f = mkflow("www.reddit.com", ctype="text/html")
    keep = {"strict-transport-security": "max-age=63072000",
            "x-frame-options": "SAMEORIGIN",
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer"}
    for k, v in keep.items():
        f.response.headers[k] = v
    f.response.headers["content-security-policy"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(f)
    assert "'nonce-" in f.response.headers["content-security-policy"]  # amended in place
    for k, v in keep.items():
        assert f.response.headers[k] == v, k                     # everything else intact


def test_unrelated_host_is_untouched():
    f = mkflow("example.com")
    f.response.headers["content-security-policy"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(f)
    assert "content-security-policy" in f.response.headers   # not our business


# ---------- request hook: the gate ----------

def test_no_session_navigation_serves_the_gate(rdb, monkeypatch):
    class FakeResp:
        # status_code included deliberately: the addon checks it now, and a stub that
        # omits it silently exercised the error path instead of the success path.
        status_code = 200
        content = b"<html>GATE</html>"
    seen = {}
    def fake_get(url, timeout=None):
        seen["url"] = url
        return FakeResp()
    monkeypatch.setattr(addon.req, "get", fake_get)

    f = mkflow("www.reddit.com", "/r/python", resp=False,
               headers={"Sec-Fetch-Mode": "navigate"})
    addon.BudgetAddon().request(f)
    assert f.response.status_code == 200
    assert b"GATE" in f.response.content
    assert "site=reddit" in seen["url"]
    assert "next=" in seen["url"]            # so Enter returns to the clicked link


def test_a_failing_budget_page_is_not_served_as_the_gate(rdb, monkeypatch):
    """Flask answering 500 must produce the fallback, not Flask's error page relabelled 200.

    When Redis is unreachable the loopback /budget call returns 500, and the addon used to
    copy `resp.content` through with a hardcoded 200 -- so a gated origin served a raw
    Werkzeug error page and called it success. The real site is still never reached, so
    this is presentation rather than a bypass, but "500 body, 200 status" is a lie the
    browser has no way to see through.
    """
    class Failing:
        status_code = 500
        content = b"<html><h1>500 Internal Server Error</h1></html>"
    monkeypatch.setattr(addon.req, "get", lambda url, timeout=None: Failing())

    f = mkflow("www.reddit.com", "/r/python", resp=False,
               headers={"Sec-Fetch-Mode": "navigate"})
    addon.BudgetAddon().request(f)

    assert f.response.status_code == 200
    assert b"unreachable" in f.response.content
    assert b"500 Internal Server Error" not in f.response.content


def test_no_session_subrequest_is_dropped_quietly(rdb):
    f = mkflow("www.reddit.com", "/api/thing", resp=False)   # no navigate header
    addon.BudgetAddon().request(f)
    assert f.response.status_code == 503


def test_active_session_passes_through(rdb, session):
    session("reddit", "active")
    f = mkflow("www.reddit.com", "/r/python", resp=False,
               headers={"Sec-Fetch-Mode": "navigate"})
    addon.BudgetAddon().request(f)
    assert f.response is None                 # untouched -> goes to the real site


def test_study_mode_bounces_off_course_navigation(rdb, session, study_on):
    session("youtube", "study")
    f = mkflow("www.youtube.com", "/feed/subscriptions", resp=False,
               headers={"Sec-Fetch-Mode": "navigate"})
    addon.BudgetAddon().request(f)
    assert f.response.status_code == 302
    assert STUDY_PL in f.response.headers["Location"]


def test_study_mode_allows_the_course_and_its_subrequests(rdb, session, study_on):
    session("youtube", "study")
    ok = mkflow("www.youtube.com", f"/watch?list={STUDY_PL}",
                resp=False, headers={"Sec-Fetch-Mode": "navigate"})
    addon.BudgetAddon().request(ok)
    assert ok.response is None

    sub = mkflow("www.youtube.com", "/youtubei/v1/player", resp=False)   # not a navigation
    addon.BudgetAddon().request(sub)
    assert sub.response is None               # sub-requests must pass or the page breaks


def test_regular_profile_is_flat_blocked(rdb):
    f = mkflow("www.reddit.com", "/", resp=False,
               headers={"User-Agent": "Mozilla/5.0 regular-profile"})
    addon.BudgetAddon().request(f)
    assert f.response.status_code == 200
    assert b"Blocked" in f.response.content


def test_cross_site_post_to_budget_is_rejected(rdb):
    """CSRF: a forged POST from another site must not drive budget state."""
    f = mkflow("www.reddit.com", "/budget/enter?site=reddit", resp=False, method="POST",
               headers={"Sec-Fetch-Site": "cross-site"})
    addon.BudgetAddon().request(f)
    assert f.response.status_code == 403


# ---------- monitoring pages are not readable by page scripts ----------

def _probe(path, headers, method="GET"):
    f = mkflow("www.reddit.com", path, resp=False, headers=headers, method=method)
    addon.BudgetAddon().request(f)
    return f.response.status_code if f.response else None      # None = allowed through

SCRIPT = {"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"}   # what fetch() sends
NAV = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"}
IFRAME = {"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate"}   # a navigation a script CAN cause

@pytest.mark.parametrize("path", [
    "/budget/devices?fmt=json", "/budget/stats", "/budget/health?fmt=json",
    "/budget/remaining", "/budget/boot-ack",
])
@pytest.mark.parametrize("hdrs", [SCRIPT, NAV, IFRAME])
def test_monitoring_pages_are_not_served_on_a_gated_origin(rdb, path, hdrs):
    """They live on the box's own origin now. However the request is made — script,
    navigation or iframe — this origin must never return their content, so there is
    nothing left for a same-origin script to read."""
    rdb.set("monitor_origin", "http://100.64.0.1:5000")
    assert _probe(path, hdrs) == 302

@pytest.mark.parametrize("path", ["/budget/devices", "/budget/stats", "/budget/health"])
def test_moved_pages_redirect_to_the_box(rdb, path):
    """A stale bookmark should land on the dashboard, not a dead end. Following the
    redirect puts the browser cross-origin, which is the whole point."""
    rdb.set("monitor_origin", "http://100.64.0.1:5000")
    f = mkflow("www.reddit.com", path, resp=False, headers=NAV)
    addon.BudgetAddon().request(f)
    assert f.response.status_code == 302
    assert f.response.headers["Location"] == "http://100.64.0.1:5000" + path[len("/budget"):]

def test_moved_pages_404_when_the_box_origin_is_unknown(rdb):
    """No tailnet address -> no origin to send them to. Still must not serve content."""
    rdb.delete("monitor_origin")
    assert _probe("/budget/devices", NAV) == 404

def test_our_own_pages_poll_with_the_token(rdb):
    rdb.set("feed_token", "SEKRIT")
    assert _probe("/budget/feed?t=SEKRIT", SCRIPT) != 403
    assert _probe("/budget/feed?t=WRONG", SCRIPT) == 403
    assert _probe("/budget/feed", SCRIPT) == 403

def test_missing_token_does_not_open_the_door(rdb):
    """If no token is set, an empty ?t= must still be rejected, not treated as a match."""
    rdb.delete("ui_token", "feed_token")
    assert _probe("/budget/feed?t=", SCRIPT) == 403

def test_gate_and_heartbeat_stay_reachable(rdb):
    """The gate must render in place of a site, and the injected heartbeat runs on the
    site's own pages — neither is a navigation to a monitoring page."""
    assert _probe("/budget?site=reddit", NAV) != 403
    assert _probe("/budget/heartbeat?site=reddit", SCRIPT, method="POST") != 403


# ---------- response hook: what gets injected ----------

HTML = "<html><body>page</body></html>"

def test_heartbeat_injected_with_the_right_site(rdb, session):
    session("reddit", "active")
    on = mkflow("www.reddit.com", "/r/x", body=HTML)
    addon.BudgetAddon().response(on)
    text = on.response.text
    assert "/budget/heartbeat" in text
    assert 'var SITE = "reddit"' in text          # charged against the right budget
    assert "__SITE__" not in text                 # placeholder actually substituted
    assert text.index("<body>") < text.index("/budget/heartbeat") < text.index("</body>")


def test_no_injection_without_session(rdb):
    f = mkflow("www.reddit.com", "/r/x", body=HTML)
    addon.BudgetAddon().response(f)
    assert f.response.text == HTML            # untouched


def test_youtube_gets_declutter_and_study_lock(rdb, session, study_on):
    session("youtube", "active")
    f = mkflow("www.youtube.com", "/", body=HTML)
    addon.BudgetAddon().response(f)
    assert "bp-yt-declutter" in f.response.text          # Shorts/feed surgery
    assert "serviceWorker" in f.response.text            # SW_KILL

    session("youtube", "study")
    s = mkflow("www.youtube.com", "/", body=HTML)
    addon.BudgetAddon().response(s)
    assert STUDY_PL in s.response.text   # STUDY_LOCK carries the allowlist


def test_facebook_gets_overlay_but_never_the_heartbeat(rdb):
    f = mkflow("www.facebook.com", "/", body=HTML)
    addon.BudgetAddon().response(f)
    assert "cd-fb-cover" in f.response.text              # the feed block
    assert "/budget/heartbeat" not in f.response.text    # no budget, no charging


def test_injection_falls_back_when_no_body_tag(rdb, session):
    """Mobile YouTube ships no </body>; the injection must still land."""
    session("youtube", "active")
    f = mkflow("m.youtube.com", "/", body="<html>no body tag</html>")
    addon.BudgetAddon().response(f)
    text = f.response.text
    assert "/budget/heartbeat" in text
    assert text.rstrip().endswith("</html>")                  # injected BEFORE </html>
    assert text.index("/budget/heartbeat") < text.index("</html>")


def test_non_html_and_budget_pages_are_not_injected(rdb, session):
    session("reddit", "active")
    js = mkflow("www.reddit.com", "/app.js", ctype="application/javascript", body="var a=1;")
    addon.BudgetAddon().response(js)
    assert js.response.text == "var a=1;"

    gate = mkflow("www.reddit.com", "/budget", body=HTML)
    addon.BudgetAddon().response(gate)
    assert gate.response.text == HTML         # don't inject into the gate itself


def test_strict_dynamic_does_not_read_as_unsafe_inline_already_allowing_us():
    """1.5: CSP3 6.7.3.2 -- when the type is script and the source list contains
    'strict-dynamic', the algorithm returns "Does Not Allow" before it reaches the
    'unsafe-inline' branch. The spec lists "'unsafe-inline' 'strict-dynamic'" as an
    example of a list that does NOT allow inline script.

    So this policy used to be returned unchanged on the grounds that unsafe-inline
    already permitted us, and the heartbeat would have been blocked with nothing
    reporting it -- no heartbeat means no time charged while browsing continues.
    """
    f = addon.BudgetAddon._csp_with_nonce
    pol = "script-src 'unsafe-inline' 'strict-dynamic'"
    out = f(pol, "TESTNONCE")
    assert "'nonce-TESTNONCE'" in out, (
        f"strict-dynamic makes unsafe-inline inert for script; a nonce is required. got: {out}")

    # Unchanged where unsafe-inline genuinely does allow us -- otherwise the fix would
    # be satisfied by always adding a nonce, which breaks the site's own inline scripts.
    assert f("script-src 'unsafe-inline'", "N") == "script-src 'unsafe-inline'"
    # And still untouched when a nonce is already present.
    assert f("script-src 'nonce-abc'", "N").count("nonce-") == 2


# --- the time-windowed SNI block ------------------------------------------------------
# This block shipped with no tests at all, and the first thing it did in production was
# fail silently in the one way tests would have caught: the hostname matched, the window
# said no, and nothing was logged because passing through is the quiet path. The window
# had been picked from an assumption about when a phone game gets played (09:00-21:00);
# the observed session was 23:57.

def _at(hour):
    """Epoch seconds at a given local hour, on a fixed date."""
    return time.mktime((2026, 9, 9, hour, 0, 0, 0, 0, -1))


def test_block_covers_subdomains_but_not_lookalikes():
    """The app does not talk to the apex -- it talks to mason.zombsroyale.io. If this
    only matched the bare domain the block would never fire on the traffic that exists.
    The negative case is the F4 class: suffix matching, not substring.
    """
    for h in ("zombsroyale.io", "mason.zombsroyale.io", "a.b.zombsroyale.io"):
        assert addon.host_matches(h, addon.BLOCKED_HOSTS), h
    for h in ("notzombsroyale.io", "zombsroyale.io.evil.com", "zombsroyale.com"):
        assert not addon.host_matches(h, addon.BLOCKED_HOSTS), h


def test_shipped_window_covers_the_hour_it_was_actually_played():
    """The regression. 23:57 is the only timestamp here with evidence behind it."""
    assert addon.blocked_now("mason.zombsroyale.io", _at(23)), (
        "the observed play time is outside the shipped window -- the block cannot fire")
    for h in range(24):
        assert addon.blocked_now("mason.zombsroyale.io", _at(h)), h


def test_window_arithmetic_still_works_for_narrower_windows(monkeypatch):
    """0-24 exercises only one branch. The wrap-around case is the one that is easy to
    get backwards, and it stays reachable config, so it stays tested.
    """
    monkeypatch.setattr(addon, "BLOCK_FROM_HOUR", 9)
    monkeypatch.setattr(addon, "BLOCK_TO_HOUR", 21)
    assert addon.blocked_now("zombsroyale.io", _at(9))        # inclusive
    assert addon.blocked_now("zombsroyale.io", _at(20))
    assert not addon.blocked_now("zombsroyale.io", _at(21))   # exclusive
    assert not addon.blocked_now("zombsroyale.io", _at(23))

    monkeypatch.setattr(addon, "BLOCK_FROM_HOUR", 21)         # wraps midnight
    monkeypatch.setattr(addon, "BLOCK_TO_HOUR", 9)
    assert addon.blocked_now("zombsroyale.io", _at(23))
    assert addon.blocked_now("zombsroyale.io", _at(2))
    assert addon.blocked_now("zombsroyale.io", _at(8))
    assert not addon.blocked_now("zombsroyale.io", _at(14))


def test_unblocked_hosts_are_never_blocked_at_any_hour():
    for h in range(24):
        assert not addon.blocked_now("www.reddit.com", _at(h))
        assert not addon.blocked_now("", _at(h))


class _Hello:
    """Stands in for mitmproxy's ClientHelloData."""
    def __init__(self, sni):
        self.client_hello = type("CH", (), {"sni": sni})()
        self.ignore_connection = False


def test_tls_clienthello_only_ever_widens():
    """The property that makes this hook safe to have: no input causes it to intercept
    something it otherwise would not. It either passes traffic through untouched or
    leaves mitmproxy's own --allow-hosts decision alone.
    """
    a = addon.BudgetAddon()
    for sni in ("www.reddit.com", "example.com", "", None, "mason.zombsroyale.io"):
        d = _Hello(sni)
        a.tls_clienthello(d)
        assert d.ignore_connection in (True, False)
        if d.ignore_connection:
            assert addon.host_matches(sni, addon.BLOCKED_HOSTS), sni


def test_blocked_host_inside_the_window_is_left_to_fail_certificate_validation():
    """Inside the window the hook must NOT set ignore_connection -- the whole mechanism
    is that the proxy intercepts and the client refuses the cert it cannot chain.
    Passing through here would be a fail-open that looks identical in the journal.
    """
    d = _Hello("mason.zombsroyale.io")
    addon.BudgetAddon().tls_clienthello(d)
    assert not d.ignore_connection


def test_a_broken_hook_cannot_take_the_proxy_down():
    d = _Hello("mason.zombsroyale.io")
    del d.client_hello                     # whatever mitmproxy hands us, this must not raise
    addon.BudgetAddon().tls_clienthello(d)


def test_watch_sni_ships_empty():
    """It writes hostnames to the journal. It is a probe, not a control, and its question
    is answered; leaving it populated logs traffic for every device on the tailnet.
    """
    assert addon.WATCH_SNI == [], "the SNI probe is still on"


def test_window_label_reads_honestly():
    assert addon.window_label() == "all day"


def test_blocked_host_is_refused_in_band_not_left_to_the_client():
    """The one the original block got wrong. Every test written for it asserted proxy
    STATE -- that ignore_connection stayed False, that a log line appeared -- and every
    one passed while the game ran perfectly, because the actual enforcement had been
    delegated to a client that turned out to trust our CA and play on.

    So this asserts the outcome instead: a request that reaches request() at all came
    from a client that accepted the forged certificate, and it must not be answered.
    """
    for host, path in (("zombsroyale.io", "/api/config?platform=ios"),
                       ("mason-ipv4.zombsroyale.io", "/gateway/?EIO=4&transport=polling"),
                       ("mason.zombsroyale.io", "/")):
        f = mkflow(host, path, resp=False)
        addon.BudgetAddon().request(f)
        assert f.response is not None, f"{host}{path} was forwarded upstream"
        assert f.response.status_code == 403, host

    # And the check is specific: it must not swallow anything else.
    ok = mkflow("www.reddit.com", "/", resp=False)
    addon.BudgetAddon().request(ok)
    assert ok.response is None or ok.response.status_code != 403


def test_block_does_not_fire_outside_the_window(monkeypatch):
    """The 403 is inside the same clock as everything else."""
    monkeypatch.setattr(addon, "BLOCK_FROM_HOUR", 9)
    monkeypatch.setattr(addon, "BLOCK_TO_HOUR", 10)
    monkeypatch.setattr(addon, "blocked_now", lambda h, now=None: False)
    f = mkflow("zombsroyale.io", "/api/config", resp=False)
    addon.BudgetAddon().request(f)
    assert f.response is None or f.response.status_code != 403


def test_the_time_warning_does_not_cover_the_top_of_the_page():
    """It used to be position:fixed;top:0;left:0;right:0 -- a full-width band sitting
    exactly where YouTube's and Reddit's search bars are. pointer-events:none meant it
    never blocked a click, only the sight of what you were clicking, which in practice is
    the same complaint. Both the last-minute warning and the persistent wind-down ribbon
    did it; the wind-down one for the whole tapering period.
    """
    js = addon.HEARTBEAT_SCRIPT
    for el in ("#bp-timewarn", "#bp-winddown"):
        i = js.index(el + "{")
        rule = js[i:js.index("}", i)]
        assert "top:0" not in rule, f"{el} is anchored to the top of the viewport again"
        assert "pointer-events:none" in rule, el

    # The warning is a frame; the readable part is a corner pill.
    assert "inset:0" in js[js.index("#bp-timewarn{"):js.index("}", js.index("#bp-timewarn{"))]
    assert "#bp-timepill{" in js

    # Escalation exists and animates opacity only -- animating the box-shadow repaints the
    # whole viewport each frame, which is what made the frost overlay unusable.
    assert "bp-urgent" in js and "@keyframes bp-pulse" in js
    kf = js[js.index("@keyframes bp-pulse"):]
    kf = kf[:kf.index("}}") + 2]
    assert "opacity" in kf and "box-shadow" not in kf, kf

    # Non-ASCII must be \u-escaped: this is injected into pages whose charset we do not
    # control, and a literal glyph mojibakes on any of them that is not UTF-8.
    body = js[js.index("var WARN_AT"):js.index("function hideWd()")]
    assert all(ord(c) < 128 for c in body), "literal non-ASCII in the injected script"
