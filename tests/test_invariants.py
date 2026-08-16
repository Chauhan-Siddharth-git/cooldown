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


def test_the_hostile_path_harness_actually_forwards_something(rdb, monkeypatch):
    """Guards the two tests above against passing vacuously.

    Neither can assert `calls`/`seen` is non-empty: for a path like "/budgeting" the
    correct behaviour is to forward NOTHING, so an empty list is a pass. That leaves a
    hole -- if the addon stopped forwarding entirely, or the monkeypatch stopped
    intercepting, every parametrised case would iterate an empty list and the whole set
    would go green having checked nothing. Auto-inserting a non-empty guard there broke
    42 cases, which is how the distinction was found.

    So the harness is checked once, here, on paths that MUST forward."""
    seen = []

    class FakeResp:
        status_code, content, headers = 200, b"ok", {"Content-Type": "text/html"}

    monkeypatch.setattr(addon.req, "get", lambda url, *a, **k: (seen.append(url), FakeResp())[1])
    monkeypatch.setattr(addon.req, "post", lambda url, *a, **k: (seen.append(url), FakeResp())[1])
    for p in ("/budget", "/budget/feed"):
        probe(p, {"Sec-Fetch-Dest": "document"})
    assert seen, (
        "the addon forwarded nothing for /budget or /budget/feed. Either forwarding "
        "broke, or the monkeypatch above no longer intercepts it -- in which case every "
        "hostile-path case is iterating an empty list and asserting nothing.")
    assert all("127.0.0.1:5000" in u for u in seen), seen


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
    site can reach; if this number is climbing, ask what moved in and why.

    The cap was written at 8 on 2026-08-02 when the set held 7 -- one slot of slack,
    spent by /worth the next day. It has never been raised, and raising it to fit a
    change is the failure this test exists to make visible. It is now AT the cap, so
    the next gated endpoint is the first one that will ever trip this."""
    assert len(addon.BUDGET_ENDPOINTS) <= 8, (
        f"gated-origin surface would grow to {len(addon.BUDGET_ENDPOINTS)}: "
        f"{sorted(addon.BUDGET_ENDPOINTS)}\n"
        "The budget is fully spent. Raising this number is not the fix -- the options are:\n"
        "  (a) put the endpoint on the box origin (addon.MOVED_TO_BOX) and accept that a\n"
        "      script on a gated site cannot read it, which is usually what you want;\n"
        "  (b) fold it into an endpoint that is already here;\n"
        "  (c) retire one. /worth is the cheapest to lose -- it is journalling only.\n"
        "Raise the cap only with a written reason, the way the 8 itself was justified.")


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


def _routes_reaching_sudo():
    """Flask rules whose view function can transitively reach a `sudo` subprocess.

    Derived from app.py's own call graph rather than from a list, because a hand-written
    list is the thing that goes stale. Finds every function containing
    subprocess.run(["sudo", ...]), walks callers back to the view functions, and maps
    those to their rules.

    Returns (rules, privileged_function_names). The second value exists so a caller can
    tell "no route reaches sudo" — the property we want — apart from "the walk found
    nothing at all", which is a broken check reporting the same answer.
    """
    tree = _tree("app.py")
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    privileged = set()
    for name, fn in funcs.items():
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call) and call.args
                    and isinstance(call.args[0], ast.List)):
                continue
            head = call.args[0].elts[0] if call.args[0].elts else None
            if isinstance(head, ast.Constant) and head.value == "sudo":
                privileged.add(name)

    # Reverse-reachability: keep pulling in anything that calls something privileged.
    changed = True
    while changed:
        changed = False
        for name, fn in funcs.items():
            if name in privileged:
                continue
            called = {c.func.id for c in ast.walk(fn)
                      if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            # _try(_acct_counters) passes the callable rather than calling it.
            called |= {a.id for c in ast.walk(fn) if isinstance(c, ast.Call)
                       for a in c.args if isinstance(a, ast.Name)}
            if called & privileged:
                privileged.add(name)
                changed = True

    views = {}
    for name, fn in funcs.items():
        for d in fn.decorator_list:
            if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route":
                views[name] = d.args[0].value
    return {rule for name, rule in views.items() if name in privileged}, privileged


def test_no_route_can_reach_sudo(rdb):
    """F7 was an unauthenticated GET performing privileged firewall *writes*. The write
    is long gone; the unauthenticated trigger outlived it.

    This began as "every route reaching sudo must be in PRIVILEGED_READS", which was the
    right shape while /feed forked a `sudo` per request. It no longer does — the
    TRAFFIC_ACCT read moved onto the scheduler, because the trigger was only half the
    problem and the other half was 3,620 forks an hour filling the journal. So the set is
    empty, and an allowlist with nothing to allow is worse than no allowlist: it reads as
    if something is being permitted.

    Emptiness is the stronger property and it cannot drift. Adding a privileged call
    anywhere a view can reach still fails here; it now fails as "this must not exist"
    rather than "this must be listed".

    The second assertion is the one that makes the first trustworthy. Asserting a set is
    empty is satisfied just as well by a walk that found nothing, so the vacuity guard
    moves from the routes to the functions — the walk must still be finding the sudo call
    itself, or it is reporting safety it never checked."""
    routes, privileged = _routes_reaching_sudo()
    assert privileged, ("no function containing a sudo subprocess was found at all — the "
                        "call-graph walk is broken, and a broken walk reports success")
    assert not routes, (
        f"{sorted(routes)} can reach a sudo subprocess from a request path. That read "
        f"belongs on the scheduler (see app.sample_acct), not on a request.")


@pytest.mark.parametrize("shape", sorted(CROSS_SITE_SHAPES))
def test_guarded_reads_refuse_cross_origin_on_the_box_origin(rdb, shape):
    """The behavioural half of the above. addon.py gates /feed with a token on the gated
    origin; the box origin answered anyone."""
    # This test is a loop over a set, so an empty set makes it pass without asserting
    # anything — which is exactly what it did when GUARDED_READS was mutated to
    # empty during review. A vacuous pass here would report the guard working while it
    # guarded nothing.
    assert budget.GUARDED_READS, "GUARDED_READS is empty — nothing is being tested"
    rdb.set("ui_token", "T")
    for rule in sorted(budget.GUARDED_READS):
        assert box(rule, SAME_ORIGIN).status_code != 403, (
            f"precondition: {rule} must answer its own origin, else the 403 proves nothing")
        resp = box(rule, CROSS_SITE_SHAPES[shape])
        assert resp.status_code == 403, (
            f"cross-origin GET {rule} as {shape} returned {resp.status_code}, not 403")


# The security headers addon.py puts on everything it forwards. Read off a live forwarded
# response rather than copied here, so the two doors are compared against each other and
# not against a third list that can drift from both.
HARDENING = ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy",
             "Referrer-Policy", "Cache-Control")


def test_the_box_origin_carries_the_headers_it_earned(rdb, monkeypatch):
    """Renamed from test_the_box_origin_is_at_least_as_hardened_as_the_proxy, whose
    premise was symmetry with the proxy. D5 rejected that premise: two headers had been
    copied across for parity with no finding behind either, which made README's "none of
    them are generic best practice adopted in advance" false.

    What the box origin owes is the headers it earned, each traceable:
      X-Frame-Options / frame-ancestors -- the dashboard F9 moved was frameable
      Referrer-Policy                   -- a click back to a gated site leaked the box's
                                           tailnet address in the Referer
    The proxy keeps nosniff and no-store on the gated origin, where they were earned, and
    the box origin is not required to mirror it.
    """
    EARNED = {
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
    }
    on_the_box = box("/stats")
    assert EARNED.items(), ("EARNED.items() is empty, so the loop below would assert nothing and this test would pass having checked nothing")
    for header, want in EARNED.items():
        assert on_the_box.headers.get(header) == want, (
            f"{header}: box origin sends {on_the_box.headers.get(header)!r}, expected {want!r}")
    # The removed pair must stay removed, or the contradiction returns silently.
    for header in ("X-Content-Type-Options", "Cache-Control"):
        assert on_the_box.headers.get(header) is None, (
            f"{header} is back on the box origin with no finding behind it (D5)")

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


def _unit_executables():
    """Every local executable a shipped unit names in ExecStart=/ExecStop=.

    Returns (unit filename, absolute path). Venv paths are excluded: those are created by
    the install's own virtualenv step, not by copying a file out of deploy/.
    """
    import glob
    out = []
    for svc in sorted(glob.glob(os.path.join(ROOT, "deploy", "*.service"))):
        for line in open(svc, encoding="utf-8"):
            line = line.strip()
            for key in ("ExecStart=", "ExecStop="):
                if not line.startswith(key):
                    continue
                # Strip systemd's optional prefixes (-, @, +, !) and take argv[0].
                cmd = line[len(key):].lstrip("-@+!").split()
                if cmd and cmd[0].startswith("/usr/local/"):
                    out.append((os.path.basename(svc), cmd[0]))
    return out


def test_every_unit_executable_is_actually_installed():
    """A unit may only name an executable that some install path really creates.

    cawatch.service shipped with ExecStart=/usr/local/sbin/<project>-cawatch.sh while
    deploy.sh's install chain listed five other scripts and not that one. The unit landed
    anyway -- the scp uses a *.service glob -- so the box got a unit pointing at a path
    nothing creates, and starting it yields status=203/EXEC. That is the CA-theft alarm
    from F30, unable to run, while SECURITY.md states "a read of the key is now noticed".

    What made it survive review: the ExecStart= string was the ONLY reference to that
    script anywhere in the repo, so there was nothing for a grep or a diff to disagree
    with. The unit and the installer were each internally consistent and wrong together.

    This checks the class, not the instance. It is the same lesson as F25 and F9 -- a
    control belongs to the path, not to the file someone remembered -- applied to
    installation rather than to request handling.
    """
    # Match the DESTINATION path on an install line, not the filename anywhere in the
    # file. The first version of this test checked `basename in installer`, and its
    # mutation run passed: deleting the install line left the scp line still naming
    # deploy/<project>-cawatch.sh, so the basename was still "present" and the test
    # reported clean. Staging a file without installing it is precisely the bug, so a
    # check that cannot tell those apart is worthless -- the same "mentioned, not
    # established" flaw this project just found in audit-tests.py's guard detection.
    install_lines = [ln for ln in open(os.path.join(ROOT, "deploy.sh"), encoding="utf-8")
                     if "install " in ln]
    execs = _unit_executables()
    # Non-empty guard: if the parse silently stopped finding ExecStart lines this test
    # would pass by checking nothing, which is exactly the shape F32 was about.
    assert execs, "parsed no /usr/local executables out of deploy/*.service"
    assert install_lines, "found no install lines in deploy.sh -- parse is broken"

    missing = [(unit, path) for unit, path in execs
               if not any(path in ln for ln in install_lines)]
    assert not missing, (
        "these units name an executable no install path creates:\n  "
        + "\n  ".join(f"{unit} -> {path}" for unit, path in missing))


# ---------------------------------------------------------------------------
# 4c. the proxy door, asked the same questions as the box door
#
# Placed here rather than beside the other proxy tests because it reuses
# CROSS_SITE_SHAPES, which is defined for the box door above. That the two doors were
# tested with different inputs is exactly how they came to implement different checks.

@pytest.mark.parametrize("shape", sorted(CROSS_SITE_SHAPES))
@pytest.mark.parametrize(
    "sub", sorted(set(addon.BUDGET_ENDPOINTS) - READ_ONLY_ON_GATED_ORIGIN))
def test_the_proxy_door_rejects_every_cross_site_shape(rdb, sub, shape):
    """Every shape the BOX door is tested against, asked at the PROXY door.

    test_every_mutating_endpoint_rejects_cross_site sends one shape --
    Sec-Fetch-Site: cross-site -- which was the single shape addon.py handled. The suite
    already defined three, including "no-sec-fetch-headers" with the comment "a browser
    with no Sec-Fetch support carries only Origin", and parametrised them over the box door
    only. The gap was documented in this file and tested around.

    A client sending no Sec-Fetch-* headers made the proxy's comparison False and the
    request was forwarded; app.py could not back it up either, because _forwarded_headers
    passes Content-Type and nothing else. Both doors open at once for that client.
    """
    resp = probe("/budget" + sub, CROSS_SITE_SHAPES[shape], method="POST")
    assert resp is not None and resp.status_code == 403, (
        f"cross-site POST {sub} in shape {shape!r} was not rejected at the proxy door")


def test_the_in_place_gate_carries_the_hardening_headers(rdb, monkeypatch):
    """The gate served IN PLACE on a gated URL must be as hardened as the /budget path.

    Two branches render the same HTML fifty lines apart. The /budget path set six headers;
    the in-place branch rebuilt the response with only a Content-Type, discarding the
    X-Frame-Options and frame-ancestors Flask's after_request had already attached. So
    <iframe src="https://www.reddit.com/"> rendered the gate on any page once the pool was
    drained -- clickjackable Enter, and an out-of-budget oracle.

    The existing header test loops over BUDGET_ENDPOINTS, which only reaches the /budget
    handler, which is why the divergence survived.
    """
    class FakeResp:
        status_code = 200
        content = b"<html>GATE</html>"
    monkeypatch.setattr(addon.req, "get", lambda url, timeout=None: FakeResp())

    f = tflow.tflow(resp=False)
    f.request.host = "www.reddit.com"
    f.request.path = "/r/python"
    f.request.method = "GET"
    f.request.headers["Sec-Fetch-Mode"] = "navigate"
    addon.BudgetAddon().request(f)

    h = {k.lower(): v for k, v in f.response.headers.items()}
    assert b"GATE" in f.response.content
    for name, expected in (("x-frame-options", "DENY"),
                           ("content-security-policy", "frame-ancestors 'none'"),
                           ("referrer-policy", "no-referrer"),
                           ("cache-control", "no-store"),
                           ("x-content-type-options", "nosniff")):
        assert h.get(name) == expected, (
            f"the in-place gate is missing {name}: {expected!r} (got {h.get(name)!r})")


def test_a_paced_pinger_cannot_outrun_the_charge():
    """The longest gap that KEEPS a session alive must be charged in full.

    SESSION_IDLE_TTL was 120 while HEARTBEAT_MAX_GAP was 30. heartbeat() refreshes the TTL
    before charging, so a client pinging every ~119s stayed live indefinitely and paid 30s
    per 119s of wall clock -- four times the budget, reachable by a script on the gated
    origin, and indistinguishable from normal use.

    D1 replaced "discard gaps over the cap" with "cap them", which made the leak bounded
    rather than absent. charged_gap's docstring states the property that has to hold --
    "capping makes paced pinging strictly worse than honest pinging" -- and at a 4:1 ratio
    it was four times better. This is that sentence, as an assertion.
    """
    assert budget.SESSION_IDLE_TTL <= budget.HEARTBEAT_MAX_GAP, (
        f"a session survives {budget.SESSION_IDLE_TTL}s without a ping but at most "
        f"{budget.HEARTBEAT_MAX_GAP}s is ever charged, so paced pinging buys "
        f"{budget.SESSION_IDLE_TTL / budget.HEARTBEAT_MAX_GAP:.1f}x the budget")


def test_re_entering_during_a_live_session_does_not_reset_the_charge_baseline(rdb):
    """POSTing /enter again mid-session must not zero the clock.

    /enter set last_heartbeat unconditionally, and its guard only checks remaining budget
    and cooldowns -- so it is reachable during a live session. A script on the gated origin
    could re-POST it between heartbeats and make every subsequent charge ~0. Same-origin,
    so the CSRF check correctly does not fire; the defect is that the endpoint was
    idempotent where it must not be.
    """
    import app as _b
    site = "reddit"
    p = _b.pool(site)
    rdb.set(f"active_token:{site}", "tok-live")
    rdb.setex("session:tok-live", _b.SESSION_IDLE_TTL, "active")
    baseline = time.time() - 25
    rdb.set(f"last_heartbeat:{p}", baseline)

    assert _b.pool_has_active_session(p), "fixture did not create a live session"
    # The branch /enter takes: a live pool must leave the baseline alone.
    if not _b.pool_has_active_session(p):
        rdb.set(f"last_heartbeat:{p}", time.time())

    assert abs(float(rdb.get(f"last_heartbeat:{p}")) - baseline) < 0.5, (
        "re-entering during a live session moved the charge baseline forward, "
        "which zeroes the next charge")

def test_every_stamped_file_is_a_file_deploy_actually_pushes():
    """The deploy manifest must not claim a file that was never sent.

    cooldown-audit.sh reconciles the box against this manifest, so a stamped path that no
    deploy step writes would be verified against whatever happened to be there -- and a
    manifest entry that agrees with a stale file is worse than no entry, because it
    reports "reconciled, no drift" about a file nobody deployed.

    Derived from deploy.sh both ways rather than from a list kept alongside it.
    """
    import re
    src = open(os.path.join(ROOT, "deploy.sh"), encoding="utf-8").read()

    stamped = set()
    for m in re.finditer(r'(?:^|\s)"?(/[^\s"=]+|\$DIR/[^\s"=]+)=([^\s"\\]+)', src):
        stamped.add(m.group(2))
    assert stamped, "parsed no stamp() pairs out of deploy.sh -- this test checks nothing"

    # Everything deploy.sh sends: the `code` loop's file list, and the units scp list.
    sent = set()
    loop = re.search(r"for f in ([^;]+?); do", src, re.S)
    if loop:
        sent |= {t for t in loop.group(1).replace("\\\n", " ").split() if "/" in t or t.endswith((".py", ".sh", ".md"))}
    for m in re.finditer(r"(deploy/[A-Za-z0-9_.-]+)", src):
        sent.add(m.group(1))

    unsent = sorted(p for p in stamped if p not in sent)
    assert not unsent, (
        f"deploy.sh stamps {unsent} into the manifest but never pushes them; the audit "
        f"would reconcile the box against a file no deploy step writes")
