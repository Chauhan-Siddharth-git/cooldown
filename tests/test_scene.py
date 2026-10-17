"""The lockout window: a pixel-art Red Line car on the gate, once you are locked out.

The picture is one script (scene/cooldown-scene.js) inlined into the gate. That script runs
on the GATED SITE's origin -- same-origin with Reddit or YouTube -- so the properties worth
pinning are not about how it looks but about what it can reach: no network, no storage, no
cookies, no way out of its <script> element, and a size that does not bloat every locked-out
page. The art itself is checked by eye (pixel-art/ in the pomodoro project); what is checked
here is that the gate shows it only when it should, survives it going missing, and that in a
real browser it draws and asks for nothing.
"""
import functools
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
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def _js():
    with open(SCENE, encoding="utf-8") as fh:
        return fh.read()


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
    monkeypatch.setattr(budget, "SCENE_FILE", str(tmp_path / "nope.js"))
    html = _locked_out(rdb, client, monkeypatch)
    assert 'id="cd-scene' not in html and "CooldownScene" not in html
    assert '<div class="card ' in html and "<h1" in html, "the rest of the locked-out gate still renders"


def test_a_file_that_could_break_out_of_its_script_is_refused(rdb, client, day, monkeypatch, tmp_path):
    bad = tmp_path / "bad.js"
    bad.write_text("var x=1;</SCRIPT><script>alert(1)</script>")
    monkeypatch.setattr(budget, "SCENE_FILE", str(bad))
    assert budget.scene_js() == ""
    html = _locked_out(rdb, client, monkeypatch)
    assert "alert(1)" not in html


def test_the_script_cannot_reach_the_network_or_storage():
    """It is same-origin with the gated site. Any of these in it is a new capability on
    that origin, and this is the test that says so before it ships."""
    js = _js()
    banned = [r"\bfetch\b", r"XMLHttpRequest", r"WebSocket", r"EventSource", r"sendBeacon",
              r"importScripts", r"\bimport\s*\(", r"\beval\b", r"new\s+Function", r"\bFunction\s*\(",
              r"document\.cookie", r"localStorage", r"sessionStorage", r"indexedDB",
              r"\blocation\b", r"window\.open", r"postMessage", r"document\.write", r"\.innerHTML",
              r"https?://", r"\bsrc\s*=", r"\.href"]
    found = [b for b in banned if re.search(b, js)]
    assert not found, found


def test_it_reads_the_clock_in_one_place_only():
    """The page picks the look. The one clock read is lookFor()'s default argument; a
    second one means something else is deciding what the picture shows."""
    js = re.sub(r"/\*.*?\*/", "", _js(), flags=re.S)         # the header comment shows the call
    assert "Date.now" not in js and "performance.now" not in js
    assert js.count("new Date") == 1


def test_it_draws_only_with_whole_pixels_and_no_wash():
    js = _js()
    for bad in (".arc(", ".stroke(", ".fillText(", "globalAlpha", ".ellipse("):
        assert bad not in js, bad
    assert "imageSmoothingEnabled=false" in js


def test_it_stays_small_enough_to_inline_in_every_locked_out_gate():
    assert len(_js().encode()) < 32 * 1024


def test_nothing_in_it_can_be_read_as_template_syntax():
    """render_gate hands it over as a value, so Jinja never parses it; this guards the day
    someone pastes it into the template instead."""
    assert not re.search(r"\{\{|\{%|\{#", _js())


def test_the_deploy_ships_the_picture():
    sh = open(os.path.join(ROOT, "deploy.sh")).read()
    assert "scene/cooldown-scene.js; do" in sh
    assert "$DIR/scene/cooldown-scene.js=scene/cooldown-scene.js" in sh
    assert "$DIR/scene" in sh.split("mkdir -p", 1)[1].split("\n", 1)[0]


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


@pytest.mark.skipif(CHROME is None, reason="needs headless Chrome")
def test_in_a_real_browser_it_draws_and_asks_for_nothing(rdb, client, day, monkeypatch, tmp_path):
    import json
    html = _locked_out(rdb, client, monkeypatch).replace("</body>", PROBE + "</body>")
    dom, seen = _serve_and_dump(html)
    probe = json.loads(re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S).group(1))
    assert probe["role"] == "img" and "Red Line" in probe["aria"], probe
    assert (probe["w"], probe["h"]) == (240, 168)
    assert probe["colours"] > 60, "a blank canvas has one colour; the scene has well over sixty"
    # A whole multiple of the native size in DEVICE pixels, on both axes (dpr is 3 here).
    zw, zh = float(probe["cssw"][:-2]) * 3 / 240, float(probe["cssh"][:-2]) * 3 / 168
    assert zw == zh and zw == int(zw) and zw >= 1, probe
    # The gate already makes its own calls (the traffic background polls /budget/feed).
    # The scene must add none: the same gate with no picture asks for exactly the same.
    monkeypatch.setattr(budget, "SCENE_FILE", str(tmp_path / "none.js"))
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
    assert probe["w"] >= 240, "at least one device pixel per art pixel on a 390 px phone"
