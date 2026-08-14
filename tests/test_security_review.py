"""Regressions for the findings of the 2026-08 review.

Each test fails against the code as it was before the fix, so they pin the behaviour
rather than just describing it.
"""
import os
import re
import sys
import json
import time

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


# ---------- 9. the three bugs found in the 2026-08 read-through ----------

def test_every_gated_site_appears_in_the_dashboard(rdb, client):
    """The old fixed series list omitted "news", so news minutes were charged to the
    shared bucket — draining it, triggering cooldowns — and shown nowhere. Derived from
    SITES now, so adding a site cannot silently fall out of the chart again."""
    import time as _t
    day = _t.strftime("%Y-%m-%d")
    for site in budget.SITES:
        rdb.set(f"usage:{day}:{site}", 600)          # 10 minutes each
    html = client.get("/stats").get_data(as_text=True)
    for site, cfg in budget.SITES.items():
        assert cfg["label"] in html, f"{site} missing from the dashboard"
    total = 10 * len(budget.SITES)
    assert f"{total}m" in html, "the headline total must count every site"


def test_dashboard_root_does_not_404(rdb, client):
    """The monitoring pages live on the box's own origin now, so http://<box>:5000/ is
    an address people type."""
    resp = client.get("/")
    assert resp.status_code in (301, 302)
    assert "/stats" in resp.headers["Location"]


def test_missed_daily_reset_is_caught_up_at_startup(rdb):
    """A cron job only fires if the process is running at 07:00. A box that was off
    skipped the day — and nothing but this reset ever clears night_spent."""
    rdb.set("spent:main", 500)
    rdb.set("night_spent:main", 300)
    rdb.delete("last_reset")                          # as if never reset
    assert budget.catch_up_reset() is True
    assert float(rdb.get("spent:main") or 0) == 0
    assert float(rdb.get("night_spent:main") or 0) == 0
    assert rdb.get("last_reset") == budget.reset_day()


def test_catch_up_is_idempotent(rdb):
    budget.catch_up_reset()
    rdb.set("spent:main", 400)
    assert budget.catch_up_reset() is False           # already done today
    assert float(rdb.get("spent:main")) == 400        # so it must not wipe a live session


def test_reset_day_belongs_to_yesterday_before_the_reset_hour(rdb):
    """At 03:00 you are still inside yesterday's budget day."""
    import time as _t
    lt = _t.localtime()
    at_3am = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 3, 0, 0, lt.tm_wday, lt.tm_yday, -1))
    at_9am = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, lt.tm_wday, lt.tm_yday, -1))
    assert budget.reset_day(at_3am) != budget.reset_day(at_9am)


def test_importing_app_does_not_reset_anything(rdb):
    """catch_up_reset DELETES budget state, so it must live in __main__, not at module
    scope where `import app` (tests, backup tooling, a REPL) would trigger it."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "app.py")).read()
    body, _, main = src.partition("if __name__ == '__main__':")
    assert "catch_up_reset()" not in body, "catch_up_reset is called at import time"
    assert "catch_up_reset()" in main


# ---------- 10. swallowed errors are counted, not silent ----------

def test_try_counts_what_it_swallows(rdb):
    budget._ERRORS.clear(); budget._LAST_ERROR.clear()
    def boom():
        raise ValueError("nope")
    assert budget._try(boom, "fallback") == "fallback"     # behaviour unchanged
    s = budget.error_summary()
    assert s["total"] == 1
    assert "ValueError" in s["top"][0]["what"]
    assert "nope" in s["last"]


def test_health_reports_proxy_side_errors_too(rdb):
    """addon.py runs as a separate service whose stdout nobody reads; without this its
    exceptions are strictly invisible."""
    budget._ERRORS.clear(); budget._LAST_ERROR.clear()
    addon._note_error("budget_handler", RuntimeError("proxy exploded"))
    assert budget.error_summary()["proxy"] == 1
    assert rdb.get("proxy_last_error").endswith("RuntimeError: proxy exploded")


def test_error_reporting_never_raises(rdb, monkeypatch):
    """A broken reporter must not break the proxy or the health page."""
    class Dead:
        def __getattr__(self, _):
            def f(*a, **k):
                raise ConnectionError("redis is down")
            return f
    monkeypatch.setattr(addon, "r", Dead())
    addon._note_error("x", ValueError("y"))                # must not raise
    monkeypatch.setattr(budget, "r", Dead())
    assert budget.error_summary()["proxy"] == 0


_HEALTH_BASE = {
    "model": "Raspberry Pi", "cpu": {"pct": 0, "per_core": [], "load": [0, 0, 0], "cores": 1},
    "temp_c": 40, "temp_hist": [40], "mem": {"used_mb": 1, "total_mb": 2, "pct": 50},
    "disk": {"used_gb": 1, "total_gb": 2, "pct": 50}, "uptime": "1h",
    "net": {"eth0": None, "tailscale0": None, "wlan0": None},
    "power": {"ok": True}, "services": {}, "services_ok": True,
    "errors": {"total": 0, "proxy": 0, "top": [], "last": "", "last_ago": None},
    "enforcement": {"ok": True, "browsing": False, "nav_ago": None, "charge_ago": None},
    "updates": {"fresh": True, "pending": 0, "security": 0, "reboot_required": False,
                "boot_ok": True, "stuck_jobs": 0,
                "last_result": "success", "last_run_ago": 3600, "checked_ago": 60},
    "audit": {"fresh": True, "findings": 0, "ssh_keys": 1, "shell_accounts": "pi root",
              "exposed_ports": "", "tampered_files": 0, "journal_persistent": True,
              "ca_mtime": 1750000000, "checked_ago": 3600},
}


def _health_html(client, monkeypatch, errors):
    """Render /health against a fixed payload. Collecting it for real is
    environment-dependent — on a non-Pi host vcgencmd and /proc/device-tree/model are
    genuinely missing, which is two swallowed errors before the test does anything."""
    monkeypatch.setattr(budget, "collect_health",
                        lambda *a, **k: dict(_HEALTH_BASE, errors=errors))
    return client.get("/health").get_data(as_text=True)


def _health_html_with(client, monkeypatch, updates):
    """Same fixed payload, with only the patch-state block swapped."""
    monkeypatch.setattr(budget, "collect_health",
                        lambda *a, **k: dict(_HEALTH_BASE, updates=updates))
    return client.get("/health").get_data(as_text=True)


def test_health_page_says_so_when_nothing_has_broken(rdb, client, monkeypatch):
    html = _health_html(client, monkeypatch,
                        {"total": 0, "proxy": 0, "top": [], "last": "", "last_ago": None})
    assert "No handled errors since boot" in html


def test_health_page_surfaces_app_and_proxy_errors(rdb, client, monkeypatch):
    html = _health_html(client, monkeypatch,
                        {"total": 3, "proxy": 2, "top": [], "last_ago": 5,
                         "last": "_power: FileNotFoundError: vcgencmd"})
    assert "in the app" in html and "in the proxy" in html
    assert "in the proxy" in html
    assert "vcgencmd" in html


# ---------- 11. the enforcement dead-man's switch ----------

def _live_session(rdb, site="reddit"):
    rdb.set(f"active_token:{site}", "tok"); rdb.set("session:tok", "active")


def test_navigation_during_a_live_session_is_recorded(rdb):
    _live_session(rdb)
    f = tflow.tflow(resp=False)
    f.request.host, f.request.path = "www.reddit.com", "/r/python"
    f.request.headers["Sec-Fetch-Dest"] = "document"
    addon.BudgetAddon().request(f)
    assert f.response is None                       # traffic still passes through
    assert rdb.get("last_active_nav")               # ...and the moment is recorded


def _meters(rdb, hb_secs, sh_secs, site="reddit", now=None):
    """Seed the two meters for the current clock hour.

    The switch reads hourly buckets written by app.py (usage_hour) and addon.py
    (shadow_hour). Tests state both numbers explicitly, because the entire judgement is
    now the relationship between them rather than the age of either one.
    """
    now = now if now is not None else time.time()
    t = time.localtime(now)
    day, hour = time.strftime("%Y-%m-%d", t), time.strftime("%H", t)
    if hb_secs:
        rdb.set(f"usage_hour:{day}:{hour}:{site}", hb_secs)
    if sh_secs:
        rdb.set(f"shadow_hour:{day}:{hour}:{site}", sh_secs)


def test_switch_fires_when_the_heartbeat_stops_but_the_site_keeps_being_used(rdb):
    """The exact F20 shape: a CSP blocks the injected script, so the site works, the
    heartbeat never posts, and the budget silently stops draining. The passive meter is
    unaffected -- it watches Reddit's own traffic -- so the two diverge sharply."""
    _meters(rdb, hb_secs=20, sh_secs=25 * 60)
    assert addon.enforcement_looks_dead() is True


def test_switch_is_quiet_when_the_heartbeat_is_working(rdb):
    _meters(rdb, hb_secs=24 * 60, sh_secs=25 * 60)   # the 1.03x the meters actually show
    assert addon.enforcement_looks_dead() is False


def test_switch_is_quiet_when_you_simply_are_not_browsing(rdb):
    """A quiet week is not a broken pipeline. No use means nothing to disagree about."""
    assert addon.enforcement_looks_dead() is False   # fresh install, no keys at all
    _meters(rdb, hb_secs=0, sh_secs=90)              # under the floor: cannot judge
    assert addon.enforcement_looks_dead() is False


def test_switch_cannot_warn_on_the_first_page_of_a_session(rdb):
    """The false alarm that fired on nearly every visit, now impossible by construction.

    The old design asked "how long since a charge?" and on arrival the answer was always
    "hours", so it warned every session until a refresh. The ratio test cannot reach that
    state: at the start of a session both meters read zero, and zero is not a
    disagreement. The bug is gone because the question changed, not because a special
    case was bolted on to suppress it.
    """
    _meters(rdb, hb_secs=0, sh_secs=0)
    assert addon.enforcement_looks_dead() is False
    _meters(rdb, hb_secs=3, sh_secs=4)               # a few seconds in
    assert addon.enforcement_looks_dead() is False


def test_switch_needs_enough_observed_use_before_it_will_judge(rdb):
    """Below the floor, one stray background request looks like a 100% shortfall."""
    _meters(rdb, hb_secs=0, sh_secs=addon.ENFORCEMENT_MIN_PASSIVE - 1)
    assert addon.enforcement_looks_dead() is False
    _meters(rdb, hb_secs=0, sh_secs=addon.ENFORCEMENT_MIN_PASSIVE + 60)
    assert addon.enforcement_looks_dead() is True


def test_switch_tolerates_the_disagreement_the_meters_actually_show(rdb):
    """Seven days of paired data put the two within 0.72x to 1.28x of each other per
    hour. The threshold has to sit well outside that or ordinary noise becomes an alarm,
    and an alarm that fires on ordinary noise is one you learn to ignore."""
    for ratio in (0.72, 0.9, 1.0, 1.28):
        rdb.flushdb()
        _meters(rdb, hb_secs=int(30 * 60 / ratio), sh_secs=30 * 60)
        assert addon.enforcement_looks_dead() is False, f"false alarm at {ratio}x"


def test_switch_looks_back_beyond_the_current_clock_hour(rdb):
    """Use that began in the previous hour still counts.

    At 14:05 the current bucket holds five minutes. A switch that read only the current
    hour would go quiet for the first few minutes of every hour, and would never judge
    at all if you browsed 13:30-14:02. Seeding the previous hour also walks the
    day-rollover path when this runs shortly after midnight.
    """
    prev = time.time() - 3600
    _meters(rdb, hb_secs=20, sh_secs=25 * 60, now=prev)
    assert addon.enforcement_looks_dead() is True


def test_switch_is_blind_not_broken_without_the_shadow_meter(rdb):
    """CLAUDE.md says the shadow meter may one day be deleted. If that happens this has
    to fall silent, not start warning about a disagreement it can no longer measure."""
    _meters(rdb, hb_secs=25 * 60, sh_secs=0)
    assert addon.enforcement_looks_dead() is False


def test_both_processes_reach_the_same_verdict(rdb):
    """addon.py draws the banner, app.py draws the /health card. Separate processes,
    duplicated logic, one Redis. On identical data they must not disagree."""
    # 0.6x sits between the real threshold and any plausible wrong one, so a drift in
    # either copy shows up here. Without a case in that gap both agree on everything.
    for hb, sh in ((20, 25 * 60), (24 * 60, 25 * 60), (0, 0), (0, 90), (0, 30 * 60),
                   (int(0.6 * 30 * 60), 30 * 60), (int(0.3 * 30 * 60), 30 * 60)):
        rdb.flushdb()
        _meters(rdb, hb_secs=hb, sh_secs=sh)
        banner = addon.enforcement_looks_dead()
        card = budget.enforcement_status()["ok"] is False
        assert banner is card, f"banner={banner} card={card} at hb={hb} sh={sh}"


def test_switch_survives_a_redis_outage_without_crying_wolf(rdb, monkeypatch):
    """It sits in the response path of every gated page. A broken reporter must not
    break browsing, and must not invent an alarm out of an error."""
    def boom(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(addon.r, "mget", boom)
    assert addon.enforcement_looks_dead() is False


def test_warning_needs_no_javascript(rdb):
    """It reports that the injected JavaScript is not running, so it must not need any.
    A <div> with inline styles survives the CSP that caused the failure."""
    assert "<script" not in addon.ENFORCEMENT_WARNING
    assert "onerror" not in addon.ENFORCEMENT_WARNING and "onload" not in addon.ENFORCEMENT_WARNING
    assert addon.ENFORCEMENT_WARNING.startswith("<div")


def test_warning_is_injected_into_the_page_when_the_switch_fires(rdb):
    _live_session(rdb)
    _meters(rdb, hb_secs=20, sh_secs=25 * 60)        # heartbeat flatlined, passive did not
    f = tflow.tflow(resp=True)
    f.request.host, f.request.path = "www.reddit.com", "/r/python"
    f.response.headers["content-type"] = "text/html"
    f.response.text = "<html><body>page</body></html>"
    addon.BudgetAddon().response(f)
    assert "isn't counting this time" in f.response.text
    assert f.response.text.index("role=\"alert\"") < f.response.text.index("bp-timewarn")


def test_no_warning_when_enforcement_is_healthy(rdb):
    _live_session(rdb)
    _meters(rdb, hb_secs=24 * 60, sh_secs=25 * 60)   # both meters agree
    f = tflow.tflow(resp=True)
    f.request.host, f.request.path = "www.reddit.com", "/r/python"
    f.response.headers["content-type"] = "text/html"
    f.response.text = "<html><body>page</body></html>"
    addon.BudgetAddon().response(f)
    assert "isn't counting" not in f.response.text
    assert "bp-timewarn" in f.response.text          # the normal heartbeat still injected


def test_health_is_quiet_on_the_first_page_of_a_session(rdb):
    """/health must apply the same rule as the banner. When it did not, the card called
    every fresh session a failure -- and a dashboard that is wrong on arrival is worse
    than one that says nothing, because you learn to discount it."""
    _meters(rdb, hb_secs=0, sh_secs=0)
    st = budget.enforcement_status()
    assert st["ok"] is True
    assert st["judged"] is False                     # and it says it could not judge


def test_health_reports_a_dead_heartbeat(rdb, client, monkeypatch):
    _meters(rdb, hb_secs=20, sh_secs=25 * 60)
    st = budget.enforcement_status()
    assert st["ok"] is False
    assert st["passive_min"] > st["hb_min"]          # and shows the numbers it judged on
    html = _health_html(client, monkeypatch,
                        {"total": 0, "proxy": 0, "top": [], "last": "", "last_ago": None})
    assert "No handled errors" in html               # fixture forces enforcement ok


# ---------- 12. the post-session check-in ----------

def test_the_trigger_is_carried_through_the_session(rdb, client):
    """Without the join, both halves are just tallies — the whole value is 'when I was
    tired it was never worth it'."""
    client.post("/enter?site=reddit", data={"trigger": "tired"})
    assert rdb.get("session_trigger:main") == "tired"


def test_session_end_queues_the_question_and_the_gate_asks_it(rdb, client, day):
    """Asked on the screen you land on BECAUSE a session ended — here, the cap being
    spent. Never on the way in: the gate must not become a toll booth."""
    rdb.set("session_trigger:main", "bored")
    rdb.set("spent:main", budget.SITES["reddit"]["budget_seconds"] + 1)
    budget.queue_worth_prompt("reddit")
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert "Was it worth it?" in html


def test_the_gate_never_asks_on_the_way_in(rdb, client, day, session):
    """A live session with budget left renders Enter — no question there."""
    budget.queue_worth_prompt("reddit")
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert "Enter Reddit" in html
    assert "Was it worth it?" not in html


def test_the_gate_does_not_ask_when_nothing_ended(rdb, client, day):
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert "Was it worth it?" not in html


def test_answering_records_it_against_the_trigger(rdb, client, day):
    rdb.set("session_trigger:main", "tired")
    budget.queue_worth_prompt("reddit")
    client.post("/worth?site=reddit", data={"v": "no"})
    s = budget.worth_summary()
    assert s["total"] == 1 and s["yes"] == 0
    assert s["rows"][0]["key"] == "tired" and s["rows"][0]["pct"] == 0


def test_a_second_tap_cannot_invent_data(rdb, client, day):
    budget.queue_worth_prompt("reddit")
    assert budget.log_worth("yes") is True
    assert budget.log_worth("yes") is False        # nothing pending any more
    assert budget.worth_summary()["total"] == 1


def test_a_bogus_verdict_is_ignored(rdb):
    budget.queue_worth_prompt("reddit")
    assert budget.log_worth("maybe") is False
    assert budget.log_worth("<script>") is False
    assert budget.worth_summary()["total"] == 0


def test_every_heartbeat_session_end_queues_the_prompt(rdb):
    """Four code paths end a budgeted session; a prompt queued at only some of them
    would quietly bias the data toward whichever cap you hit."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py")).read()
    hb = src[src.index("def heartbeat("):src.index("@app.route('/remaining')")]
    ends = hb.count('r.delete(f"session:{token}")')
    assert ends == 4, f"heartbeat now has {ends} session-end paths"
    assert hb.count("queue_worth_prompt(site, now)") == ends


def test_stats_pairs_the_reason_with_the_outcome(rdb, client):
    day_key = time.strftime("%Y-%m-%d")
    for t, v in [("tired", "no"), ("tired", "no"), ("need", "yes")]:
        rdb.rpush(f"worth:{day_key}", f"{time.time():.0f} {t} {v}")
    html = client.get("/stats").get_data(as_text=True)
    assert "was it worth it?" in html
    assert "Tired" in html and "Actually needed it" in html


# ---------- 13. the weekly digest ----------

def test_digest_renders_with_no_data_at_all(rdb, client):
    """A fresh install must not 500 on an empty week."""
    resp = client.get("/digest")
    assert resp.status_code == 200
    assert "Nothing charged this week" in resp.get_data(as_text=True)


def test_digest_summarises_the_week(rdb, client):
    now = time.time()
    for i in range(7):                                  # 20 min/day on reddit, 10 on news
        k = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        rdb.set(f"usage:{k}:reddit", 1200)
        rdb.set(f"usage:{k}:news", 600)
    html = client.get("/digest").get_data(as_text=True)
    assert "210m" in html                               # 30 min x 7 days
    assert "Reddit" in html and "News" in html          # every gated site, not a fixed four


def test_digest_reports_cluster_not_just_volume(rdb, client):
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    rdb.rpush(f"cooldown_events:{day}", f"{now - 7200:.0f} reddit", f"{now - 3600:.0f} reddit")
    html = client.get("/digest").get_data(as_text=True)
    assert "2</b> cooldown" in html
    assert "1</b> came within" in html                  # the rapid repeat is the signal


def test_digest_is_box_origin_only(rdb):
    """It reads a week of behavioural history — it must not be reachable from a gated
    site any more than /stats is."""
    assert "/digest" in addon.MOVED_TO_BOX
    rdb.set("monitor_origin", "http://100.64.0.1:5000")
    for hdrs in (FETCH, IFRAME, NAV):
        assert probe("/budget/digest", hdrs).status_code == 302


def test_a_healthy_box_reports_zero_handled_errors(rdb, monkeypatch):
    """wlan0 with no association returns EINVAL for `speed`; tunnels report -1. Both are
    "not applicable". If they land in the counter, the health line permanently reads
    "1 handled error" on a perfectly healthy Pi and everyone learns to ignore it."""
    budget._ERRORS.clear()
    def fake_first_line(path):
        if path.endswith("/speed"):
            raise OSError(22, "Invalid argument")     # the wlan0 case, on a real Pi
        if path.endswith("/operstate"):
            return "down"
        return "0"                                    # rx_bytes / tx_bytes
    monkeypatch.setattr(budget, "_first_line", fake_first_line)
    monkeypatch.setattr(budget.os.path, "exists", lambda p: True)
    budget._iface("wlan0")
    assert budget.error_summary()["total"] == 0, budget.error_summary()["top"]
    assert budget._iface_speed("/sys/class/net/wlan0") is None


# ---------- 14. the shadow meter (experiment, 2026-08-03) ----------

def test_shadow_accrues_time_between_signs_of_life(rdb):
    addon._SHADOW.clear()
    t = time.time()
    for i in range(6):                       # a request every 5s for 25s
        addon.shadow_note("reddit", t + i * 5)
    addon._shadow_flush("reddit", addon._SHADOW["reddit"])
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    assert abs(float(rdb.get(f"shadow_usage:{day}:reddit")) - 25.0) < 0.01


def test_shadow_ignores_gaps_that_mean_you_left(rdb):
    """Same rule the heartbeat uses: a gap bigger than the cap is idleness, not use.
    Without it, opening a tab on Monday and again on Friday would bill you four days."""
    addon._SHADOW.clear()
    # Fixed midday clock: with time.time() the +3600 crosses midnight for an hour every
    # night, the meter correctly banks the old day, and the assertion reads half the
    # total. A test that fails only between 23:00 and midnight is worse than no test.
    t = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))
    addon.shadow_note("reddit", t)
    addon.shadow_note("reddit", t + 5)        # counted
    addon.shadow_note("reddit", t + 5 + 3600) # an hour later: not counted
    addon.shadow_note("reddit", t + 10 + 3600)
    addon._shadow_flush("reddit", addon._SHADOW["reddit"])
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    assert abs(float(rdb.get(f"shadow_usage:{day}:reddit")) - 10.0) < 0.01


def test_shadow_never_touches_budget_state(rdb):
    """The whole point of shadow mode. If this can move real state it isn't an
    experiment, it's a change."""
    rdb.set("spent:main", 123)
    before = {k: rdb.get(k) for k in rdb.keys("*")}
    addon._SHADOW.clear()
    t = time.time()
    for i in range(40):
        addon.shadow_note("reddit", t + i)
        addon.shadow_note("youtube", t + i)
    # The property is "shadow-namespaced only", not one specific key — it also writes
    # shadow_started. Anything outside that namespace would make this a change, not an
    # experiment.
    after = {k: rdb.get(k) for k in rdb.keys("*") if not k.startswith("shadow")}
    assert after == before, "shadow mode wrote outside its own namespace"
    assert float(rdb.get("spent:main")) == 123


def test_shadow_survives_a_dead_redis(rdb, monkeypatch):
    """A measurement must never be able to break the proxy."""
    class Dead:
        def __getattr__(self, _):
            def f(*a, **k):
                raise ConnectionError("redis down")
            return f
    monkeypatch.setattr(addon, "r", Dead())
    addon._SHADOW.clear()
    t = time.time()
    for i in range(30):
        addon.shadow_note("reddit", t + i)     # must not raise
    assert addon._SHADOW["reddit"]["pending"] > 0   # held, not lost


def test_shadow_banks_yesterday_before_rolling_over(rdb):
    addon._SHADOW.clear()
    t = time.time()
    yesterday = t - 86400
    addon.shadow_note("reddit", yesterday)
    addon.shadow_note("reddit", yesterday + 5)
    addon.shadow_note("reddit", t)             # new day -> flush the old one first
    addon._shadow_flush("reddit", addon._SHADOW["reddit"])
    yday = time.strftime("%Y-%m-%d", time.localtime(yesterday))
    assert abs(float(rdb.get(f"shadow_usage:{yday}:reddit") or 0) - 5.0) < 0.01


def test_shadow_writes_are_batched_not_per_request(rdb, monkeypatch):
    """This runs on the proxy's hot path. A Redis round-trip per request is exactly the
    kind of cost that starved the worker pool in F12."""
    calls = []
    real = addon.r.incrbyfloat
    monkeypatch.setattr(addon.r, "incrbyfloat", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    addon._SHADOW.clear()
    t = time.time()
    for i in range(100):                        # 100 requests, 1s apart = 99s of time
        addon.shadow_note("reddit", t + i)
    # Two writes per flush now (daily + hourly bucket), so ~20 for 100 requests. The
    # property under test is "batched, not per-request", not an exact count.
    assert len(calls) <= 25, f"{len(calls)} redis writes for 100 requests"


def test_stats_hides_the_comparison_until_the_meter_has_run(rdb, client):
    assert "measuring the same time two ways" not in client.get("/stats").get_data(as_text=True)


def _seed_hours(rdb, now, n, hb, sh, site="reddit"):
    """Fill n whole hours before `now` with hb/sh seconds each."""
    for i in range(1, n + 1):
        lt = time.localtime(now - i * 3600)
        d, h = time.strftime("%Y-%m-%d", lt), time.strftime("%H", lt)
        if hb: rdb.set(f"usage_hour:{d}:{h}:{site}", hb)
        if sh: rdb.set(f"shadow_hour:{d}:{h}:{site}", sh)


def test_comparison_counts_only_hours_where_both_meters_ran(rdb, client):
    """Day-granular keys cannot express "the meter started at 15:49". The first attempt
    divided 15 hours of heartbeat by 3 hours of passive and reported 0.22x — an artefact
    that looked exactly like a real finding."""
    now = time.time()
    # Heartbeat has history stretching back before the meter; passive only overlaps 20h.
    _seed_hours(rdb, now, 40, hb=300, sh=0)
    _seed_hours(rdb, now, 20, hb=300, sh=600)
    rdb.set("shadow_started", f"{now - 21 * 3600:.0f}")
    c = budget.shadow_comparison()
    assert c["ratio"] == 2.0, c            # only the overlapping hours, not the 40
    assert c["settled"] is True
    assert "2.0&times;" in client.get("/stats").get_data(as_text=True)


def test_comparison_ignores_the_partial_hour_the_meter_started_in(rdb):
    now = time.time()
    _seed_hours(rdb, now, 3, hb=300, sh=300)
    rdb.set("shadow_started", f"{now - 3 * 3600:.0f}")
    c = budget.shadow_comparison()
    # 3 hours seeded, meter started 3h ago -> only whole hours after that count
    assert c["hb"] == c["sh"], c
    assert c["ratio"] == 1.0


def test_comparison_says_it_is_too_early_on_day_one(rdb, client):
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    rdb.set(f"usage:{day}:reddit", 600)
    rdb.set(f"shadow_usage:{day}:reddit", 1200)
    rdb.set("shadow_started", f"{now - 3600:.0f}")   # one hour old
    assert budget.shadow_comparison()["settled"] is False
    assert "too early to draw conclusions" in client.get("/stats").get_data(as_text=True)


# ---------- 15. you can always get back to the gate ----------

@pytest.mark.parametrize("page", ["/stats", "/health", "/devices", "/digest"])
def test_every_dashboard_page_offers_a_way_back(rdb, client, page):
    """The dashboard moved to the box's own origin, and the 'back to the gate' link was
    removed at the same time without replacement — leaving a dead end you could only
    escape with the browser's back button."""
    html = client.get(page + "?from=reddit").get_data(as_text=True)
    assert 'href="https://www.reddit.com"' in html
    assert "Reddit" in html


@pytest.mark.parametrize("page", ["/stats", "/health", "/devices", "/digest"])
def test_the_trail_survives_hopping_between_dashboard_pages(rdb, client, page):
    """stats -> health -> devices must not lose the way out on the way."""
    html = client.get(page + "?from=youtube").get_data(as_text=True)
    for other in ("/stats", "/health", "/devices", "/digest"):
        if other in html:
            assert other + "?from=youtube" in html, f"{page} drops ?from on its {other} link"


def test_no_back_link_when_you_arrived_directly(rdb, client):
    """Typed the URL? There is nowhere to go 'back' to, so don't invent one."""
    html = client.get("/stats").get_data(as_text=True)
    assert 'class="back home"' not in html


def test_a_bogus_from_parameter_is_ignored(rdb, client):
    html = client.get("/stats?from=https://evil.example").get_data(as_text=True)
    assert "evil.example" not in html
    assert 'class="back home"' not in html


# ---------- 16. found by walking every link, not by reading the code ----------

def test_the_moved_page_redirect_carries_the_query_string(rdb):
    """Dropping it meant ?from=reddit was lost (landing you on a page with no way back)
    and ?fmt=json silently returned HTML to something expecting JSON."""
    rdb.set("monitor_origin", "http://100.64.0.1:5000")
    for path, expect in [("/budget/stats?from=reddit", "http://100.64.0.1:5000/stats?from=reddit"),
                         ("/budget/health?fmt=json", "http://100.64.0.1:5000/health?fmt=json"),
                         ("/budget/devices", "http://100.64.0.1:5000/devices")]:
        resp = probe(path, NAV)
        assert resp.status_code == 302
        assert resp.headers["Location"] == expect, path


@pytest.mark.parametrize("page", ["/stats", "/health", "/devices", "/digest"])
def test_every_dashboard_page_looks_like_the_same_product(rdb, client, page):
    """/digest was added after _add_bg and skipped it, so it alone had no ambient
    background and no frosted panels."""
    html = client.get(page).get_data(as_text=True)
    assert 'id="bp-bg"' in html, f"{page} has no background canvas"
    assert "backdrop-filter" in html, f"{page} panels are not frosted"
    assert 'name="cd-feed"' in html, f"{page} cannot poll the traffic feed"


# ---------- 17. disk reporting, retention, and the year in review ----------

def test_disk_matches_df_not_the_reserved_blocks(rdb, monkeypatch):
    """The old sum counted root-reserved blocks as used and reported 6.4 GB where df
    said 4.7 GB — alarming, and wrong."""
    class FakeVFS:
        f_frsize = 4096
        f_blocks = 7_500_000        # 30.7 GB total
        f_bfree = 6_300_000         # 25.8 GB actually free
        f_bavail = 5_950_000        # 24.4 GB available to a non-root user
    monkeypatch.setattr(budget.os, "statvfs", lambda p: FakeVFS())
    d = budget._disk()
    assert d["used_gb"] == 4.9, d          # total - f_bfree, as df computes it
    assert d["avail_gb"] == 24.4
    assert d["pct"] == 17                  # against used + available, not raw total


def test_history_survives_long_enough_for_a_year_in_review(rdb):
    """A yearly page is impossible if the data expires at 100 days — the year ends after
    the history does."""
    assert budget.HISTORY_TTL >= 366 * 86400
    day = time.strftime("%Y-%m-%d")
    budget.log_reflection("tired", "pass")
    budget.queue_worth_prompt("reddit"); budget.log_worth("no")
    budget.start_cooldown("main", "reddit")
    for key in (f"reflect:{day}", f"worth:{day}", f"cooldown_events:{day}"):
        assert rdb.ttl(key) > 365 * 86400, key


def test_wrapped_is_honest_when_there_is_no_data(rdb, client):
    html = client.get("/wrapped").get_data(as_text=True)
    assert "No usage recorded yet" in html
    assert client.get("/wrapped").status_code == 200


def test_wrapped_reports_the_trend_that_answers_did_it_work(rdb, client):
    now = time.time()
    for i in range(120):                      # heavier at the start, lighter lately
        k = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        rdb.set(f"usage:{k}:reddit", 600 if i < 30 else 2400)
    d = budget.wrapped_data()
    assert d["any"] and d["days"] == 120
    assert d["trend_pct"] < 0, d              # recent days lighter than early ones
    assert d["enough"] is True
    assert "Did it work?" in client.get("/wrapped").get_data(as_text=True)


def test_wrapped_admits_when_the_sample_is_too_small(rdb, client):
    now = time.time()
    for i in range(10):
        k = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        rdb.set(f"usage:{k}:reddit", 600)
    assert budget.wrapped_data()["enough"] is False
    assert "treat this as a hint" in client.get("/wrapped").get_data(as_text=True)


def test_wrapped_is_box_origin_only(rdb):
    """It reads a year of behavioural history — the most sensitive page in the project."""
    assert "/wrapped" in addon.MOVED_TO_BOX
    rdb.set("monitor_origin", "http://100.64.0.1:5000")
    for hdrs in (FETCH, IFRAME, NAV):
        assert probe("/budget/wrapped", hdrs).status_code == 302


def test_wide_binds_are_only_flagged_when_they_are_actually_wrong(rdb, client, monkeypatch):
    """mitmproxy binds every interface and cannot be told otherwise in transparent mode;
    sshd likewise. Both are contained by the interface-scoped firewall — that is what F1
    fixed. Painting them red would train you to ignore the colour, and then miss the case
    that matters: the dashboard port going wide."""
    monkeypatch.setattr(budget, "_listening_ports", lambda: [
        {"port": 8080, "scope": "all interfaces", "rank": 0, "what": "proxy"},
        {"port": 5000, "scope": "all interfaces", "rank": 0, "what": "dashboard"},
    ])
    monkeypatch.setattr(budget, "_HEALTH_CACHE", {})
    # "8080 is contained" is now a fact read from the live rules by the audit, not a
    # constant in app.py -- the constant claimed port 22 was firewalled while it was not.
    # The test therefore has to supply the fact.
    monkeypatch.setattr(budget, "_audit",
                        lambda *a, **k: {"fw_policy": "DROP", "fw_contained": [8080, 22]})
    html = client.get("/health").get_data(as_text=True)
    rows = re.findall(r'<td>(\d+)[^<]*</td>\s*<td class="([a-z]*)"', html)
    flags = dict(rows)
    assert flags.get("8080") == "dim", flags       # expected, firewalled
    assert flags.get("5000") == "wide", flags      # bound narrowly on purpose — alarm


def test_ports_are_not_called_contained_when_the_firewall_state_is_unknown(rdb, client, monkeypatch):
    """If the audit has not reported, nothing can be called contained.

    The alternative -- assume the old hardcoded set -- is how a dashboard ends up
    asserting a control that does not exist. Unknown renders as wide, and says so.
    """
    monkeypatch.setattr(budget, "_listening_ports", lambda: [
        {"port": 8080, "scope": "all interfaces", "rank": 0, "what": "proxy"},
    ])
    monkeypatch.setattr(budget, "_HEALTH_CACHE", {})
    monkeypatch.setattr(budget, "_audit", lambda *a, **k: {"fw_policy": "unknown",
                                                           "fw_contained": []})
    html = client.get("/health").get_data(as_text=True)
    assert "Firewall state unknown" in html
    flags = dict(re.findall(r'<td>(\d+)[^<]*</td>\s*<td class="([a-z]*)"', html))
    assert flags.get("8080") == "wide", flags


# ---------- 18. themes ----------

def test_no_theme_may_lighten_the_surface_the_charts_sit_on():
    """SERIES_COLORS and CPU_LINE_COLORS were validated for contrast and colour-vision
    separation against a #14171d card. A theme that lightened the card would silently
    invalidate all of it, and the charts would stop being readable for exactly the people
    the validation protects. Themes repaint the backdrop and the accents, never the
    surface the data sits on."""
    def luminance(hexstr):
        r, g, bl = (int(hexstr[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl)
    base = luminance("#14171d")
    assert budget.THEMES.items(), ("budget.THEMES.items() is empty, so the loop below would assert nothing and this test would pass having checked nothing")
    for name, th in budget.THEMES.items():
        card = th["vars"]["card"]
        assert luminance(card) <= base * 1.6, f"{name} card {card} is too light for the charts"


def test_every_theme_keeps_body_text_readable():
    """Contrast of --fg against --card, WCAG-style. Below 7:1 and the smallest labels on
    these pages start to go."""
    def lum(h):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    assert budget.THEMES.items(), ("budget.THEMES.items() is empty, so the loop below would assert nothing and this test would pass having checked nothing")
    for name, th in budget.THEMES.items():
        a, bg = lum(th["vars"]["fg"]), lum(th["vars"]["card"])
        ratio = (max(a, bg) + 0.05) / (min(a, bg) + 0.05)
        assert ratio >= 7, f"{name}: fg on card is only {ratio:.1f}:1"


def test_seasonal_themes_own_their_dates(rdb):
    import time as _t
    for date, expect in [("2026-12-25", "christmas"), ("2026-10-31", "halloween"),
                         ("2027-01-01", "newyear"), ("2026-12-19", "christmas")]:
        got, _ = budget.active_theme(_t.mktime(_t.strptime(date, "%Y-%m-%d")))
        assert got == expect, f"{date} -> {got}"


def test_a_spontaneous_theme_cannot_be_rerolled_by_refreshing(rdb):
    """Something that changes when you look at it is a slot machine, not a surprise —
    same reasoning as the reflection prompt being seeded rather than live-random."""
    import time as _t
    day = _t.mktime(_t.strptime("2026-08-04", "%Y-%m-%d"))
    picks = {budget.active_theme(day + n)[0] for n in range(0, 3600, 300)}
    assert len(picks) == 1, f"theme changed within the same day: {picks}"


def test_spontaneous_themes_are_occasional_not_constant(rdb):
    import time as _t
    base = _t.mktime(_t.strptime("2026-03-01", "%Y-%m-%d"))
    days = [budget.active_theme(base + i * 86400)[0] for i in range(200)]
    themed = [d for d in days if d and not budget.THEMES[d]["season"]]
    assert 10 < len(themed) < 80, f"{len(themed)} themed days out of 200"


def test_theme_override_and_off(rdb, client):
    plain = client.get("/budget?site=reddit&theme=off").get_data(as_text=True)
    assert '<style id="cd-theme"></style>' in plain
    xmas = client.get("/budget?site=reddit&theme=christmas").get_data(as_text=True)
    assert "--bg:#0a1410" in xmas
    junk = client.get("/budget?site=reddit&theme=../../etc/passwd").get_data(as_text=True)
    assert "passwd" not in junk and '<style id="cd-theme"></style>' in junk


@pytest.mark.parametrize("page", ["/stats", "/health", "/devices", "/digest", "/wrapped"])
def test_every_page_carries_the_theme_hook(rdb, client, page):
    assert 'id="cd-theme"' in client.get(page).get_data(as_text=True)


# ---------- 19. the gate's own copy ----------

def test_a_data_moment_never_replaces_a_line_carrying_information(rdb):
    """The 'spent' line names the site that still has time; 'cooldown_escalated' explains
    why the wall got taller. Both were briefly replaceable by a wisecrack, which dropped
    information the screen has nowhere else. Only lines whose number is already on screen
    above them may be swapped."""
    assert budget.MOMENT_STATES == {"day", "cooldown"}
    # Pinned to midday. Without a fixed clock this test passes by day and fails after
    # 22:00, when the wind-down time-line legitimately fires — a test that depends on the
    # hour it runs at is worse than no test.
    noon = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))
    for state in ("spent", "cooldown_escalated", "night", "night_closed",
                  "winddown", "winddown_spent", "soft"):
        seen = {budget.gate_line(state, now=noon, label="Reddit",
                                 steer=" Still time on YouTube.",
                                 end=7, entries=n) for n in range(40)}
        bank = {v.format(label="Reddit", steer=" Still time on YouTube.", end=7)
                for v in budget.GATE_LINES[state]}
        assert seen <= bank, f"{state} produced a line outside its bank: {seen - bank}"


def test_the_hour_may_speak_over_the_bank_but_only_at_night(rdb):
    """The time-aware lines deliberately sit outside the banks — that is the point of
    them. Asserted separately so the bank check above can stay strict."""
    late = time.mktime((2026, 6, 15, 22, 30, 0, 0, 0, -1))
    lines = {budget.gate_line("winddown", now=late, label="R", entries=n) for n in range(30)}
    assert any(l not in budget.GATE_LINES["winddown"] for l in lines), "hour never speaks"


def test_the_steer_survives_every_variant(rdb):
    """Whichever wording comes up, 'you still have time on YouTube' must reach the user."""
    assert budget.GATE_LINES['spent'], ("budget.GATE_LINES['spent'] is empty, so the loop below would assert nothing and this test would pass having checked nothing")
    for v in budget.GATE_LINES["spent"]:
        assert "{steer}" in v


def test_gate_copy_rotates_but_cannot_be_rerolled_by_refreshing(rdb):
    """Same rule as the reflection prompt: stable while you stare at it, different next
    session. Something that changes on refresh is a slot machine."""
    day = time.strftime("%Y-%m-%d")
    rdb.set(f"entries:{day}", 3)
    fixed = {budget.gate_line("day", label="Reddit") for _ in range(15)}
    assert len(fixed) == 1, "the line changed on refresh"
    varied = {budget.gate_line("day", label="Reddit", entries=n) for n in range(25)}
    assert len(varied) > 3, "the line barely rotates across sessions"


def test_every_bank_is_non_empty_and_formats_cleanly(rdb):
    assert budget.GATE_LINES.items(), ("budget.GATE_LINES.items() is empty, so the loop below would assert nothing and this test would pass having checked nothing")
    for state, bank in budget.GATE_LINES.items():
        assert bank, state
        for v in bank:
            v.format(label="Reddit", steer=" x", end=7)      # must not raise


def test_data_moments_say_something_true_or_nothing(rdb):
    now = time.time()
    assert budget._moment_visits({"entries": 0}) is None      # nothing to report
    assert "3" in budget._moment_visits({"entries": 2})
    assert budget._moment_late_cooldowns({}, now) is None     # no history yet
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    for h in (21, 22, 23):
        rdb.rpush(f"cooldown_events:{day}", f"{now - (24 - h) * 3600:.0f} reddit")
    line = budget._moment_late_cooldowns({}, now)
    assert line and "3" in line


def test_blunt_lines_are_a_fallback_not_the_default(rdb):
    """If a moment fires but there is nothing true to say, you get a dry one-liner —
    never an empty line, never a fabricated statistic."""
    assert len(budget.BLUNT_LINES) >= 8
    for line in budget.BLUNT_LINES:
        assert not any(ch.isdigit() for ch in line), f"blunt line implies data: {line}"


# ---------- 20. the four spontaneity touches ----------

def test_the_status_bar_tint_follows_the_theme(rdb, client):
    """Hardcoded, it left Christmas repainting the whole gate and then sitting under a
    cold grey bar — the one piece of chrome a phone user always has in frame."""
    for theme, expect in [("off", "#070b0e"), ("christmas", "#0a1410"),
                          ("terminal", "#080a07")]:
        html = client.get(f"/budget?site=reddit&theme={theme}").get_data(as_text=True)
        assert f'name="theme-color" content="{expect}"' in html, theme


def test_the_pass_screen_rotates(rdb):
    seen = set()
    day = time.strftime("%Y-%m-%d")
    for n in range(20):
        rdb.set(f"entries:{day}", n)
        seen.add(budget.pass_line())
    assert len(seen) > 3, "the highest-value screen in the product barely varies"
    assert all(len(v) == 3 for v in seen)                 # kicker, title, body


def test_the_pass_copy_is_rendered_server_side_not_smuggled_into_script(rdb, client, monkeypatch):
    """It lives in HTML where Jinja escapes it, rather than as JSON inside a <script>.
    Only rendered when the reflection prompt is — there is no 'you passed' screen
    without a prompt to pass on."""
    monkeypatch.setattr(budget, "reflect_decision", lambda now=None: (True, "Why now?"))
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert 'id="passMsg"' in html
    # Compare against the ESCAPED form -- which is the property being asserted. Two of
    # the six lines contain an apostrophe, so the raw-string version of this failed on a
    # third of all days, silently, since the copy was added. Note markupsafe (&#39;) not
    # html.escape (&#x27;): Jinja uses the former, and the stdlib version would fail on
    # exactly the same days for a different reason.
    from markupsafe import escape
    assert any(str(escape(t)) in html for _, t, _ in budget.PASS_LINES)
    assert "PASS_LINES" not in html and "JSON.parse" not in html


def test_every_pass_line_survives_rendering(rdb, client, monkeypatch):
    """Pin each line in turn. The seeded picker shows one per day, so a line that breaks
    rendering would only surface on the day it came up -- which is how the apostrophe
    problem stayed hidden."""
    from markupsafe import escape
    monkeypatch.setattr(budget, "reflect_decision", lambda now=None: (True, "Why now?"))
    assert budget.PASS_LINES, ("budget.PASS_LINES is empty, so the loop below would assert nothing and this test would pass having checked nothing")
    for entry in budget.PASS_LINES:
        monkeypatch.setattr(budget, "pass_line", lambda *a, **k: entry)
        html = client.get("/budget?site=reddit").get_data(as_text=True)
        assert str(escape(entry[1])) in html, f"{entry[1]!r} did not render"


def test_no_pass_screen_when_there_is_no_prompt(rdb, client):
    assert 'id="passMsg"' not in client.get("/budget?site=reddit").get_data(as_text=True)


def test_a_seasonal_theme_can_speak_but_does_not_take_over(rdb):
    xmas = time.mktime((2026, 12, 24, 14, 0, 0, 0, 0, -1))
    lines = {budget.gate_line("day", now=xmas, label="Reddit", entries=n) for n in range(40)}
    themed = set(budget.THEMES["christmas"]["lines"])
    assert lines & themed, "the season never gets a word in"
    assert lines - themed, "the season drowned out the normal copy"


def test_the_night_gate_can_be_blunt_about_the_hour(rdb):
    night = time.mktime((2026, 8, 5, 2, 0, 0, 0, 0, -1))
    lines = {budget.gate_line("night_closed", now=night, label="Reddit", end=7, entries=n)
             for n in range(30)}
    assert any("2am" in l for l in lines), "2am is the one fact worth stating at 2am"
    noon = time.mktime((2026, 8, 5, 14, 0, 0, 0, 0, -1))
    day_lines = {budget.gate_line("day", now=noon, label="Reddit", entries=n) for n in range(30)}
    assert not any("am." in l or "Go to bed" in l for l in day_lines), "told to sleep at 2pm"


# ---------- 21. personal themes must not leak the date ----------

def test_no_personal_date_is_committed_to_the_repository():
    """A birthday hardcoded here would be public on GitHub forever, in the history and
    indexed — a certain disclosure, and a far bigger exposure than anything on the box.
    It comes from the environment on the box alone."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py")).read()
    assert 'os.environ.get("COOLDOWN_BIRTHDAY"' in src
    assert not re.search(r'BIRTHDAY\s*=\s*["\']\d{2}-\d{2}["\']', src), "a date is hardcoded"


def test_the_birthday_theme_is_unlabelled_on_the_gated_origin(rdb):
    """The gate is served on reddit.com, so any script there can fetch it. A cake emoji
    would hand that script the date once a year; nice colours reveal nothing."""
    assert budget.PERSONAL_THEMES, ("budget.PERSONAL_THEMES is empty, so the loop below would assert nothing and this test would pass having checked nothing")
    for name in budget.PERSONAL_THEMES:
        assert budget.THEMES[name]["emoji"] == "", name
        assert budget.THEMES[name]["label"] == "", name


def test_the_greeting_lives_only_on_the_box_origin(rdb, client, monkeypatch):
    monkeypatch.setattr(budget, "active_theme",
                        lambda now=None, override=None: ("birthday", budget.THEMES["birthday"]))
    gate = client.get("/budget?site=reddit").get_data(as_text=True)
    assert "birthday" not in gate.lower(), "the gate names the occasion"
    assert "Happy birthday" in client.get("/wrapped").get_data(as_text=True)
    assert "/wrapped" in addon.MOVED_TO_BOX          # unreachable from a gated site


def test_personal_themes_never_appear_at_random(rdb):
    """They are date-driven; they just carry no season predicate because the date is not
    in this file. Without the guard your birthday theme turns up on a wet Tuesday."""
    base = time.mktime((2027, 1, 1, 12, 0, 0, 0, 0, -1))
    monkey = {budget.active_theme(base + i * 86400)[0] for i in range(365)}
    assert not (monkey & budget.PERSONAL_THEMES), monkey & budget.PERSONAL_THEMES


def test_the_birthday_fires_when_the_env_var_says_so(rdb, monkeypatch):
    monkeypatch.setattr(budget, "BIRTHDAY", "03-14")
    on = time.mktime((2027, 3, 14, 12, 0, 0, 0, 0, -1))
    off = time.mktime((2027, 3, 15, 12, 0, 0, 0, 0, -1))
    assert budget.active_theme(on)[0] == "birthday"
    assert budget.active_theme(off)[0] != "birthday"


def test_the_two_new_surfaces_keep_the_charts_valid():
    """Same rule as every other theme: --card stays dark or the validated palettes stop
    being valid."""
    def lum(h):
        r_, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r_) + 0.7152 * f(g) + 0.0722 * f(b)
    for n in ("ember", "slate", "birthday", "anniversary"):
        assert lum(budget.THEMES[n]["vars"]["card"]) <= lum("#14171d") * 1.6, n


# ---------- 22. history pages read in batches, not one key at a time ----------

def _count_roundtrips(client, page):
    import redis as _r
    calls = {"n": 0}
    real = _r.Redis.execute_command
    def counting(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)
    _r.Redis.execute_command = counting
    try:
        client.get(page)
    finally:
        _r.Redis.execute_command = real
    return calls["n"]


@pytest.mark.parametrize("page,ceiling", [
    ("/wrapped", 30), ("/stats", 25), ("/digest", 20), ("/health", 15),
])
def test_history_pages_do_not_read_one_key_per_day(rdb, client, page, ceiling):
    """/wrapped walked a year one key at a time: 2,924 sequential round-trips for one
    render, two thirds of the page's time spent in socket wait. MGET and pipelines took
    it to 8. The ceilings here are generous — they exist to catch a reintroduced loop,
    not to police an exact number."""
    now = time.time()
    pipe = rdb.pipeline(transaction=False)
    for i in range(365):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for s in budget.SITES:
            pipe.set(f"usage:{d}:{s}", 600)
        pipe.rpush(f"cooldown_events:{d}", f"{now:.0f} reddit")
        pipe.rpush(f"reflect:{d}", f"{now:.0f} tired enter")
        pipe.rpush(f"worth:{d}", f"{now:.0f} tired no")
    pipe.execute()
    n = _count_roundtrips(client, page)
    assert n <= ceiling, f"{page} made {n} round-trips for a year of history"


def test_batched_reads_survive_a_dead_redis(rdb, monkeypatch):
    """A dashboard is not worth a 500. Both helpers degrade to empty."""
    class Dead:
        def mget(self, *a, **k): raise ConnectionError("down")
        def pipeline(self, *a, **k): raise ConnectionError("down")
    monkeypatch.setattr(budget, "r", Dead())
    assert budget._mget(["a", "b", "c"]) == [None, None, None]
    assert budget._mget_floats(["a", "b"]) == [0.0, 0.0]
    assert budget._mlrange(["a", "b"]) == [[], []]


def test_day_keys_are_ordered_oldest_first(rdb):
    now = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))
    keys = budget._day_keys("usage", 3, now, ["reddit"])
    assert keys == ["usage:2026-06-13:reddit", "usage:2026-06-14:reddit",
                    "usage:2026-06-15:reddit"], keys


# ---------- patch state on /health ----------

def test_updates_reports_unknown_when_the_hourly_check_has_not_run(tmp_path, monkeypatch):
    """A missing report must not read as a clean one. The watchdog that reports on apt's
    timers can stall exactly the way apt's timers did."""
    monkeypatch.setattr(budget, "UPDATES_STATE", str(tmp_path / "nope.json"))
    u = budget._updates()
    assert u["fresh"] is False and u["pending"] is None


def test_updates_goes_stale_rather_than_reporting_old_counts(tmp_path, monkeypatch):
    f = tmp_path / "u.json"
    f.write_text(json.dumps({"pending": 4, "security": 1, "reboot_required": False,
                             "checked": time.time() - 9 * 3600}))
    monkeypatch.setattr(budget, "UPDATES_STATE", str(f))
    assert budget._updates()["fresh"] is False


def test_updates_reads_a_fresh_report(tmp_path, monkeypatch):
    f = tmp_path / "u.json"
    f.write_text(json.dumps({"pending": 19, "security": 3, "reboot_required": True,
                             "last_run": time.time() - 7200, "last_result": "success",
                             "checked": time.time() - 120}))
    monkeypatch.setattr(budget, "UPDATES_STATE", str(f))
    u = budget._updates()
    assert u["fresh"] is True and u["pending"] == 19 and u["security"] == 3
    assert u["reboot_required"] is True and u["last_run_ago"] >= 7200


def test_updates_survives_a_corrupt_report(tmp_path, monkeypatch):
    """It sits in the health path. Garbage on disk must not 500 the dashboard."""
    f = tmp_path / "u.json"
    f.write_text("{not json at all")
    monkeypatch.setattr(budget, "UPDATES_STATE", str(f))
    assert budget._updates()["fresh"] is False


def test_health_page_shows_reboot_required(client, monkeypatch):
    html = _health_html_with(client, monkeypatch,
                             {"fresh": True, "pending": 19, "security": 3,
                              "reboot_required": True, "last_result": "success",
                              "last_run_ago": 60, "checked_ago": 60})
    assert "Reboot required" in html


def test_health_page_shows_pending_security_count(client, monkeypatch):
    html = _health_html_with(client, monkeypatch,
                             {"fresh": True, "pending": 19, "security": 3,
                              "reboot_required": False, "last_result": "success",
                              "last_run_ago": 60, "checked_ago": 60})
    assert "security" in html and "<b>19</b> pending" in html


def test_health_page_says_when_it_cannot_tell(client, monkeypatch):
    html = _health_html_with(client, monkeypatch,
                             {"fresh": False, "pending": None, "security": None,
                              "reboot_required": False, "checked_ago": 4 * 3600})
    assert "Update status unknown" in html
    assert "Up to date" not in html


def test_health_page_says_fully_patched_when_it_is(client, monkeypatch):
    html = _health_html_with(client, monkeypatch,
                             {"fresh": True, "pending": 0, "security": 0,
                              "reboot_required": False, "last_result": "success",
                              "last_run_ago": 60, "checked_ago": 60})
    assert "Up to date" in html


def test_updates_reports_a_boot_that_never_completed(tmp_path, monkeypatch):
    """The signal the watchdog was missing for eleven days.

    A timer can fire exactly on schedule and achieve nothing if its job then queues
    behind a target that is never reached -- which is what happened, while every other
    check here read healthy.
    """
    f = tmp_path / "u.json"
    f.write_text(json.dumps({"pending": 0, "security": 0, "reboot_required": False,
                             "boot_ok": False, "stuck_jobs": 2,
                             "checked": time.time() - 60}))
    monkeypatch.setattr(budget, "UPDATES_STATE", str(f))
    u = budget._updates()
    assert u["boot_ok"] is False and u["stuck_jobs"] == 2


def test_updates_assumes_boot_is_fine_when_an_old_report_lacks_the_field(tmp_path, monkeypatch):
    """A state file written before this field existed must not read as a broken boot."""
    f = tmp_path / "u.json"
    f.write_text(json.dumps({"pending": 3, "checked": time.time() - 60}))
    monkeypatch.setattr(budget, "UPDATES_STATE", str(f))
    assert budget._updates()["boot_ok"] is True


def test_health_page_reports_a_jammed_boot_above_everything_else(client, monkeypatch):
    """When jobs cannot run, the patch counts are meaningless -- 'Fully patched' would be
    actively misleading, because nothing can install. The jam has to outrank them."""
    html = _health_html_with(client, monkeypatch,
                             {"fresh": True, "pending": 0, "security": 0,
                              "reboot_required": False, "boot_ok": False, "stuck_jobs": 2,
                              "last_result": "success", "last_run_ago": 60, "checked_ago": 60})
    assert "Boot never completed" in html
    assert "2 timers fired without executing" in html
    assert "Fully patched" not in html


def test_health_page_reports_stuck_jobs_even_when_boot_looks_fine(client, monkeypatch):
    """multi-user.target can be active while an individual job is still wedged."""
    html = _health_html_with(client, monkeypatch,
                             {"fresh": True, "pending": 0, "security": 0,
                              "reboot_required": False, "boot_ok": True, "stuck_jobs": 1,
                              "last_result": "success", "last_run_ago": 60, "checked_ago": 60})
    assert "1 timer fired without executing" in html


# ---------- the weekly security audit ----------

def _health_html_with_updates(client, monkeypatch, updates):
    monkeypatch.setattr(budget, "collect_health", lambda *a, **k: dict(_HEALTH_BASE, updates=updates))
    return client.get("/health").get_data(as_text=True)


def _audit_html(client, monkeypatch, audit):
    monkeypatch.setattr(budget, "collect_health", lambda *a, **k: dict(_HEALTH_BASE, audit=audit))
    return client.get("/health").get_data(as_text=True)


def test_audit_reports_unknown_when_it_has_not_run(tmp_path, monkeypatch):
    """A missing audit must not read as a passing one -- the whole reason this exists is
    that 'no records' got mistaken for 'nothing happened'."""
    monkeypatch.setattr(budget, "AUDIT_STATE", str(tmp_path / "nope.json"))
    a = budget._audit()
    assert a["fresh"] is False and a["findings"] is None


def test_audit_goes_stale_rather_than_reporting_old_results(tmp_path, monkeypatch):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"findings": 0, "checked": time.time() - 30 * 86400}))
    monkeypatch.setattr(budget, "AUDIT_STATE", str(f))
    assert budget._audit()["fresh"] is False


def test_audit_reads_findings(tmp_path, monkeypatch):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"findings": 2, "ssh_keys": 1, "exposed_ports": "22 ",
                             "tampered_files": 0, "journal_persistent": True,
                             "checked": time.time() - 3600}))
    monkeypatch.setattr(budget, "AUDIT_STATE", str(f))
    a = budget._audit()
    assert a["fresh"] is True and a["findings"] == 2 and a["exposed_ports"] == "22"


def test_audit_survives_a_corrupt_report(tmp_path, monkeypatch):
    f = tmp_path / "a.json"
    f.write_text("{ not json")
    monkeypatch.setattr(budget, "AUDIT_STATE", str(f))
    assert budget._audit()["fresh"] is False


def test_health_page_names_the_exposed_port(client, monkeypatch):
    """The exact finding from tonight: port 22 open to the LAN while the dashboard
    called it firewalled. It has to be named, not merely counted."""
    html = _audit_html(client, monkeypatch,
                       {"fresh": True, "findings": 1, "ssh_keys": 1, "exposed_ports": "22",
                        "tampered_files": 0, "journal_persistent": True, "checked_ago": 3600})
    assert "1 audit finding" in html and "open to the LAN: 22" in html


def test_health_page_says_when_the_audit_is_missing(client, monkeypatch):
    html = _audit_html(client, monkeypatch,
                       {"fresh": False, "findings": None, "exposed_ports": "",
                        "journal_persistent": True, "checked_ago": 20 * 86400})
    assert "Security audit has not run" in html
    assert "Security invariants hold" not in html


def test_health_page_confirms_a_clean_audit(client, monkeypatch):
    html = _audit_html(client, monkeypatch,
                       {"fresh": True, "findings": 0, "ssh_keys": 1, "exposed_ports": "",
                        "tampered_files": 0, "journal_persistent": True, "checked_ago": 3600})
    assert "Security invariants hold" in html


def test_short_ago_reads_like_a_human_wrote_it():
    assert budget._short_ago(30) == "just now"
    assert budget._short_ago(600) == "10 min ago"
    assert budget._short_ago(7200) == "2 h ago"
    assert budget._short_ago(3 * 86400) == "3 days ago"
    assert budget._short_ago(-5) == "just now"      # clock skew must not print a negative


def test_health_page_says_when_it_last_checked(client, monkeypatch):
    """"Fully patched" alone does not say whether that was five minutes or five weeks
    ago, which is the difference between reassuring and meaningless."""
    html = _health_html_with(client, monkeypatch,
                             {"fresh": True, "pending": 0, "security": 0,
                              "reboot_required": False, "boot_ok": True, "stuck_jobs": 0,
                              "packages": [], "pkg_total": 0, "checked_human": "12 min ago",
                              "last_result": "success", "last_run_ago": 60, "checked_ago": 720})
    assert "Up to date" in html and "Checked 12 min ago" in html


def test_health_page_names_the_pending_packages(client, monkeypatch):
    """A count does not tell you whether it matters; a kernel in the list does."""
    html = _health_html_with(client, monkeypatch,
                             {"fresh": True, "pending": 12, "security": 2,
                              "reboot_required": False, "boot_ok": True, "stuck_jobs": 0,
                              "packages": ["linux-image-rpi-v8", "openssl", "tailscale"],
                              "pkg_total": 12, "checked_human": "5 min ago",
                              "last_result": "success", "last_run_ago": 60, "checked_ago": 300})
    assert "linux-image-rpi-v8, openssl, tailscale" in html
    assert "and 9 more" in html


def test_audit_surfaces_the_expiries_that_would_lock_you_out(tmp_path, monkeypatch):
    """Two dates can silently end access: the Tailscale key (port 22 is tailnet-only)
    and the CA (every gated site breaks at once). Both belong on the page."""
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"findings": 0, "ssh_keys": 1, "exposed_ports": "",
                             "ts_days": 126, "ca_days": 3595, "backup_age": 0,
                             "mode": "quick", "checked": time.time() - 300}))
    monkeypatch.setattr(budget, "AUDIT_STATE", str(f))
    a = budget._audit()
    assert a["ts_days"] == 126 and a["ca_days"] == 3595 and a["backup_age"] == 0
    assert a["checked_human"] == "5 min ago"


def test_health_page_shows_the_cheap_tier_recency(client, monkeypatch):
    html = _audit_html(client, monkeypatch,
                       {"fresh": True, "findings": 0, "ssh_keys": 1, "exposed_ports": "",
                        "tampered_files": 0, "journal_persistent": True, "checked_ago": 300,
                        "checked_human": "5 min ago", "ca_days": 3595, "ts_days": 126,
                        "backup_age": 0, "mode": "quick"})
    assert "Checked 5 min ago" in html
    assert "tailnet key 126d" in html and "CA valid 3595d" in html


# ---------- every line of copy, not just today's ----------

def _banks():
    """(bank name, line) for every piece of rotating copy."""
    for state, lines in budget.GATE_LINES.items():
        for t in lines:
            yield f"GATE_LINES[{state}]", t
    for t in budget.BLUNT_LINES:
        yield "BLUNT_LINES", t
    for entry in budget.PASS_LINES:
        yield "PASS_LINES", entry[1]


def test_every_line_of_copy_is_escaped_when_rendered(rdb, client, monkeypatch):
    """No bank may be rendered with |safe.

    17 of 31 GATE_LINES contain characters that escape differently. If any bank were
    ever marked |safe, a line containing an angle bracket would inject markup — and
    because the picker is seeded per day, it would only do so on that line's day.
    Pinning each line in turn is the only way to see the whole surface at once.
    """
    monkeypatch.setattr(budget, "reflect_decision", lambda now=None: (True, "Why now?"))
    from markupsafe import escape
    unescaped = []
    for name, text in _banks():
        if str(escape(text)) == text:
            continue                     # nothing to escape; nothing to prove
        monkeypatch.setattr(budget, "gate_line", lambda *a, **k: text)
        monkeypatch.setattr(budget, "pass_line", lambda *a, **k: ("", text, ""))
        html = client.get("/budget?site=reddit").get_data(as_text=True)
        if text in html:                 # raw form present => rendered unescaped
            unescaped.append(f"{name}: {text[:50]!r}")
    assert not unescaped, "copy rendered without escaping:\n  " + "\n  ".join(unescaped)


def test_the_copy_banks_are_not_empty():
    """A bank that lost its contents would make the test above vacuously pass."""
    assert len(budget.BLUNT_LINES) >= 5
    assert len(budget.PASS_LINES) >= 3
    assert sum(len(v) for v in budget.GATE_LINES.values()) >= 20
    for state, lines in budget.GATE_LINES.items():
        assert lines, f"GATE_LINES[{state}] is empty"


def test_audit_reports_whether_the_backup_actually_restores(tmp_path, monkeypatch):
    """A backup nobody has restored is a hypothesis. -1 means the weekly tier has not
    run yet, and must not read as a pass."""
    f = tmp_path / "a.json"
    for value, expect in ((1, 1), (0, 0)):
        f.write_text(json.dumps({"findings": 0, "backup_restores": value,
                                 "checked": time.time() - 60}))
        monkeypatch.setattr(budget, "AUDIT_STATE", str(f))
        assert budget._audit()["backup_restores"] == expect
    f.write_text(json.dumps({"findings": 0, "checked": time.time() - 60}))
    assert budget._audit()["backup_restores"] == -1     # absent != verified


def test_health_page_distinguishes_verified_from_never_checked(client, monkeypatch):
    base = {"fresh": True, "findings": 0, "ssh_keys": 1, "exposed_ports": "",
            "tampered_files": 0, "journal_persistent": True, "checked_ago": 300,
            "checked_human": "5 min ago", "ca_days": 3595, "ts_days": 126,
            "backup_age": 0, "mode": "full"}
    ok = _audit_html(client, monkeypatch, dict(base, backup_restores=1))
    assert "restore verified" in ok
    unknown = _audit_html(client, monkeypatch, dict(base, backup_restores=-1))
    assert "restore verified" not in unknown and "restore FAILED" not in unknown
    bad = _audit_html(client, monkeypatch, dict(base, backup_restores=0))
    assert "restore FAILED" in bad


def test_health_page_says_how_pending_updates_will_be_installed(client, monkeypatch):
    """"6 pending" alone tells you a number and no plan. The row has to say what happens
    next, and the schedule comes from the timer rather than being written in the page --
    a hardcoded time is a claim that drifts, which is how the firewalled-ports set ended
    up asserting a control that did not exist."""
    html = _health_html_with_updates(client, monkeypatch,
        {"fresh": True, "pending": 6, "security": 0, "reboot_required": False,
         "boot_ok": True, "stuck_jobs": 0, "packages": ["libdrm2", "libgbm1"],
         "pkg_total": 6, "checked_human": "5 min ago",
         "next_install": "tonight at 3:27 am", "auto_reboot": "04:00",
         "last_result": "success", "last_run_ago": 60, "checked_ago": 300})
    assert "Installs automatically tonight at 3:27 am" in html
    assert "reboots 04:00" in html
    assert "libdrm2, libgbm1" in html and "and 4 more" in html


def test_health_page_does_not_invent_a_schedule_it_does_not_know(client, monkeypatch):
    """If the timer never reported a next run, say nothing rather than guess a time."""
    html = _health_html_with_updates(client, monkeypatch,
        {"fresh": True, "pending": 6, "security": 0, "reboot_required": False,
         "boot_ok": True, "stuck_jobs": 0, "packages": [], "pkg_total": 6,
         "checked_human": "5 min ago", "next_install": "", "auto_reboot": "",
         "last_result": "success", "last_run_ago": 60, "checked_ago": 300})
    assert "Installs automatically." in html
    assert "reboots" not in html


# ---------- D2: off-box alert on an unexplained reboot ----------

def test_alert_is_off_unless_the_url_is_set(rdb, monkeypatch):
    """Off by default is the whole shape of this feature. No URL, no thread, no send."""
    monkeypatch.setattr(budget, "ALERT_URL", "")
    sent = []
    monkeypatch.setattr(budget.urllib.request, "urlopen", lambda *a, **k: sent.append(1))
    assert budget.send_alert("anything") is False
    assert sent == []


def test_alert_carries_no_address_token_or_hostname(rdb, monkeypatch):
    """It goes to a third party by definition. Putting the box's address in it would
    repeat the Referer leak that earned Referrer-Policy in the first place."""
    captured = {}

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req_, timeout=None):
        captured["body"] = req_.data.decode()
        return FakeResp()

    monkeypatch.setattr(budget, "ALERT_URL", "http://example.invalid/hook")
    monkeypatch.setattr(budget.urllib.request, "urlopen", fake_urlopen)
    rdb.set("last_boot_id", "old-boot-id")
    monkeypatch.setattr(budget, "_first_line", lambda *a, **k: "new-boot-id")
    budget.boot_watch()
    for _ in range(50):
        if "body" in captured:
            break
        time.sleep(0.05)
    body = captured.get("body", "")
    assert body, "no alert was sent on an unexplained reboot"
    assert "reboot" in body
    for leak in ("100.", "tail", ".ts.net", "token", "tok="):
        assert leak not in body.lower(), f"alert payload contains {leak!r}: {body!r}"


def test_a_failed_alert_is_recorded_not_swallowed(rdb, monkeypatch):
    """A send that silently failed is indistinguishable from no alert, which is the exact
    failure this feature exists to prevent."""
    monkeypatch.setattr(budget, "ALERT_URL", "http://example.invalid/hook")

    def boom(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr(budget.urllib.request, "urlopen", boom)
    assert budget.send_alert("test") is True
    for _ in range(50):
        if rdb.get("alert_last"):
            break
        time.sleep(0.05)
    last = rdb.get("alert_last") or ""
    assert "failed" in last, f"a failed send left {last!r}"


def test_alert_never_raises_into_the_request(rdb, monkeypatch):
    """boot_watch() runs inside a request. The alert path must not be able to break it."""
    monkeypatch.setattr(budget, "ALERT_URL", "http://example.invalid/hook")
    monkeypatch.setattr(budget.threading, "Thread",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no threads")))
    rdb.set("last_boot_id", "old")
    monkeypatch.setattr(budget, "_first_line", lambda *a, **k: "new")
    budget.boot_watch()                       # must not raise
    assert rdb.get("unacked_boot") is not None, "the boot was still recorded locally"


def test_a_failed_apt_simulation_never_renders_as_up_to_date(rdb, tmp_path, monkeypatch):
    """cooldown-updates.sh ran `apt-get -s dist-upgrade 2>/dev/null || true`, so any apt
    failure produced an empty simulation, which counts as zero pending, which this page
    rendered as "Up to date". Proven with a stub apt exiting 100.

    That is absence-of-signal read as good news, in the one script whose entire job is
    noticing that patching stopped. The writer now records apt_ok; this asserts the page
    cannot say "up to date" when it is false."""
    import json as _json
    state = tmp_path / "updates.json"
    state.write_text(_json.dumps({
        "apt_ok": False, "pending": 0, "security": 0, "reboot_required": False,
        "boot_ok": True, "stuck_jobs": 0, "checked": time.time(), "packages": "",
    }))
    monkeypatch.setattr(budget, "UPDATES_STATE", str(state))
    # /health is cached to collapse concurrent pollers, so without this the previous
    # test's page is served to this one. Found by these two tests disagreeing depending
    # on order -- item 1.7's class, in tests written to fix a different bug.
    budget._HEALTH_CACHE.clear()

    u = budget._updates()
    assert u["pending"] is None, "a failed simulation must not report a count of zero"
    assert u["security"] is None

    page = budget.app.test_client().get("/health", base_url="http://100.64.0.1:5000").data.decode()
    assert "Up to date" not in page, "a failed apt simulation is being rendered as up to date"
    assert "pending count unknown" in page


def test_a_successful_apt_simulation_with_nothing_pending_still_says_up_to_date(rdb, tmp_path, monkeypatch):
    """The other direction, so the fix above cannot be satisfied by never saying it."""
    import json as _json
    state = tmp_path / "updates.json"
    state.write_text(_json.dumps({
        "apt_ok": True, "pending": 0, "security": 0, "reboot_required": False,
        "boot_ok": True, "stuck_jobs": 0, "checked": time.time(), "packages": "",
    }))
    monkeypatch.setattr(budget, "UPDATES_STATE", str(state))
    budget._HEALTH_CACHE.clear()
    assert budget._updates()["pending"] == 0
    page = budget.app.test_client().get("/health", base_url="http://100.64.0.1:5000").data.decode()
    assert "Up to date" in page


def test_the_restart_record_survives_dismissing_the_banner(rdb):
    """1.6: boot_events was written on every unexplained restart and read by nothing.

    The only surfacing was unacked_boot, a single flag the dismiss button deletes -- so
    acknowledging a reboot erased the evidence it happened, which is the opposite of what
    tamper-evidence is for. The list must outlive the dismissal and stay on the page."""
    now = time.time()
    rdb.rpush("boot_events", f"{now - 7200:.0f}", f"{now - 300:.0f}")
    rdb.set("unacked_boot", f"{now - 300:.0f}")
    budget._HEALTH_CACHE.clear()

    c = budget.app.test_client()
    page = c.get("/health", base_url="http://100.64.0.1:5000").data.decode()
    assert "This box restarted" in page, "the banner should be up before dismissal"
    assert "restarts on record" in page

    c.post("/boot-ack", base_url="http://100.64.0.1:5000")
    budget._HEALTH_CACHE.clear()
    page = c.get("/health", base_url="http://100.64.0.1:5000").data.decode()

    assert "This box restarted" not in page, "dismissing should clear the banner"
    assert "restarts on record" in page, (
        "dismissing the banner also erased the restart history — the record has to "
        "outlive the acknowledgement or there is nothing left to audit")
    assert budget.boot_history(), "boot_events must still hold the entries"
