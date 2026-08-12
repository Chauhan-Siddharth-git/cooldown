"""Invariants, not incidents.

Every other test file checks that a specific attack is blocked. Those tests pass
forever and catch nothing new — three security reviews in, four of eight findings were
the same bug in a second location, because each fix repaired the instance rather than
the class.

The tests here assert *properties*, and derive what they expect from the code itself
rather than from a list written alongside them. Adding a route, a listener, a host
comparison or an endpoint should therefore FAIL one of these until it has been
classified — which is the point. If you are here because one of them broke, the
question is not "how do I satisfy the test" but "which decision did I skip".
"""
import ast
import itertools
import os
import sys
import time

import pytest
import redis
from mitmproxy.test import tflow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import addon  # noqa: E402
import app as budget  # noqa: E402


def _tree(name):
    with open(os.path.join(ROOT, name)) as f:
        return ast.parse(f.read(), filename=name)


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
    r.set("monitor_origin", "http://100.64.0.1:5000")
    yield r
    r.flushdb()


def flow(path, headers=None, method="GET", host="www.reddit.com"):
    f = tflow.tflow(resp=False)
    f.request.host, f.request.path, f.request.method = host, path, method
    for k, v in (headers or {}).items():
        f.request.headers[k] = v
    return f


def probe(path, headers=None, method="GET"):
    f = flow(path, headers, method)
    addon.BudgetAddon().request(f)
    return f.response


# Every Sec-Fetch-Dest a browser will emit. A control that reasons about "how the
# request was made" has to hold for all of them, not the two you thought of.
ALL_DESTS = ["", "document", "iframe", "empty", "object", "embed", "frame",
             "script", "image", "iframe", "worker", "serviceworker"]


# ---------------------------------------------------------------------------
# 1. The endpoint surface is fully classified
# ---------------------------------------------------------------------------

def test_every_flask_route_is_classified_as_gated_or_box_origin():
    """The decision "which origin serves this?" is the one F9 got wrong for two rounds.
    Make it impossible to add a route without making that decision: every Flask route
    must appear in exactly one of the addon's two lists."""
    routes = {d.args[0].value
              for n in ast.walk(_tree("app.py")) if isinstance(n, ast.FunctionDef)
              for d in n.decorator_list
              if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"}

    gated = {"/budget" if e == "" else e for e in addon.BUDGET_ENDPOINTS}
    moved = set(addon.MOVED_TO_BOX)

    assert not (gated & moved), f"served on both origins: {gated & moved}"
    unclassified = routes - gated - moved
    assert not unclassified, (
        f"new Flask route(s) {sorted(unclassified)} are not classified. Add each to "
        f"addon.BUDGET_ENDPOINTS (served on the gated site's origin — keep this small) "
        f"or addon.MOVED_TO_BOX (served only by the box).")
    stale = (gated | moved) - routes - {"/budget"}
    assert not stale, f"listed but no longer a Flask route: {sorted(stale)}"


# ---------------------------------------------------------------------------
# 2. Nothing the browser sends can redirect the proxy's internal call
# ---------------------------------------------------------------------------

HOSTILE_PATHS = [
    "/budget" + s for s in [
        "", "/", "/enter", "/feed",
        "@evil.example/", "@evil.example:80/x", ":8080@evil.example/",
        "/@evil.example/", "//evil.example/", "///evil.example/",
        "/../health", "/../../etc/passwd", "/..%2fhealth", "/%2e%2e/health",
        "/enter/../devices", "/./devices", "/.%2e/devices",
        "\\@evil.example/", "/\\evil.example", "/%5cevil.example",
        "/enter?next=http://evil.example", "/feed#@evil.example",
        "/health%00", "/health\t", "/health ", "/HEALTH", "/Health",
        "/enter;/../devices", "/enter%3f/../devices",
        "/" + "a" * 400, "/enter?" + "b" * 400,
    ]
] + ["/budgeting", "/budget-advice", "/budgetary/x", "/budgetenter"]


@pytest.mark.parametrize("path", HOSTILE_PATHS)
def test_no_request_path_can_redirect_the_internal_call(rdb, path, monkeypatch):
    """F13: "/budget" + attacker_string was concatenated onto the Flask base URL, and
    "@" turned the base into userinfo. The property — not the payload — is that every
    call the proxy makes still goes to 127.0.0.1:5000."""
    from urllib.parse import urlsplit
    calls = []

    class FakeResp:
        status_code, content, headers = 200, b"ok", {"Content-Type": "text/html"}

    def record(url, *a, **k):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(addon.req, "get", record)
    monkeypatch.setattr(addon.req, "post", record)
    for dest in ("document", "iframe", "empty"):
        probe(path, {"Sec-Fetch-Dest": dest})
        probe(path, {"Sec-Fetch-Dest": dest}, method="POST")
    for url in calls:
        u = urlsplit(url)
        assert (u.hostname, u.port) == ("127.0.0.1", 5000), f"{path!r} escaped to {url!r}"


@pytest.mark.parametrize("path", HOSTILE_PATHS)
def test_only_declared_endpoints_are_ever_forwarded(rdb, path, monkeypatch):
    """Stronger than the above: not just "stays on loopback" but "reaches only a
    path we declared". Closes the door on dot-segment and encoding tricks resolving
    to an endpoint that was never meant to be reachable through the proxy."""
    from urllib.parse import urlsplit
    seen = []

    class FakeResp:
        status_code, content, headers = 200, b"ok", {"Content-Type": "text/html"}

    monkeypatch.setattr(addon.req, "get", lambda url, *a, **k: (seen.append(url), FakeResp())[1])
    monkeypatch.setattr(addon.req, "post", lambda url, *a, **k: (seen.append(url), FakeResp())[1])
    probe(path, {"Sec-Fetch-Dest": "document"})
    allowed = {"/budget"} | {e for e in addon.BUDGET_ENDPOINTS if e}
    for url in seen:
        got = urlsplit(url).path
        assert got in allowed, f"{path!r} forwarded to undeclared endpoint {got!r}"


# ---------------------------------------------------------------------------
# 3. The origin split holds for every request shape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sub,dest", itertools.product(addon.MOVED_TO_BOX, ALL_DESTS))
def test_box_origin_pages_never_return_content_on_a_gated_origin(rdb, sub, dest):
    """F9 took three attempts because each fix reasoned about *some* request shapes.
    This asserts over all of them, including the window.open shape (Dest: document)
    that no header could distinguish from a user-typed URL."""
    resp = probe("/budget" + sub, {"Sec-Fetch-Dest": dest} if dest else {})
    assert resp is not None and resp.status_code in (302, 404), (
        f"{sub} via Dest:{dest or '(absent)'} returned {resp and resp.status_code}")


def test_the_gated_origin_surface_stays_small(rdb):
    """A budget, not a rule. Every endpoint here is one a hostile script on a gated
    site can reach; if this number is climbing, ask what moved in and why."""
    assert len(addon.BUDGET_ENDPOINTS) <= 8, sorted(addon.BUDGET_ENDPOINTS)


# ---------------------------------------------------------------------------
# 4. Every mutating endpoint is CSRF-checked — derived, not listed
# ---------------------------------------------------------------------------

# Derived from BUDGET_ENDPOINTS minus the two that only read. Note the subtlety this
# survived a mutation test to find: parametrising over STATE_CHANGING itself would be
# circular — removing an endpoint from that list would also remove it from the test.
# Deriving from the *reachable* set is what makes "reachable but unchecked" — the actual
# shape of F14 — impossible to introduce quietly.
READ_ONLY_ON_GATED_ORIGIN = {"", "/feed"}


@pytest.mark.parametrize(
    "sub", sorted(set(addon.BUDGET_ENDPOINTS) - READ_ONLY_ON_GATED_ORIGIN))
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_every_mutating_endpoint_rejects_cross_site(rdb, sub, method):
    """F14: the CSRF check keyed on POST, and /exit accepted GET. The list of endpoints
    is derived from BUDGET_ENDPOINTS, so adding one without also adding it to
    STATE_CHANGING fails here rather than in the next review."""
    resp = probe("/budget" + sub, {"Sec-Fetch-Site": "cross-site"}, method=method)
    assert resp is not None and resp.status_code == 403, (
        f"cross-site {method} {sub} was not rejected — add it to addon.STATE_CHANGING")


# ---------------------------------------------------------------------------
# 4b. ...and the same question asked at the OTHER door
#
# Every control above reaches the app through addon.request(). Flask also answers these
# routes directly on the box's own origin (loopback + tailscale0), which the addon never
# sees. F3's fix was written at the proxy boundary and rested on a parenthetical — "every
# endpoint is same-origin" — which was true when written. F9's third fix then moved five
# endpoints to an origin where that sentence is false and no check runs.
#
# So the property is not "the proxy rejects cross-site writes", it is "a cross-site write
# is rejected", and it has to hold at every entrance the route has. A control that exists
# at one of two doors is not a control.
# ---------------------------------------------------------------------------

def box(path, headers=None, method="GET", data=None):
    """One request straight at Flask, the way the box's own listener answers it.

    The deliberate sibling of probe(). Anything asserted about a request shape should be
    asserted through both, or it is only true of the door somebody remembered."""
    kwargs = {"headers": headers or {}, "method": method}
    if method != "GET":
        kwargs["data"] = data or {}
    return budget.app.test_client().open(path, **kwargs)


# The shapes a cross-site request actually arrives in. Three, not one, because a check
# reading either signal ALONE passes a test that always sends both — and the two do not
# cover each other: a cross-site <img src> GET carries no Origin at all (which is how
# F14's /exit slipped through), while a browser with no Sec-Fetch support carries only
# Origin. Parametrising over the shapes is what makes this assert the property rather
# than one instance of it.
CROSS_SITE_SHAPES = {
    "form-post": {"Origin": "https://evil.example", "Referer": "https://evil.example/",
                  "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"},
    "img-get-no-origin": {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image",
                          "Referer": "https://evil.example/"},
    "no-sec-fetch-headers": {"Origin": "https://evil.example",
                             "Referer": "https://evil.example/"},
}
# The control. The test client's host is "localhost", so this is the page's own origin.
SAME_ORIGIN = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}


def _seed_live_state(rdb):
    """Give every mutating endpoint something real to act on.

    Written after this test passed for the wrong reason: /heartbeat answers 403 when
    there is no session, so with an empty Redis it looked protected while being wide
    open. A 403 has to mean "refused" and never "nothing to do", which means the state
    each endpoint needs must exist before the request is made.
    """
    rdb.set("active_token:reddit", "tok-invariant")
    rdb.setex("session:tok-invariant", 120, "active")
    rdb.set("last_heartbeat:main", time.time() - 10)
    rdb.setex("pending_worth", 7200, f"{time.time():.0f} reddit tired")
    rdb.set("unacked_boot", f"{time.time():.0f}")


def _mutating_rules():
    """(path, method) for every Flask route declaring a state-changing method — read from
    the app's own url_map, so a new POST route joins this test by existing rather than by
    being remembered.

    Every declared method is exercised, not only the unsafe-looking ones. F14 was /exit
    accepting GET, and a check that tries POST alone misses the next one the same way.
    """
    out = []
    for rule in budget.app.url_map.iter_rules():
        methods = (rule.methods or set()) - {"HEAD", "OPTIONS"}
        if methods <= {"GET"}:
            continue                                    # read-only route
        out += [(str(rule.rule), m) for m in sorted(methods)]
    return sorted(out)


@pytest.mark.parametrize("shape", sorted(CROSS_SITE_SHAPES))
@pytest.mark.parametrize("path,method", _mutating_rules())
def test_every_mutating_endpoint_rejects_cross_site_on_the_box_origin(
        rdb, path, method, shape):
    """The box origin is reachable from any page that knows the address — and the gate
    hands that address to every script on a gated site, in the dashboard links.

    A 302/200 here means an unrelated site can drive this endpoint: burn the budget into
    a pool-wide cooldown, end sessions, or clear `unacked_boot` — the reboot alarm that
    is the compensating control for CA theft in "Accepted by design"."""
    body = {"v": "yes", "trigger": "tired"}

    # Control first: prove this endpoint DOES something when the origin is its own, so
    # the 403 asserted below can only be caused by the cross-site headers.
    _seed_live_state(rdb)
    ok = box(path + "?site=reddit", SAME_ORIGIN, method=method, data=body)
    assert ok.status_code != 403, (
        f"precondition failed: {method} {path} answers 403 even same-origin, so a 403 "
        f"in the real check would prove nothing. Fix _seed_live_state, not this line.")

    _seed_live_state(rdb)
    resp = box(path + "?site=reddit", CROSS_SITE_SHAPES[shape], method=method, data=body)
    assert resp.status_code == 403, (
        f"cross-site {method} {path} as {shape} returned {resp.status_code}, not 403 — "
        f"a check that reads only the signal the OTHER shapes carry is not a check")


def test_the_box_origin_check_is_exercising_something(rdb):
    """The derivation above is a filter over url_map; if it ever returns nothing the
    parametrised test silently becomes zero tests and reports green. Absence of a failure
    would then be indistinguishable from a pass.

    The three named here are the ones that would be missed by an obvious weaker version:
    /boot-ack because F14 claims it is already covered, /exit as a GET because that WAS
    F14, and /heartbeat because it answers 403 unprompted and fooled the first draft."""
    rules = _mutating_rules()
    assert rules, "no mutating routes derived from url_map — the filter is broken"
    for want in (("/boot-ack", "POST"), ("/exit", "GET"), ("/heartbeat", "POST")):
        assert want in rules, f"{want} missing from the derived set"


# ---------------------------------------------------------------------------
# 5. Responses the proxy synthesises are always hardened
# ---------------------------------------------------------------------------

def test_forwarded_responses_always_carry_the_hardening_headers(rdb, monkeypatch):
    class FakeResp:
        status_code, content = 200, b'{"ok":1}'
        headers = {"Content-Type": "application/json"}

    monkeypatch.setattr(addon.req, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(addon.req, "post", lambda *a, **k: FakeResp())
    rdb.set("feed_token", "T")
    checked = 0
    for sub in addon.BUDGET_ENDPOINTS:
        q = "?t=T" if sub == "/feed" else ""
        resp = probe("/budget" + sub + q,
                     {"Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-origin"})
        if resp is None or resp.status_code != 200:
            continue
        for h, v in (("X-Content-Type-Options", "nosniff"),
                     ("X-Frame-Options", "DENY"),
                     ("Cache-Control", "no-store")):
            assert resp.headers.get(h) == v, f"{sub} missing {h}"
        assert resp.headers["Content-Type"].startswith("application/json"), sub
        checked += 1
    assert checked, "no endpoint exercised — the loop is not testing anything"


# ---------------------------------------------------------------------------
# 6. Source-level invariants: the shapes that keep coming back
# ---------------------------------------------------------------------------

def _funcs(tree):
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_host_comparison_happens_in_exactly_one_place():
    """F4 fixed substring host matching in site_for_host. F18 was the same bug six
    lines below, in the exemption list, and survived two more reviews. One chokepoint
    means there is no second place to get it wrong."""
    tree = _tree("addon.py")
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name == "host_matches":
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare) and any(isinstance(o, ast.In) for o in n.ops) \
               and any(isinstance(c, ast.Name) and c.id == "host" for c in n.comparators):
                offenders.append(f"{fn.name}:{n.lineno} substring test against `host`")
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
               and n.func.attr == "endswith" \
               and isinstance(n.func.value, ast.Name) and n.func.value.id == "host":
                offenders.append(f"{fn.name}:{n.lineno} ad-hoc host.endswith")
    assert not offenders, "host matching outside host_matches(): " + "; ".join(offenders)


def test_no_listener_is_hardcoded_to_all_interfaces():
    """F1. The only way to widen the bind is the COOLDOWN_LISTEN env var, which is a
    deliberate act with a comment attached — not a literal someone typed while
    debugging."""
    for name in ("app.py", "addon.py"):
        tree = _tree(name)
        # Docstrings are prose, not configuration. Excluding them keeps the check strict
        # about VALUES while letting the code explain why the value is forbidden — which
        # is exactly what the port-listing helper's docstring does.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) \
                   and isinstance(body[0].value, ast.Constant) \
                   and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        lits = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "0.0.0.0" in n.value and id(n) not in docstrings]
        assert not lits, f"{name} hardcodes an all-interfaces bind: {lits}"


def test_templates_are_never_built_from_runtime_data():
    """Rendering anything but a module constant is SSTI. Nothing does this today; this is
    here so nothing starts. Both spellings are checked: the project renders through
    render_page() now, and a guard that names only the function it used to call would
    quietly stop guarding anything."""
    bad = []
    for n in ast.walk(_tree("app.py")):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") in (
                "render_template_string", "render_page"):
            first = n.args[0] if n.args else None
            if not (isinstance(first, ast.Name) and first.id.isupper()):
                bad.append(f"{getattr(n.func, 'id', '?')}:{n.lineno}")
    assert not bad, f"template rendered from a non-constant at {bad}"


def test_the_template_cache_cannot_grow_without_bound():
    """Keyed by the source string. Fine while every key is a module constant — a caller
    passing a per-request string would turn the cache into a memory leak, which is
    exactly the class of bug this change was made to remove."""
    budget._TEMPLATE_CACHE.clear()
    with budget.app.app_context():
        for _ in range(25):
            budget.render_page(budget.DIGEST_PAGE, d={
                "range": "", "total_min": 0, "per_day_min": 0, "delta_pct": None,
                "delta_abs": 0, "sites": [], "cooldowns": 0, "rapid": 0, "rapid_hours": 3,
                "busiest_day": "", "busiest_min": 0,
                "why": {"total": 0, "rows": [], "passes": 0, "rate": 0},
                "worth": {"total": 0, "rate": 0}, "top_trigger": "", "worst_trigger": "",
                "worst_pct": 0})
    assert len(budget._TEMPLATE_CACHE) == 1, "one entry per distinct template, not per call"


def test_the_gate_never_carries_the_master_token():
    """The gate is readable by any script on the gated site, so whatever it holds is
    public to that site. Asserted against the template source, so it holds regardless
    of which values Redis happens to contain."""
    src = open(os.path.join(ROOT, "app.py")).read()
    gate_line = [l for l in src.splitlines()
                 if l.startswith("BUDGET_PAGE = _add_bg(")][0]
    assert "feed_tok" in gate_line and "ui_tok" not in gate_line, gate_line


def test_every_site_has_its_own_validated_chart_colour():
    """Colour is identity in a categorical chart, so two sites sharing a hue asserts a
    match that isn't there. Six colours is what this surface validates for — a seventh
    hue fails the normal-vision separation floor against the teal. If this fails because
    you added a site, the fix is to validate a new colour, not to append a guess."""
    assert len(budget.SITES) <= len(budget.SERIES_COLORS), (
        f"{len(budget.SITES)} sites but only {len(budget.SERIES_COLORS)} validated colours")
    assert len(set(budget.SERIES_COLORS)) == len(budget.SERIES_COLORS), "duplicate hue"


def test_overflow_never_recycles_another_sites_hue():
    n = len(budget.SERIES_COLORS)
    assert budget.series_color(n) == budget.SERIES_OVERFLOW
    assert budget.series_color(n + 5) == budget.SERIES_OVERFLOW
    assert budget.SERIES_OVERFLOW not in budget.SERIES_COLORS
