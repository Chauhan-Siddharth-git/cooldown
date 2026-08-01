"""The mitmproxy addon: which hosts get gated/decrypted, CSP stripping and streaming
decisions, the request gate (block / study lock / pass-through / CSRF), and what gets
injected into a page. These are the interception layer's security boundaries — getting
host matching or the study lock wrong silently un-gates a site.
"""
import os
import sys

import pytest
import redis
from mitmproxy.test import tflow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import addon  # noqa: E402


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
    (f"/watch?list={addon.STUDY_PLAYLISTS[0]}", True),
    (f"/playlist?list={addon.STUDY_PLAYLISTS[0]}", True),
    (f"/watch?v=abc&list={addon.STUDY_PLAYLISTS[0]}&index=2", True),
    ("/watch?v=abc", False),                       # a video with no playlist
    ("/watch?list=PLsomeotherplaylist", False),    # someone else's playlist
    ("/feed/subscriptions", False),
    ("/results?search_query=cats", False),
    ("/", False),
    ("/shorts/abc", False),
])
def test_study_url_allowed(path, allowed):
    assert addon.study_url_allowed(path) is allowed


def test_session_mode_reads_redis(rdb, session):
    assert addon.session_mode("reddit") is None
    session("reddit", "active")
    assert addon.session_mode("reddit") == "active"
    rdb.delete("session:tok-reddit")          # session expired, token left behind
    assert addon.session_mode("reddit") is None


# ---------- responseheaders: CSP stripping + streaming ----------

def test_gated_site_is_buffered_and_csp_stripped():
    f = mkflow("www.reddit.com")
    f.response.headers["content-security-policy"] = "default-src 'self'"
    f.response.headers["content-security-policy-report-only"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(f)
    assert f.response.stream is False                      # must buffer to inject
    assert "content-security-policy" not in f.response.headers
    assert "content-security-policy-report-only" not in f.response.headers


def test_facebook_html_buffered_but_realtime_streams():
    """Buffering Facebook's never-ending realtime responses hangs the page."""
    doc = mkflow("www.facebook.com", ctype="text/html; charset=utf-8")
    doc.response.headers["content-security-policy"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(doc)
    assert doc.response.stream is False
    assert "content-security-policy" not in doc.response.headers

    rt = mkflow("www.facebook.com", ctype="application/json")
    addon.BudgetAddon().responseheaders(rt)
    assert rt.response.stream is True


def test_unrelated_host_is_untouched():
    f = mkflow("example.com")
    f.response.headers["content-security-policy"] = "default-src 'self'"
    addon.BudgetAddon().responseheaders(f)
    assert "content-security-policy" in f.response.headers   # not our business


# ---------- request hook: the gate ----------

def test_no_session_navigation_serves_the_gate(rdb, monkeypatch):
    class FakeResp:
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


def test_study_mode_bounces_off_course_navigation(rdb, session):
    session("youtube", "study")
    f = mkflow("www.youtube.com", "/feed/subscriptions", resp=False,
               headers={"Sec-Fetch-Mode": "navigate"})
    addon.BudgetAddon().request(f)
    assert f.response.status_code == 302
    assert addon.STUDY_PLAYLISTS[0] in f.response.headers["Location"]


def test_study_mode_allows_the_course_and_its_subrequests(rdb, session):
    session("youtube", "study")
    ok = mkflow("www.youtube.com", f"/watch?list={addon.STUDY_PLAYLISTS[0]}",
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


def test_youtube_gets_declutter_and_study_lock(rdb, session):
    session("youtube", "active")
    f = mkflow("www.youtube.com", "/", body=HTML)
    addon.BudgetAddon().response(f)
    assert "bp-yt-declutter" in f.response.text          # Shorts/feed surgery
    assert "serviceWorker" in f.response.text            # SW_KILL

    session("youtube", "study")
    s = mkflow("www.youtube.com", "/", body=HTML)
    addon.BudgetAddon().response(s)
    assert addon.STUDY_PLAYLISTS[0] in s.response.text   # STUDY_LOCK carries the allowlist


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
