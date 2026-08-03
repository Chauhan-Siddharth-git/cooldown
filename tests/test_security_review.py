"""Regressions for the findings of the 2026-08 review.

Each test fails against the code as it was before the fix, so they pin the behaviour
rather than just describing it.
"""
import os
import re
import sys

import pytest
import redis
from mitmproxy.test import tflow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import addon  # noqa: E402
import app as budget  # noqa: E402


@pytest.fixture()
def rdb(monkeypatch):
    r = redis.Redis(host="localhost", port=6379, db=15, decode_responses=True)
    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        pytest.skip("needs a local redis (tests use db 15)")
    r.flushdb()
    monkeypatch.setattr(addon, "r", r)
    monkeypatch.setattr(budget, "r", r)
    yield r
    r.flushdb()


@pytest.fixture()
def client(rdb):
    budget.app.config["TESTING"] = True
    return budget.app.test_client()


def probe(path, headers=None, method="GET", host="www.reddit.com"):
    f = tflow.tflow(resp=False)
    f.request.host = host
    f.request.path = path
    f.request.method = method
    for k, v in (headers or {}).items():
        f.request.headers[k] = v
    addon.BudgetAddon().request(f)
    return f.response


# What a browser really sends, same-origin on the gated site.
FETCH = {"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
IFRAME = {"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin"}
NAV = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"}


# ---------- 1. SSRF / content injection via userinfo in the path ----------

@pytest.mark.parametrize("path", [
    "/budget@evil.example/x",           # -> http://127.0.0.1:5000@evil.example/x
    "/budget@127.0.0.1:9/x",
    "/budget:8080@evil.example/",
    "/budget/../health",
    "/budget/nope",
    "/budget/enter/../devices",
])
def test_no_path_can_redirect_the_internal_call_off_the_box(rdb, path, monkeypatch):
    """The one invariant that matters: whatever the browser puts in the path, the call
    the proxy makes must still go to 127.0.0.1:5000. "/budget@evil.example/" used to
    build "http://127.0.0.1:5000@evil.example/" — userinfo, not host — so the proxy
    fetched the attacker's page and served it on the gated site's origin."""
    from urllib.parse import urlsplit as _split
    calls = []

    class FakeResp:
        status_code, content, headers = 200, b"ok", {"Content-Type": "text/html"}

    def record(url, *a, **k):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(addon.req, "get", record)
    monkeypatch.setattr(addon.req, "post", record)
    probe(path, NAV)
    for url in calls:
        u = _split(url)
        assert (u.hostname, u.port) == ("127.0.0.1", 5000), f"{path} escaped to {url}"


def test_site_paths_merely_starting_with_budget_reach_the_site(rdb):
    """https://www.reddit.com/budgeting is Reddit's page, not ours — don't swallow it."""
    resp = probe("/budgeting", NAV)
    assert resp is None or resp.status_code != 404   # falls through to the gate logic


# ---------- 2. monitoring pages must not be readable by page scripts ----------

@pytest.mark.parametrize("path", [
    "/budget/devices?fmt=json", "/budget/health?fmt=json", "/budget/stats",
    "/budget/remaining", "/budget/boot-ack",
])
@pytest.mark.parametrize("hdrs,how", [(FETCH, "fetch"), (IFRAME, "iframe"), (NAV, "window.open")])
def test_gated_origin_never_serves_the_dashboard(rdb, path, hdrs, how):
    """The header-based version of this control could not cover window.open: a popup is
    a genuine top-level navigation and stays same-origin, so the opener can read it.
    The pages moved instead. Now no request shape on this origin returns their content —
    including the one no header could have distinguished."""
    rdb.set("monitor_origin", "http://100.64.0.1:5000")
    assert probe(path, hdrs).status_code == 302


def test_feed_still_needs_the_token(rdb):
    """/feed stays on the gated origin (the gate's background polls it) and is the only
    thing left here that returns data — two aggregate byte-rates."""
    rdb.set("feed_token", "SEKRIT")
    assert probe("/budget/feed?t=SEKRIT", FETCH).status_code != 403
    assert probe("/budget/feed?t=WRONG", FETCH).status_code == 403
    assert probe("/budget/feed", FETCH).status_code == 403


# ---------- 3. the gate page must not carry the master token ----------

def test_gate_page_carries_only_the_feed_token(rdb, client):
    """The gate is fetchable by any script on the gated site, so whatever it embeds is
    public to that site. It must therefore not embed ui_token."""
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    tok = re.search(r'<meta name="cd-tok" content="([^"]*)"', html).group(1)
    assert tok == rdb.get("feed_token")
    assert tok != rdb.get("ui_token")


def test_stolen_gate_token_unlocks_only_the_feed(rdb, client):
    rdb.set("monitor_origin", "http://100.64.0.1:5000")
    client.get("/budget?site=reddit")                     # mint both tokens
    stolen = rdb.get("feed_token")
    assert probe(f"/budget/feed?t={stolen}", FETCH).status_code != 403
    # Not 403 any more but 302 — the token is irrelevant because the data isn't here.
    for p in ("/budget/devices", "/budget/health", "/budget/stats", "/budget/remaining"):
        assert probe(f"{p}?t={stolen}", FETCH).status_code == 302


def test_monitoring_pages_still_carry_the_master_token(rdb, client):
    """Served straight off the box now, so this token only ever travels on that origin."""
    html = client.get("/health").get_data(as_text=True)
    tok = re.search(r'<meta name="cd-tok" content="([^"]*)"', html).group(1)
    assert tok == rdb.get("ui_token")


def test_monitoring_pages_do_not_link_back_onto_the_gated_origin(rdb, client):
    """Their cross-links must be root-relative to the box, or a tap sends you to a
    /budget/* path on the gated site that no longer exists."""
    for page in ("/health", "/stats", "/devices"):
        html = client.get(page).get_data(as_text=True)
        assert "/budget/stats" not in html, page
        assert "/budget/health" not in html, page
        assert "/budget/devices" not in html, page


def test_empty_or_wrong_token_is_never_a_match(rdb):
    rdb.delete("ui_token", "feed_token")
    for q in ("", "?t=", "?t=WRONG"):
        assert probe(f"/budget/feed{q}", FETCH).status_code == 403


# ---------- 4. cross-site GET to a state-changing endpoint ----------

def test_cross_site_get_to_exit_is_rejected(rdb):
    """<img src="https://www.reddit.com/budget/exit?site=reddit"> on any page."""
    hdrs = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image"}
    assert probe("/budget/exit?site=reddit", hdrs).status_code == 403


def test_cross_site_post_still_rejected(rdb):
    assert probe("/budget/enter?site=reddit", {"Sec-Fetch-Site": "cross-site"},
                 method="POST").status_code == 403


def test_same_origin_exit_still_works(rdb):
    hdrs = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "document"}
    assert probe("/budget/exit?site=youtube", hdrs).status_code != 403


# ---------- 5. response headers ----------

def test_proxied_responses_keep_their_content_type_and_cannot_be_framed(rdb, monkeypatch):
    class FakeResp:
        status_code = 200
        content = b'{"enc": 1, "unenc": 2}'
        headers = {"Content-Type": "application/json"}

    monkeypatch.setattr(addon.req, "get", lambda *a, **k: FakeResp())
    rdb.set("feed_token", "SEKRIT")
    resp = probe("/budget/feed?t=SEKRIT", FETCH)
    assert resp.headers["Content-Type"].startswith("application/json")   # was text/html
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


# ---------- 6. the POST body actually reaches Flask ----------

def test_post_body_is_forwarded(rdb):
    """Without this the reflection prompt's answer is dropped and /stats stays empty."""
    f = tflow.tflow(resp=False)
    f.request.host = "www.reddit.com"
    f.request.path = "/budget/reflect"
    f.request.method = "POST"
    f.request.headers["Content-Type"] = "application/x-www-form-urlencoded"
    f.request.headers["Sec-Fetch-Site"] = "same-origin"
    f.request.set_content(b"trigger=bored")
    seen = {}

    class FakeResp:
        status_code, content, headers = 200, b"{}", {"Content-Type": "application/json"}

    def fake_post(url, data=None, headers=None, **kw):
        seen.update(url=url, data=data, headers=headers)
        return FakeResp()

    addon.req.post, orig = fake_post, addon.req.post
    try:
        addon.BudgetAddon().request(f)
    finally:
        addon.req.post = orig
    assert seen["data"] == b"trigger=bored"
    assert seen["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


# ---------- 7. CSP: the directive the browser will actually consult ----------

def _amend(policy):
    return addon.BudgetAddon._csp_with_nonce(policy, "NONCE")


def test_script_src_elem_is_what_governs_our_inline_script():
    """script-src says unsafe-inline, but script-src-elem is stricter and wins for a
    <script> ELEMENT. Reading only script-src concluded 'already allowed' and left the
    policy alone — our heartbeat was then blocked, silently, and time stopped being
    charged while browsing continued."""
    out = _amend("script-src 'unsafe-inline'; script-src-elem 'self'")
    assert "'nonce-NONCE'" in out
    elem = [d for d in out.split(";") if d.strip().startswith("script-src-elem")][0]
    assert "'nonce-NONCE'" in elem          # added where the browser will look
    assert "script-src 'unsafe-inline'" in out   # theirs untouched


def test_script_src_elem_alone_is_not_ignored():
    out = _amend("script-src-elem 'self'")
    assert "'nonce-NONCE'" in out


def test_script_src_elem_with_unsafe_inline_is_left_alone():
    """Adding a nonce would switch off THEIR inline scripts. Ours already runs."""
    p = "script-src-elem 'unsafe-inline'; script-src 'self'"
    assert _amend(p) == p


def test_plain_script_src_and_default_src_still_behave():
    assert "'nonce-NONCE'" in _amend("script-src 'self'")
    out = _amend("default-src 'self'")
    assert "script-src 'self' 'nonce-NONCE'" in out     # synthesised, default-src intact
    assert _amend("default-src 'unsafe-inline'") == "default-src 'unsafe-inline'"
    assert _amend("img-src 'self'") == "img-src 'self'"  # nothing constrains scripts


# ---------- 8. host matching is suffix-based everywhere, not substring ----------

@pytest.mark.parametrize("host", [
    "redd.it.evil.example",         # IGNORED_HOSTS substring match let this through
    "notredd.it",
    "api.puzzmo.com.evil.example",
    "ytimg.com.attacker.test",
])
def test_lookalike_hosts_are_not_treated_as_ignored_assets(host):
    assert not addon.host_matches(host, addon.IGNORED_HOSTS)


@pytest.mark.parametrize("host", ["redd.it", "i.redd.it", "api.puzzmo.com", "s.ytimg.com"])
def test_real_asset_hosts_still_match(host):
    assert addon.host_matches(host, addon.IGNORED_HOSTS)


def test_host_matching_ignores_port_and_case():
    assert addon.host_matches("I.Redd.It:443", addon.IGNORED_HOSTS)
