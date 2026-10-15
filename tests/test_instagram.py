"""Instagram: the feed is budgeted, DMs and your own profile are free.

Instagram is an SPA, so the split lives in two places that must agree on every path --
addon.instagram_path_is_free() in the proxy and the same list substituted into the injected
script. Half of what follows exists to make that agreement a tested fact: the JS is run in
node against the Python classifier, because two implementations of one rule drift, and the
failure here is not a wrong answer but a reload loop.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
import redis
from mitmproxy.test import tflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "deploy"))
import addon  # noqa: E402
import app as budget  # noqa: E402
import gen_allow_hosts  # noqa: E402

HANDLE = "mine"
HTML = "<html><body>page</body></html>"


@pytest.fixture()
def rdb(rdb, monkeypatch):
    """conftest's db-15 client (which also points app.py at it), shared with the addon."""
    monkeypatch.setattr(addon, "r", rdb)
    monkeypatch.setattr(addon, "_SHADOW", {})
    return rdb


@pytest.fixture(autouse=True)
def _handle(monkeypatch):
    monkeypatch.setattr(addon, "INSTAGRAM_SELF", HANDLE)


@pytest.fixture(autouse=True)
def _no_ambient_forwarding(monkeypatch):
    """Same reason as test_addon.py: never reach whatever listens on 127.0.0.1:5000."""
    def refuse(url, *a, **k):
        raise addon.req.exceptions.ConnectionError(f"ambient forwarding blocked: {url}")
    monkeypatch.setattr(addon.req, "get", refuse)
    monkeypatch.setattr(addon.req, "post", refuse)


def mkflow(path="/", host="www.instagram.com", ctype="text/html", body=HTML,
           headers=None, method="GET", nav=True):
    f = tflow.tflow(resp=True)
    f.request.host = host
    f.request.path = path
    f.request.method = method
    if nav:
        f.request.headers["Sec-Fetch-Mode"] = "navigate"
        f.request.headers["Sec-Fetch-Dest"] = "document"
    else:
        f.request.headers["Sec-Fetch-Mode"] = "cors"
        f.request.headers["Sec-Fetch-Dest"] = "empty"
    for k, v in (headers or {}).items():
        f.request.headers[k] = v
    f.response.headers["content-type"] = ctype
    if body is not None:
        f.response.text = body
    return f


def stub_gate(monkeypatch, body=b"<html>THE GATE</html>"):
    class Resp:
        status_code = 200
        content = body
    monkeypatch.setattr(addon.req, "get", lambda *a, **k: Resp())


def run_request(flow):
    """Run request() and report whether it answered for the site (a gate / 503) or let
    the request through to Instagram. tflow flows start with a response attached, so
    'answered' is detected by the response having been REPLACED."""
    flow.response = None
    addon.BudgetAddon().request(flow)
    return flow.response


FREE_PATHS = ["/direct", "/direct/", "/direct/inbox/", "/direct/t/123/", "/direct/new/",
              f"/{HANDLE}", f"/{HANDLE}/", f"/{HANDLE.upper()}/", f"/{HANDLE}/followers/",
              f"/{HANDLE}/following/", "/accounts/login/", "/accounts/activity/",
              "/accounts/", "/Direct/Inbox/", "/direct/?x=1", f"/{HANDLE}/?hl=en"]
BUDGETED_PATHS = ["/", "/?variant=following", "/explore/", "/explore/search/", "/reels/",
                  "/reel/x/", "/p/x/", "/stories/x/1/", "/someoneelse/",
                  f"/{HANDLE}x/", f"/x{HANDLE}/", "/directory/", "/directs/", "/account/",
                  "/accountsx/", "", "/?next=/direct/", "/someoneelse/direct/",
                  f"/explore/{HANDLE}/",
                  # Parser-differential shapes: urlparse would read "direct" / "mine" as a
                  # HOST here, and classify by the rest.
                  "//direct/", "//direct", "//evil/direct/", f"//{HANDLE}/", "///direct/"]


# ---------- hosts ----------

@pytest.mark.parametrize("host,expect", [
    ("www.instagram.com", "instagram"),
    ("WWW.INSTAGRAM.COM", "instagram"),
    ("www.instagram.com:443", "instagram"),
    # The CA vouches for the page host only. These stay tunneled, so they must not be
    # gated or decrypted (DM realtime, API host, media).
    ("instagram.com", None),
    ("i.instagram.com", None),
    ("graph.instagram.com", None),
    ("edge-chat.instagram.com", None),
    ("static.cdninstagram.com", None),
    ("scontent.cdninstagram.com", None),
    ("evil-instagram.com", None),
    ("notwww.instagram.com.evil.io", None),
    ("www.instagram.com.attacker.io", None),
])
def test_only_the_page_host_is_gated(host, expect):
    assert addon.site_for_host(host) == expect


def _unit_allow_hosts():
    text = open(os.path.join(ROOT, "deploy", "cooldown-proxy.service")).read()
    m = re.search(r'--allow-hosts "([^"]*)"', text)
    assert m, "no --allow-hosts in the unit; every assertion below would be vacuous"
    return m.group(1)


def test_the_units_allowlist_is_what_the_generator_emits():
    """CLAUDE.md: FOUR places must agree. Nothing compared the unit's regex to its
    generator, so a site added to CORE and never pasted into the unit passed every test
    and tunneled on the box, un-gated, with nothing saying why."""
    assert _unit_allow_hosts() == subprocess.run(
        [sys.executable, os.path.join(ROOT, "deploy", "gen_allow_hosts.py")],
        capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.parametrize("host,intercepted", [
    ("www.instagram.com", True),
    ("www.instagram.com:443", True),
    ("instagram.com", False),
    ("i.instagram.com", False),
    ("edge-chat.instagram.com", False),
    ("graph.instagram.com", False),
    ("static.cdninstagram.com", False),
    ("evil-instagram.com", False),
    ("www.instagram.com.attacker.io", False),
])
def test_the_unit_decrypts_the_page_host_and_nothing_else_of_instagram(host, intercepted):
    """Applies the regex the unit ships, rather than reading it. Decrypting edge-chat.
    would put DM realtime behind a certificate the CA cannot (and must not) vouch for."""
    rx = re.compile(_unit_allow_hosts().replace("$$", "$"))
    assert bool(rx.search(host)) is intercepted


def test_every_addon_site_is_in_the_decrypt_allowlist():
    """The other direction: a SITES entry the unit does not decrypt is a gated site that
    tunnels through unread."""
    rx = re.compile(_unit_allow_hosts().replace("$$", "$"))
    patterns = [(site, m) for site, cfg in addon.SITES.items() for m in cfg["match"]]
    assert len(patterns) > 50, "SITES parsed to almost nothing; the loop below would be vacuous"
    missing = [f"{site}: {m}" for site, m in patterns if not rx.search(m)]
    assert not missing, f"gated in addon.py but not decrypted by the unit: {missing}"


def test_the_ca_permits_exactly_one_instagram_name():
    names = [d for d in gen_allow_hosts.domains() if "instagram" in d]
    assert names == ["www.instagram.com"], names
    assert len(gen_allow_hosts.domains()) == 97     # 96 -> 97 permitted names


def test_instagram_is_in_the_budget_and_the_shared_pool():
    cfg = budget.SITES["instagram"]
    assert cfg["budget_seconds"] == 7 * 60
    assert cfg["group"] == "main" and budget.pool("instagram") == "main"
    assert "instagram" in budget.pool_sites("main")
    assert cfg["home"] == "https://www.instagram.com/"


def test_instagram_is_appended_so_no_existing_chart_colour_moves():
    order = list(budget.SITES)
    assert order[-1] == "instagram" and order.index("news") == 4


# ---------- the path classifier ----------

@pytest.mark.parametrize("path", FREE_PATHS)
def test_free_paths(path):
    assert addon.instagram_path_is_free(path), path


@pytest.mark.parametrize("path", BUDGETED_PATHS)
def test_budgeted_paths(path):
    assert not addon.instagram_path_is_free(path), path


def test_an_empty_handle_frees_no_profile(monkeypatch):
    monkeypatch.setattr(addon, "INSTAGRAM_SELF", "")
    assert addon.instagram_free_segments() == ["direct", "accounts"]
    assert not addon.instagram_path_is_free("/")
    assert not addon.instagram_path_is_free("//")
    assert not addon.instagram_path_is_free("/someoneelse/")
    assert addon.instagram_path_is_free("/direct/")      # the fixed list is unaffected


@pytest.mark.parametrize("bad", ["../direct", "a/b", "my handle", "x" * 31, "a;b", "<script>",
                                 '"];alert(1);//', " / "])
def test_a_malformed_handle_frees_nothing_and_cannot_reach_the_script(monkeypatch, bad):
    """The handle is substituted into JavaScript, so it is validated against Instagram's
    own alphabet rather than trusted to be a word."""
    monkeypatch.setattr(addon, "INSTAGRAM_SELF", bad)
    assert addon.instagram_free_segments() == ["direct", "accounts"], bad


@pytest.mark.parametrize("raw", ["Mine", "@mine", "/mine/", " mine "])
def test_the_handle_is_normalised(monkeypatch, raw):
    monkeypatch.setattr(addon, "INSTAGRAM_SELF", raw)
    assert addon.instagram_path_is_free("/mine/followers/")
    assert not addon.instagram_path_is_free("/minex/")


def test_the_handle_comes_from_the_environment_and_the_unit_sets_it():
    """addon.py is byte-identical across the two repos; only the unit differs."""
    code = ("import sys; sys.path.insert(0, %r); import addon; "
            "print(addon.instagram_path_is_free('/Zed.Zed/followers/'), "
            "addon.instagram_path_is_free('/zed.zedx/'))" % ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "INSTAGRAM_SELF": "zed.zed"}).stdout.split()
    assert out == ["True", "False"]
    unit = open(os.path.join(ROOT, "deploy", "cooldown-proxy.service")).read()
    assert re.search(r"^Environment=INSTAGRAM_SELF=", unit, re.M)


# ---------- request(): the design table ----------

def test_no_session_budgeted_navigation_gets_the_gate(rdb, monkeypatch):
    stub_gate(monkeypatch)
    for path in ("/", "/explore/", "/reels/", "/p/x/", "/someoneelse/", "/?variant=following"):
        resp = run_request(mkflow(path))
        assert resp is not None and b"THE GATE" in resp.content, path


def test_no_session_free_navigation_passes(rdb, monkeypatch):
    stub_gate(monkeypatch)
    for path in ("/direct/", "/direct/inbox/", "/direct/t/123/", f"/{HANDLE}/",
                 f"/{HANDLE}/followers/", "/accounts/login/", "/accounts/activity/"):
        assert run_request(mkflow(path)) is None, path


def test_no_session_subrequests_pass_so_dms_keep_working(rdb, monkeypatch):
    """Today's 503 for a sub-request would break every DM. The feed's own API calls pass
    as well -- that is the design, and it is the injected script that closes the
    window (next section)."""
    stub_gate(monkeypatch)
    for path in ("/api/v1/direct_v2/inbox/", "/graphql/query", "/api/v1/feed/timeline/"):
        f = mkflow(path, ctype="application/json", nav=False,
                   headers={"Referer": "https://www.instagram.com/direct/inbox/"})
        assert run_request(f) is None, path


def test_a_live_session_passes_everything(rdb):
    rdb.set("active_token:instagram", "tok")
    rdb.set("session:tok", "active")
    for path in ("/", "/direct/", "/explore/"):
        assert run_request(mkflow(path)) is None


def test_other_sites_still_get_503_for_subrequests(rdb):
    """The Instagram branch must not have loosened anyone else."""
    f = mkflow("/api/x", host="www.reddit.com", ctype="application/json", nav=False)
    resp = run_request(f)
    assert resp is not None and resp.status_code == 503


def test_a_failing_budget_server_still_gates_a_budgeted_page(rdb):
    resp = run_request(mkflow("/"))          # req.get refuses (autouse fixture)
    assert resp is not None and b"unreachable" in resp.content


def test_redis_down_fails_closed_for_the_feed_but_not_for_dms(rdb, monkeypatch, capsys):
    def boom(site):
        raise redis.exceptions.ConnectionError("down")
    monkeypatch.setattr(addon, "session_mode", boom)
    stub_gate(monkeypatch)
    assert run_request(mkflow("/")) is not None          # feed: gate
    assert run_request(mkflow("/direct/inbox/")) is None  # DMs: unaffected


def test_the_budget_endpoints_are_unchanged():
    """Six of eight. The SPA hop reuses the heartbeat's 403 -> reload, so no new endpoint."""
    assert addon.BUDGET_ENDPOINTS == frozenset(("", "/feed", "/enter", "/heartbeat",
                                                "/reflect", "/worth"))


# ---------- responseheaders(): buffer HTML, stream the rest ----------

def test_instagram_html_is_buffered_and_csp_amended_but_api_traffic_streams():
    doc = mkflow("/", ctype="text/html; charset=utf-8")
    doc.response.headers["content-security-policy"] = "script-src 'self'"
    addon.BudgetAddon().responseheaders(doc)
    assert doc.response.stream is False
    assert "nonce-" in doc.response.headers["content-security-policy"]

    for ctype in ("application/json", "text/javascript", "application/x-ndjson"):
        api = mkflow("/graphql/query", ctype=ctype, nav=False)
        api.response.headers["content-security-policy"] = "script-src 'self'"
        addon.BudgetAddon().responseheaders(api)
        assert api.response.stream is True, ctype
        # CSP is only ever amended on the documents we inject into.
        assert api.response.headers["content-security-policy"] == "script-src 'self'"


def test_other_gated_sites_still_buffer_everything():
    f = mkflow("/x.json", host="www.reddit.com", ctype="application/json")
    addon.BudgetAddon().responseheaders(f)
    assert f.response.stream is False


# ---------- response(): inject on every HTML page, session or not ----------

def test_a_free_page_with_no_session_is_still_guarded(rdb):
    """Otherwise a page first loaded on /direct/ carries no script and the SPA hop to Home
    is free."""
    f = mkflow("/direct/inbox/")
    addon.BudgetAddon().response(f)
    text = f.response.text
    assert "/budget/heartbeat" in text and 'var SITE = "instagram"' in text
    assert 'var FREE = ["direct", "accounts", "mine"];' in text
    assert "__FREE__" not in text and "__SITE__" not in text
    assert "serviceWorker" in text                     # SW_KILL: no cached pages past us


def test_a_budgeted_page_with_a_session_gets_the_same_script(rdb):
    rdb.set("active_token:instagram", "tok")
    rdb.set("session:tok", "active")
    f = mkflow("/")
    addon.BudgetAddon().response(f)
    assert "/budget/heartbeat" in f.response.text


def test_the_gate_itself_is_never_injected(rdb, monkeypatch):
    """mitmproxy fires response() for a response synthesized in request(). With Instagram
    injected regardless of session, that would put a heartbeat on the gate -- a page whose
    first ping returns 403 and reloads the page it is on, forever."""
    stub_gate(monkeypatch, b"<html><body>THE GATE</body></html>")
    f = mkflow("/")
    assert run_request(f) is not None
    f.response.headers["content-type"] = "text/html; charset=utf-8"
    addon.BudgetAddon().response(f)
    assert b"heartbeat" not in f.response.content
    assert b"THE GATE" in f.response.content


def test_the_unreachable_fallback_page_is_not_injected_either(rdb):
    f = mkflow("/")
    run_request(f)                                    # req.get refuses -> fallback page
    f.response.headers["content-type"] = "text/html; charset=utf-8"
    f.response.text = "<html><body>Budget server unreachable</body></html>"
    addon.BudgetAddon().response(f)
    assert "heartbeat" not in f.response.text


def test_only_instagram_injects_without_a_session(rdb):
    f = mkflow("/r/x", host="www.reddit.com")
    addon.BudgetAddon().response(f)
    assert f.response.text == HTML


def test_other_sites_get_a_script_with_no_free_paths(rdb):
    """null, not an empty list: every path on Reddit is budgeted, and the script's
    isFree() must be a constant false there."""
    rdb.set("active_token:reddit", "tok")
    rdb.set("session:tok", "active")
    f = mkflow("/r/x", host="www.reddit.com")
    addon.BudgetAddon().response(f)
    assert "var FREE = null;" in f.response.text


# ---------- the injected script, run for real ----------

NODE = shutil.which("node")

HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls = [], timers = [], winHandlers = {};
let reloads = 0;
const el = () => ({ style: {}, classList: { add() {}, remove() {} }, setAttribute() {},
                    appendChild() {}, remove() {} });
const location = { pathname: input.start, reload() { reloads++; } };
const document = { visibilityState: 'visible', addEventListener() {}, createElement: el,
                   getElementById: () => null, head: el(), documentElement: el() };
const history = { pushState() {}, replaceState() {} };
const sandbox = {
  location, document, history, Date, Intl, encodeURIComponent, JSON, Math,
  fetch(url) {
    calls.push(url);
    return Promise.resolve({ status: input.status || 200,
                             json: () => Promise.resolve({ remaining: 300 }) });
  },
  setInterval(fn, ms) { timers.push({ fn, ms }); return timers.length; },
  navigator: {},
};
sandbox.window = sandbox;
sandbox.addEventListener = (t, fn) => { (winHandlers[t] = winHandlers[t] || []).push(fn); };
vm.createContext(sandbox);
vm.runInContext(input.script, sandbox);
const out = [calls.length];
for (const step of input.steps) {
  if (step.op === 'push') { location.pathname = step.path; history.pushState({}, '', step.path); }
  if (step.op === 'replace') { location.pathname = step.path; history.replaceState({}, '', step.path); }
  if (step.op === 'set') location.pathname = step.path;
  if (step.op === 'pop') { location.pathname = step.path; (winHandlers.popstate || []).forEach(f => f()); }
  if (step.op === 'tick10') timers.filter(t => t.ms === 10000).forEach(t => t.fn());
  if (step.op === 'tick1') timers.filter(t => t.ms === 1000).forEach(t => t.fn());
  if (step.op === 'hidden') document.visibilityState = 'hidden';
  if (step.op === 'visible') document.visibilityState = 'visible';
  out.push(calls.length);
}
Promise.resolve().then(() => Promise.resolve()).then(() => Promise.resolve()).then(() => {
  console.log(JSON.stringify({ counts: out, calls, reloads }));
});
"""


def _script_for(site="instagram", host="www.instagram.com"):
    """The heartbeat <script> body, exactly as response() would inject it."""
    f = mkflow("/", host=host)
    addon.BudgetAddon().response(f)
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", f.response.text, re.S)
    hb = [s for s in scripts if "/budget/heartbeat" in s]
    assert len(hb) == 1, "expected exactly one heartbeat script"
    return hb[0]


def run_js(start, steps=(), status=200, script=None):
    if NODE is None:
        pytest.skip("needs node")
    payload = json.dumps({"script": script or _script_for(), "start": start,
                          "steps": list(steps), "status": status})
    p = subprocess.run([NODE, "-e", HARNESS], input=payload, capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.fixture()
def injected(rdb):
    """A session exists only on budgeted pages in real use; the script is the same either
    way, which is what the 'no session' variant of response() proves above."""
    return _script_for()


@pytest.mark.parametrize("path", FREE_PATHS + BUDGETED_PATHS)
def test_the_script_and_the_proxy_agree_on_every_path(rdb, path):
    """The reload-loop guard. JS says budgeted but Python serves the page free -> ping,
    403, reload, the same page, ping... JS says free but Python gates -> nothing pings,
    and the feed is free. Either direction is a bug, so both are asserted per path."""
    got = run_js(path.split("?")[0] or "/", script=_script_for())
    pinged = got["counts"][0] > 0
    assert pinged is (not addon.instagram_path_is_free(path)), path


def test_a_free_page_never_pings_not_on_the_interval_either(rdb):
    got = run_js("/direct/inbox/", [{"op": "tick10"}, {"op": "tick10"}, {"op": "tick10"}])
    assert got["counts"] == [0, 0, 0, 0], "a free path pinged -- that is a keep-alive"


def test_a_budgeted_page_pings_on_load_and_on_the_interval(rdb):
    got = run_js("/", [{"op": "tick10"}, {"op": "tick10"}])
    assert got["counts"] == [1, 2, 3]
    assert "site=instagram" in got["calls"][0]


@pytest.mark.parametrize("op", ["push", "replace", "pop"])
def test_hopping_from_dms_to_home_pings_at_once(rdb, op):
    """The row the proxy cannot see. Tapping Home in the DMs is a pushState."""
    got = run_js("/direct/inbox/", [{"op": op, "path": "/"}])
    assert got["counts"] == [0, 1], f"{op}: no ping on entering a budgeted path"


def test_the_hop_with_no_session_reloads_into_the_gate(rdb):
    """The 403 path the plan reuses instead of adding an endpoint."""
    got = run_js("/direct/inbox/", [{"op": "push", "path": "/"}], status=403)
    assert got["reloads"] == 1


def test_the_one_second_backstop_catches_a_navigation_nobody_hooked(rdb):
    """location changes with no pushState/replaceState/popstate (a router that captured
    the original history methods before we wrapped them)."""
    got = run_js("/direct/", [{"op": "set", "path": "/"}, {"op": "tick1"}, {"op": "tick1"}])
    assert got["counts"] == [0, 0, 1, 1], "entered the feed once, so pinged once"


def test_moving_between_free_paths_or_between_budgeted_ones_adds_no_ping(rdb):
    got = run_js("/direct/", [{"op": "push", "path": "/direct/t/1/"},
                              {"op": "push", "path": f"/{HANDLE}/"},
                              {"op": "push", "path": "/accounts/login/"}])
    assert got["counts"] == [0, 0, 0, 0]
    got = run_js("/", [{"op": "push", "path": "/explore/"}, {"op": "push", "path": "/p/x/"}])
    assert got["counts"] == [1, 1, 1], "the 10s interval charges; navigation within the feed need not ping"


def test_leaving_the_feed_for_dms_stops_the_pings(rdb):
    got = run_js("/", [{"op": "push", "path": "/direct/inbox/"}, {"op": "tick10"}])
    assert got["counts"] == [1, 1, 1]


def test_a_hidden_tab_does_not_ping_on_entering_the_feed(rdb):
    got = run_js("/direct/", [{"op": "hidden"}, {"op": "push", "path": "/"}])
    assert got["counts"] == [0, 0, 0]


def test_other_sites_script_still_pings_on_every_path(rdb):
    rdb.set("active_token:reddit", "tok")
    rdb.set("session:tok", "active")
    script = _script_for("reddit", "www.reddit.com")
    # 'direct' is only special on Instagram; /direct/ on Reddit is an ordinary page.
    got = run_js("/direct/", [{"op": "tick10"}], script=script)
    assert got["counts"] == [1, 2]


# ---------- the shadow meter must not see free traffic ----------

def _noted(flow):
    run_request(flow)
    return bool(addon._SHADOW.get("instagram", {}).get("last"))


def test_dm_subrequest_with_a_direct_referer_records_nothing(rdb):
    f = mkflow("/api/v1/direct_v2/inbox/", ctype="application/json", nav=False,
               headers={"Referer": "https://www.instagram.com/direct/inbox/"})
    assert not _noted(f)


def test_free_navigation_records_nothing(rdb):
    assert not _noted(mkflow("/direct/t/9/"))
    assert not _noted(mkflow(f"/{HANDLE}/"))


def test_budgeted_traffic_is_still_recorded(rdb, monkeypatch):
    stub_gate(monkeypatch)
    assert _noted(mkflow("/"))
    addon._SHADOW.clear()
    assert _noted(mkflow("/api/v1/feed/timeline/", ctype="application/json", nav=False,
                         headers={"Referer": "https://www.instagram.com/"}))


def test_a_subrequest_with_no_usable_referer_is_counted(rdb):
    """A missing header must not hide a measurement."""
    api = dict(ctype="application/json", nav=False)
    assert _noted(mkflow("/api/v1/feed/timeline/", **api))
    addon._SHADOW.clear()
    assert _noted(mkflow("/api/v1/feed/timeline/", **api,
                         headers={"Referer": "https://evil.example/direct/"}))   # not ours


def _thirty_minutes_of_api_traffic(monkeypatch, referer):
    """120 sub-requests, 15s apart, through the real request() -- so the skip is tested
    where it lives, not by calling the classifier from the test."""
    clock = [1_700_000_000.0]
    monkeypatch.setattr(addon.time, "time", lambda: clock[0])
    for _ in range(120):
        f = mkflow("/api/v1/direct_v2/inbox/", ctype="application/json", nav=False,
                   headers={"Referer": referer})
        run_request(f)
        clock[0] += 15
    for site, st in addon._SHADOW.items():
        addon._shadow_flush(site, st)
    clock[0] -= 15            # the hour the traffic landed in, for the switch's window


def test_half_an_hour_in_dms_cannot_raise_the_dead_mans_warning(rdb, monkeypatch):
    """The consequence the skip exists for. Thirty minutes of DM traffic with no
    heartbeat to match is, to enforcement_looks_dead(), 'the injected script is not
    running'. The control below is the same traffic from the feed, where that reading is
    right -- it is what shows this test is able to fail."""
    _thirty_minutes_of_api_traffic(monkeypatch, "https://www.instagram.com/direct/t/1/")
    assert rdb.keys("shadow_hour:*") == [], "DM traffic reached the shadow meter"
    assert not addon.enforcement_looks_dead()


def test_control_the_same_traffic_from_the_feed_does_trip_it(rdb, monkeypatch):
    _thirty_minutes_of_api_traffic(monkeypatch, "https://www.instagram.com/")
    assert rdb.keys("shadow_hour:*"), "feed traffic should have been recorded"
    assert addon.enforcement_looks_dead()


def test_the_heartbeat_endpoint_still_never_feeds_the_shadow_meter(rdb):
    f = mkflow("/budget/heartbeat?site=instagram", method="POST", nav=False)
    addon.BudgetAddon().request(f)
    assert "instagram" not in addon._SHADOW


# ---------- the Flask side ----------

def test_the_gate_renders_for_instagram(client, rdb, day):
    html = client.get("/budget?site=instagram").data.decode()
    assert "Enter Instagram" in html
    assert "7:00" in html


def test_enter_returns_to_the_instagram_link_and_rejects_offsite(client, rdb, day):
    from urllib.parse import quote
    deep = "https://www.instagram.com/p/abc123/"
    r1 = client.post("/enter?site=instagram&next=" + quote(deep, safe=""))
    assert r1.headers["Location"] == deep
    for bad in ("https://evil.example/", "https://www.reddit.com/", "https://instagram.com.evil.io/"):
        r2 = client.post("/enter?site=instagram&next=" + quote(bad, safe=""))
        assert r2.headers["Location"] == budget.SITES["instagram"]["home"], bad


def test_instagram_time_is_charged_to_the_shared_pool(client, rdb, day):
    client.post("/enter?site=instagram")
    rdb.set("last_heartbeat:main", __import__("time").time() - 10)
    resp = client.post("/heartbeat?site=instagram")
    assert resp.status_code == 200
    assert 9 < float(rdb.get("spent:main")) < 12      # the SAME bucket Reddit draws from
    assert float(rdb.get(f"usage:{__import__('time').strftime('%Y-%m-%d')}:instagram")) > 9


def test_instagram_cap_is_its_own_slice_of_the_pool(rdb, day):
    rdb.set("spent:main", budget.SITES["instagram"]["budget_seconds"])
    assert budget.get_remaining_budget("instagram") == 0
    assert budget.get_remaining_budget("youtube") > 0     # a bigger slice survives
