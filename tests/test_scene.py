"""The lockout window: a pixel-art picture on the gate, once you are locked out.

Two pictures take turns, switching with every lockout: a Red Line car (scene/cooldown-scene.js)
and a Beacon Hill lane (scene/beacon-hill.js). The current one is a script inlined into the gate. It runs
on the GATED SITE's origin -- same-origin with Reddit or YouTube -- so the properties worth
pinning are not about how it looks but about what it can reach: no network, no storage, no
cookies, no way out of its <script> element, and a size that does not bloat every locked-out
page. The art itself is checked by eye (pixel-art/ in the pomodoro project); what is checked
here is that the gate shows it only when it should, survives it going missing, and that in a
real browser it draws and asks for nothing. The rules about the script hold for every file in
scene/, whichever one is up.
"""
import functools
import glob
import http.server
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

import app as budget

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENE = os.path.join(ROOT, "scene", "cooldown-scene.js")
# Every picture the gate could inline, not only the one it inlines today: each runs on the
# gated site's origin the moment it is wired in, so each is held to the same rules now.
SCENES = sorted(glob.glob(os.path.join(ROOT, "scene", "*.js")))
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
each_scene = pytest.mark.parametrize("path", SCENES, ids=os.path.basename)


def _js(path=SCENE):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _pick(monkeypatch, path):
    """Make `path` the current picture."""
    monkeypatch.setattr(budget, "scene_file", lambda now=None: str(path))


def _locked_out(rdb, client, monkeypatch):
    monkeypatch.setattr(budget, "monitor_origin", lambda *a: "http://100.64.0.1:5000")
    rdb.set("cooldown:main", time.time() - 100)
    return client.get("/budget?site=reddit").get_data(as_text=True)


def test_the_gate_shows_the_window_once_you_are_locked_out(rdb, client, day, monkeypatch):
    html = _locked_out(rdb, client, monkeypatch)
    assert 'id="cd-scene-c"' in html
    assert "G.CooldownScene={" in html, "the script itself is inlined"
    assert "window.CooldownScene" in html and "S.mount(" in html, "and the gate mounts it"


def test_the_gate_has_no_window_while_you_can_enter(rdb, client, day, monkeypatch):
    monkeypatch.setattr(budget, "monitor_origin", lambda *a: "http://100.64.0.1:5000")
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert "Enter Reddit" in html
    assert 'id="cd-scene' not in html and "CooldownScene" not in html


def test_a_missing_picture_never_costs_the_gate(rdb, client, day, monkeypatch, tmp_path):
    _pick(monkeypatch, tmp_path / "nope.js")
    html = _locked_out(rdb, client, monkeypatch)
    assert 'id="cd-scene' not in html and "CooldownScene" not in html
    assert '<div class="card ' in html and "<h1" in html, "the rest of the locked-out gate still renders"


def test_a_file_that_could_break_out_of_its_script_is_refused(rdb, client, day, monkeypatch, tmp_path):
    bad = tmp_path / "bad.js"
    bad.write_text("var x=1;</SCRIPT><script>alert(1)</script>")
    _pick(monkeypatch, bad)
    assert budget.scene_js() == ""
    html = _locked_out(rdb, client, monkeypatch)
    assert "alert(1)" not in html


def test_the_scene_list_is_the_pictures_that_exist():
    """The rules below run once per file in scene/. An empty or stale list would let every
    one of them pass by running on nothing."""
    assert SCENE in SCENES
    assert os.path.join(ROOT, "scene", "beacon-hill.js") in SCENES
    assert sorted(budget.SCENE_FILES) == SCENES, "the gate takes turns between exactly these"


POOL = budget.pool_max_budget("main")
SLICE = budget.SITES["reddit"]["budget_seconds"]
# Each way enforcement ends a session, set to end it on the next heartbeat: [phase, site, keys].
LOCKOUTS = {
    "slice spent": ("day", "reddit", {"spent:main": SLICE - 5}),
    "bucket drained": ("day", "youtube", {"spent:main": POOL - 10}),
    "night allowance spent": ("night", "reddit", {"night_spent:main": budget.NIGHT_BUDGET_SECONDS - 10}),
    "wind-down cap reached": ("winddown", "reddit", None),       # the cap is read under the fixture
}


@pytest.mark.parametrize("kind", LOCKOUTS)
def test_every_lockout_turns_the_picture(request, client, rdb, session, kind):
    phase, site, keys = LOCKOUTS[kind]
    request.getfixturevalue(phase)
    for k, v in (keys or {"spent:main": budget.effective_cap(site) - 5}).items():
        rdb.set(k, v)
    before = budget.scene_file()
    session(site, last_gap=15)
    assert client.post(f"/heartbeat?site={site}").status_code == 403, "the setup has to lock you out"
    assert budget.scene_file() != before


def test_a_lockout_turns_it_once_however_often_you_look(client, rdb, day, monkeypatch):
    """The gate starts a cooldown itself when it finds the bucket drained; after that it only
    reads. A GET that turned the picture would be state changed by anything that can fetch it,
    the gated site's own scripts included."""
    monkeypatch.setattr(budget, "monitor_origin", lambda *a: "http://100.64.0.1:5000")
    rdb.set("spent:main", POOL)
    turns = []
    for _ in range(3):
        client.get("/budget?site=reddit")
        turns.append(rdb.get(budget.SCENE_TURN))
    assert rdb.get("cooldown:main") is not None and turns == ["1", "1", "1"], turns
    budget.start_cooldown("main", "reddit")              # already running: not a new lockout
    assert rdb.get(budget.SCENE_TURN) == "1"


@pytest.mark.parametrize("kind", LOCKOUTS)
def test_a_failing_turn_never_changes_what_enforcement_answers(request, client, rdb, session, monkeypatch, kind):
    """The picture is decoration on the enforcement path. If counting the lockout fails, the
    session still ends and the answer is still 403 -- and the failure is recorded, not lost."""
    phase, site, keys = LOCKOUTS[kind]
    request.getfixturevalue(phase)
    noted = []
    monkeypatch.setattr(budget, "_note_error", noted.append)
    monkeypatch.setattr(budget, "lockout_begins", lambda: 1 / 0)
    for k, v in (keys or {"spent:main": budget.effective_cap(site) - 5}).items():
        rdb.set(k, v)
    session(site, last_gap=15)
    assert client.post(f"/heartbeat?site={site}").status_code == 403
    assert rdb.get(f"active_token:{site}") is None
    assert noted and isinstance(noted[0], ZeroDivisionError)


def test_the_gate_inlines_the_current_picture_and_only_that(rdb, client, day, monkeypatch):
    names = {"cooldown-scene.js": "Red Line", "beacon-hill.js": "Beacon Hill"}
    for path in budget.SCENE_FILES:
        _pick(monkeypatch, path)
        html = _locked_out(rdb, client, monkeypatch)
        mine = names[os.path.basename(path)]
        assert mine in html and [n for n in names.values() if n != mine][0] not in html, path


@each_scene
def test_every_scene_is_a_drop_in_for_the_window(path):
    """The gate mounts window.CooldownScene on a 240x168 canvas inside the card; another
    picture is only a choice of file if it keeps that interface and that size."""
    js = _js(path)
    assert "G.CooldownScene={mount,lookFor,SEASONS,TIMES,size:[W,H]}" in js
    assert re.search(r"\bconst W=240,H=168\b", js)


@each_scene
def test_the_script_cannot_reach_the_network_or_storage(path):
    """It is same-origin with the gated site. Any of these in it is a new capability on
    that origin, and this is the test that says so before it ships."""
    js = _js(path)
    banned = [r"\bfetch\b", r"XMLHttpRequest", r"WebSocket", r"EventSource", r"sendBeacon",
              r"importScripts", r"\bimport\s*\(", r"\beval\b", r"new\s+Function", r"\bFunction\s*\(",
              r"document\.cookie", r"localStorage", r"sessionStorage", r"indexedDB",
              r"\blocation\b", r"window\.open", r"postMessage", r"document\.write", r"\.innerHTML",
              r"https?://", r"\bsrc\s*=", r"\.href"]
    found = [b for b in banned if re.search(b, js)]
    assert not found, found


@each_scene
def test_it_reads_the_clock_in_one_place_only(path):
    """The page picks the look. The one clock read is lookFor()'s default argument; a
    second one means something else is deciding what the picture shows."""
    js = re.sub(r"/\*.*?\*/", "", _js(path), flags=re.S)     # the header comment shows the call
    assert "Date.now" not in js and "performance.now" not in js
    assert js.count("new Date") == 1


@each_scene
def test_it_draws_only_with_whole_pixels_and_no_wash(path):
    js = _js(path)
    for bad in (".arc(", ".stroke(", ".fillText(", "globalAlpha", ".ellipse("):
        assert bad not in js, bad
    assert "imageSmoothingEnabled=false" in js


@each_scene
def test_it_stays_small_enough_to_inline_in_every_locked_out_gate(path):
    assert len(_js(path).encode()) < 32 * 1024


@each_scene
def test_nothing_in_it_can_be_read_as_template_syntax(path):
    """render_gate hands it over as a value, so Jinja never parses it; this guards the day
    someone pastes it into the template instead."""
    assert not re.search(r"\{\{|\{%|\{#", _js(path))


def test_every_picture_ships_in_both_deployment_shapes():
    """Each picture reaches the Pi through deploy.sh (copied, and stamped into the manifest the
    box audits) and each Docker image through a COPY. One shipped only one way would leave the
    other shape's gate blank every other day with nothing to say why: a missing picture is
    built to cost the picture and never the gate."""
    sh = open(os.path.join(ROOT, "deploy.sh")).read()
    loop = sh.split("for f in app.py", 1)[1].split("; do", 1)[0].split()
    assert "$DIR/scene" in sh.split("mkdir -p", 1)[1].split("\n", 1)[0]
    docker = {f: open(os.path.join(ROOT, f)).read().splitlines() for f in ("Dockerfile", "Dockerfile.tailscale")}
    for path in budget.SCENE_FILES:
        rel = "scene/" + os.path.basename(path)
        assert rel in loop and f"$DIR/{rel}={rel}" in sh, rel
        for name, lines in docker.items():
            assert any(l.startswith("COPY ") and rel in l.split() for l in lines), (name, rel)


def test_the_scene_is_not_a_new_route():
    """Inlined, so there is nothing to classify. If this ever becomes a route it needs a
    place in addon.MOVED_TO_BOX or BUDGET_ENDPOINTS, with a justification."""
    assert not [r for r in budget.app.url_map.iter_rules() if "scene" in r.rule]


def test_the_window_sits_inside_the_card_under_the_countdown(rdb, client, day, monkeypatch):
    """Where the Tetris link used to be: the bottom of the card, not a banner above it."""
    html = _locked_out(rdb, client, monkeypatch)
    card = html.index('<div class="card ')
    scene = html.index('<div id="cd-scene">')
    foots = html.index('class="foots"')
    assert card < scene < foots, (card, scene, foots)


def test_there_is_no_tetris_any_more(rdb, client, day, monkeypatch):
    """Retired 2026-10-17: it was hard to play on a phone. The gate offers no game, the box
    serves none, and the deploy ships none."""
    html = _locked_out(rdb, client, monkeypatch)
    assert "Tetris" not in html and "/play" not in html
    assert client.get("/play?min=3").status_code == 404
    assert not os.path.exists(os.path.join(ROOT, "play"))
    sh = open(os.path.join(ROOT, "deploy.sh")).read()
    assert "tetris" not in sh.lower() and "/play" not in sh


# ---------- a real browser ----------

PROBE = """<script>setTimeout(function(){var c=document.getElementById("cd-scene-c"),o=document.createElement("pre");o.id="probe";
var d=c.getContext("2d").getImageData(0,0,240,168).data,seen={},n=0;for(var i=0;i<d.length;i+=4){var k=d[i]<<16|d[i+1]<<8|d[i+2];if(!seen[k]){seen[k]=1;n++;}}
o.textContent=JSON.stringify({role:c.getAttribute("role"),aria:(c.getAttribute("aria-label")||"").slice(0,40),w:c.width,h:c.height,colours:n,cssw:c.style.width,cssh:c.style.height});
document.body.appendChild(o);},1500);</script>"""


def _serve_and_dump(html):
    seen = []

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            super().do_GET()

        def do_POST(self):
            seen.append("POST " + self.path)
            self.send_response(204)
            self.end_headers()

    with tempfile.TemporaryDirectory() as site, tempfile.TemporaryDirectory() as profile:
        with open(os.path.join(site, "index.html"), "w") as fh:
            fh.write(html)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=site))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out = subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                 f"--user-data-dir={profile}", "--force-device-scale-factor=3", "--window-size=390,844",
                 "--virtual-time-budget=5000", "--dump-dom", f"http://127.0.0.1:{srv.server_address[1]}/"],
                capture_output=True, text=True, timeout=60)
        finally:
            srv.shutdown()
    return out.stdout, seen


PROBE_TOP = """<script>setTimeout(function(){var c=document.getElementById("cd-scene-c"),o=document.createElement("pre");o.id="probe";
var r=c.getBoundingClientRect(),k=document.querySelector(".card").getBoundingClientRect(),e=document.scrollingElement;
o.textContent=JSON.stringify({top:Math.round(r.top),cardBottom:Math.round(k.bottom),scrollH:e.scrollHeight,vh:innerHeight});document.body.appendChild(o);},1500);</script>"""


@pytest.mark.skipif(CHROME is None, reason="needs headless Chrome")
def test_a_page_taller_than_the_screen_starts_at_the_top_and_scrolls(rdb, client, day, monkeypatch):
    """The first version centred a fixed-height flex body, so content taller than the screen
    overflowed equally off the TOP and the bottom, and the top (the ad cards) could not be
    scrolled to. Phone viewports are short enough to hit it; this makes the card taller than
    any viewport so the test does not depend on the window size Chrome gives it."""
    import json
    html = _locked_out(rdb, client, monkeypatch)
    html = html.replace("</head>", "<style>.card{min-height:2500px}</style></head>").replace("</body>", PROBE_TOP + "</body>")
    dom, _ = _serve_and_dump(html)
    probe = json.loads(re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S).group(1))
    assert probe["top"] >= 0, probe
    assert probe["scrollH"] >= probe["cardBottom"] > probe["vh"], probe


def _paths(seen):
    return {p.split("?")[0] for p in seen}


# What each picture says it is, so the check below knows it drew the file it was handed.
NAMES = {"cooldown-scene.js": "Red Line", "beacon-hill.js": "Beacon Hill"}
NO_FRAMES = "<script>window.requestAnimationFrame=function(){return 0};</script>"


@pytest.mark.skipif(CHROME is None, reason="needs headless Chrome")
@each_scene
def test_it_paints_before_the_first_animation_frame(rdb, client, day, monkeypatch, path):
    """The loop draws on animation frames, and only every 66 ms. A hidden tab delivers none,
    and headless Chrome under a virtual-time budget gives two or three, all before 66 ms, in
    about one run in four -- which is what made the draws-and-asks-for-nothing check flaky for
    the Red Line window until it, too, painted at mount. A picture that does depends on neither."""
    import json
    _pick(monkeypatch, path)
    html = _locked_out(rdb, client, monkeypatch).replace("<head>", "<head>" + NO_FRAMES, 1).replace("</body>", PROBE + "</body>")
    dom, _ = _serve_and_dump(html)
    probe = json.loads(re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S).group(1))
    assert probe["colours"] > 60, probe


@pytest.mark.skipif(CHROME is None, reason="needs headless Chrome")
@each_scene
def test_in_a_real_browser_it_draws_and_asks_for_nothing(rdb, client, day, monkeypatch, tmp_path, path):
    import json
    _pick(monkeypatch, path)
    html = _locked_out(rdb, client, monkeypatch).replace("</body>", PROBE + "</body>")
    dom, seen = _serve_and_dump(html)
    probe = json.loads(re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S).group(1))
    assert probe["role"] == "img" and NAMES[os.path.basename(path)] in probe["aria"], probe
    assert (probe["w"], probe["h"]) == (240, 168)
    assert probe["colours"] > 60, "a blank canvas has one colour; the scene has well over sixty"
    # A whole multiple of the native size in DEVICE pixels, on both axes (dpr is 3 here).
    zw, zh = float(probe["cssw"][:-2]) * 3 / 240, float(probe["cssh"][:-2]) * 3 / 168
    assert zw == zh and zw == int(zw) and zw >= 1, probe
    # The gate already makes its own calls (the traffic background polls /budget/feed).
    # The scene must add none: the same gate with no picture asks for exactly the same.
    _pick(monkeypatch, tmp_path / "none.js")
    _, baseline = _serve_and_dump(_locked_out(rdb, client, monkeypatch))
    assert _paths(seen) == _paths(baseline), (_paths(seen), _paths(baseline))


PROBE_CARD = """<script>setTimeout(function(){var c=document.getElementById("cd-scene-c"),o=document.createElement("pre");o.id="probe";
var r=c.getBoundingClientRect(),k=document.querySelector(".card"),b=k.getBoundingClientRect();
o.textContent=JSON.stringify({inCard:!!c.closest(".card"),l:Math.round(r.left-b.left),rr:Math.round(b.right-r.right),t:Math.round(r.top-b.top),bt:Math.round(b.bottom-r.bottom),w:Math.round(r.width)});document.body.appendChild(o);},1500);</script>"""


@pytest.mark.skipif(CHROME is None, reason="needs headless Chrome")
def test_in_a_real_browser_the_window_is_inside_the_card_and_not_clipped(rdb, client, day, monkeypatch):
    import json
    html = _locked_out(rdb, client, monkeypatch).replace("</body>", PROBE_CARD + "</body>")
    dom, _ = _serve_and_dump(html)
    probe = json.loads(re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S).group(1))
    assert probe["inCard"], probe
    assert min(probe["l"], probe["rr"], probe["t"], probe["bt"]) >= 0, probe
    assert probe["w"] >= 320, "the size it had above the card: 4x on a 390 px phone at dpr 3"
