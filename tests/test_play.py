"""One fixed-length round of Tetris, offered by the gate once you are locked out.

A feed never ends; this always does, and the same way -- a wind-down, not a buzzer. It
lives on the box's own origin like the rest of the dashboard, and is a copy of the
pomodoro project's tetris.html with touch controls, because the gate is on a phone.
"""
import json
import os
import re
import shutil
import subprocess
import time

import pytest

import addon
import app as budget

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MON = "http://100.64.0.1:5000"


def test_play_is_box_origin_only():
    assert "/play" in addon.MOVED_TO_BOX
    assert "/play" not in addon.BUDGET_ENDPOINTS


def test_the_game_is_served_whole_and_touch_playable(rdb, client):
    html = client.get("/play?min=3").get_data(as_text=True)
    assert "touchstart" in html and "touch-action: none" in html
    assert 'name="cd-line"' in html
    # Self-contained: the box serves it alone, with nothing fetched from anywhere else.
    urls = [u for u in re.findall(r"https?://[^\s\"')]+", html) if "w3.org" not in u]
    assert not urls, urls


def test_it_offers_the_way_back_to_the_site_it_came_from(rdb, client):
    html = client.get("/play?min=3&from=reddit").get_data(as_text=True)
    assert 'name="cd-back" content="https://www.reddit.com"' in html
    assert 'name="cd-back-label" content="Reddit"' in html
    html = client.get("/play?min=3&from=javascript:alert(1)").get_data(as_text=True)
    assert 'name="cd-back"' not in html, "only a known site's home is ever linked"


def test_the_gate_offers_a_round_once_you_are_locked_out(rdb, client, day, monkeypatch):
    monkeypatch.setattr(budget, "monitor_origin", lambda *a: MON)
    rdb.set("cooldown:main", time.time() - 100)        # a cooldown that began 100s ago
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert f'href="{MON}/play?min=3&amp;from=reddit"' in html or \
           f'href="{MON}/play?min=3&from=reddit"' in html
    assert "3-minute round of Tetris" in html


def test_the_gate_does_not_offer_it_while_you_can_enter(rdb, client, day, monkeypatch):
    monkeypatch.setattr(budget, "monitor_origin", lambda *a: MON)
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert "Enter Reddit" in html
    assert "/play" not in html


def test_reclaimed_time_counts_finished_days_under_the_baseline(rdb):
    now = time.mktime((2026, 11, 4, 15, 0, 0, 0, 0, -1))         # the 4th: three days done
    for day, mins in (("2026-11-01", 40), ("2026-11-02", 70), ("2026-11-03", 50)):
        rdb.set(f"usage:{day}:reddit", mins * 60)
    rdb.set("usage:2026-11-04:reddit", 5 * 60)                    # today: not counted
    rdb.set("usage:2026-10-31:reddit", 0)                         # last month: not counted
    got = budget.reclaimed_this_month(now)
    want = (62.6 - 40) + 0 + (62.6 - 50)                          # a day over counts as 0
    assert abs(got - want) < 0.01, got


def test_the_deploy_ships_the_game():
    sh = open(os.path.join(ROOT, "deploy.sh")).read()
    assert "play/tetris.html; do" in sh and "$DIR/play/tetris.html=play/tetris.html" in sh


def test_the_touch_controls_work_on_the_real_game():
    """The gate is on a phone, and the original game was keyboard-only. This drives the
    shipped file's own script with real touch events (tests/tetris_touch_harness.js):
    tap rotates, a drag moves a column per cell, a slow drag down soft-drops, a flick
    down slams, a flick up holds, and touches on the start card are left alone so its
    button still works. The keyboard-only original fails every one of these."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    p = subprocess.run([node, os.path.join(ROOT, "tests", "tetris_touch_harness.js"),
                        os.path.join(ROOT, "play", "tetris.html")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got == {"titlePreventsDefault": False, "hasTouchHints": True, "state": "play",
                   "tapRotates": True, "dragRightMoves": 3, "slowDragDownSoftDrops": 2,
                   "flickDownHardDrops": True, "holdBefore": None, "flickUpHolds": True}, got
