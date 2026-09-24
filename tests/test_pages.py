"""Gate + stats rendering per state, and the enter route."""
import time

import pytest
from urllib.parse import quote

import app as budget

def gate(client, site="reddit"):
    return client.get(f"/budget?site={site}").data.decode()


# ---------- gate states ----------

def test_day_enter_page(client, rdb, day):
    html = gate(client, "youtube")
    assert "Enter YouTube" in html
    cap = budget.SITES["youtube"]["budget_seconds"]
    assert f"{cap // 60}:{cap % 60:02d}" in html   # full budget as the headline
    assert "/budget/stats" not in html           # the dashboard is NOT on this origin
    assert "Budget" not in html                  # renamed to Countdown


def test_gate_links_to_the_dashboard_absolutely(client, rdb, day, monkeypatch):
    """The footer must point at the box, not at a path on the gated site — that path
    no longer exists there, and a relative link would put the dashboard back inside
    the origin it was moved out of."""
    monkeypatch.setattr(budget, "monitor_origin", lambda *a: "http://100.64.0.1:5000")
    html = gate(client, "youtube")
    # ?from= is load-bearing, not decoration: the dashboard is on a different origin and
    # has no other way to know where you tapped through from, so without it there is no
    # way back to the gate.
    assert 'href="http://100.64.0.1:5000/stats?from=youtube"' in html
    assert 'href="http://100.64.0.1:5000/health?from=youtube"' in html


def test_gate_hides_dashboard_links_when_the_box_has_no_address(client, rdb, day, monkeypatch):
    """Better no link than one that hangs."""
    monkeypatch.setattr(budget, "monitor_origin", lambda *a: "")
    html = gate(client, "youtube")
    assert "Usage stats" not in html
    assert "Pi health" not in html
    assert "Enter YouTube" in html               # the gate itself still works


def test_day_site_spent_steers_no_cooldown(client, rdb, day):
    rdb.set("spent:main", budget.SITES["reddit"]["budget_seconds"])
    html = gate(client, "reddit")
    assert "Reddit is done for now" in html
    assert "YouTube" in html                     # steer to remaining time
    assert rdb.get("cooldown:main") is None


def test_day_full_drain_starts_cooldown_with_live_timer(client, rdb, day):
    rdb.set("spent:main", 900)
    html = gate(client, "youtube")
    assert rdb.get("cooldown:main") is not None
    # Live countdown to reopen — the full ~1h base cooldown (a hair under 3600 since a
    # sliver has already elapsed against the just-set start).
    assert 'data-secs="359' in html
    assert rdb.get("cooldown_secs:main") == "3600"   # base duration (no prior clustering)


def test_cooldown_page_counts_down(client, rdb, day):
    rdb.set("cooldown:main", time.time() - 100)
    html = gate(client)
    assert "Take a break" in html
    assert 'data-secs="34' in html or 'data-secs="35' in html


def test_night_bedtime_closed(client, rdb, night):
    rdb.set("night_spent:main", budget.NIGHT_BUDGET_SECONDS)   # night buffer used up
    html = gate(client, "youtube")
    assert "Bedtime" in html
    assert rdb.get("cooldown:main") is None      # closing != cooldown


def test_night_buffer_enterable(client, rdb, night):
    rdb.set("night_spent:main", 60)
    html = gate(client)
    assert "Night mode" in html
    assert "Enter Reddit" in html
    assert "4:00" in html                        # 300-60 of the night buffer


def test_night_buffer_independent_of_day_spend(client, rdb, night):
    rdb.set("spent:main", 900)                    # whole DAY bucket drained
    rdb.set("cooldown:main", time.time())         # and a leftover daytime cooldown
    html = gate(client)
    assert "Night mode" in html                   # still get the fresh night buffer
    assert "Enter Reddit" in html
    assert "5:00" in html                         # full 300s, day spend doesn't eat it


def test_winddown_paused_when_ramp_cap_spent(client, rdb, winddown):
    rdb.set("spent:main", 800)                   # above any wind-down cap
    html = gate(client)
    assert "Winding down" in html
    assert "Enter Reddit" not in html


def test_night_gate_beats_leftover_cooldown(client, rdb, night):
    rdb.set("spent:main", 900)
    rdb.set("night_spent:main", budget.NIGHT_BUDGET_SECONDS)  # night buffer used up
    rdb.set("cooldown:main", time.time() - 100)  # daytime cooldown still ticking
    html = gate(client)
    assert "Bedtime" in html                     # night owns the gate, not the cooldown
    assert "Take a break" not in html


# ---------- enter ----------

def test_enter_grants_session(client, rdb, day):
    resp = client.post("/enter?site=reddit")
    assert resp.status_code == 302
    assert "reddit.com" in resp.headers["Location"]
    tok = rdb.get("active_token:reddit")
    assert tok and rdb.get(f"session:{tok}") == "active"


def test_enter_refused_when_spent(client, rdb, day):
    rdb.set("spent:main", 600)
    resp = client.post("/enter?site=reddit")
    assert "/budget" in resp.headers["Location"]
    assert rdb.get("active_token:reddit") is None


def test_gate_shows_short_break_during_cluster_cooldown(client, rdb, day):
    rdb.setex("soft_cd:reddit", 1200, "x")           # 20 min cluster brake
    html = gate(client, "reddit")
    assert "Short break" in html
    assert "Enter Reddit" not in html


def test_enter_refused_during_cluster_cooldown(client, rdb, day):
    rdb.setex("soft_cd:reddit", 1200, "x")
    resp = client.post("/enter?site=reddit")
    assert "/budget" in resp.headers["Location"]
    assert rdb.get("active_token:reddit") is None


def test_enter_returns_to_original_link(client, rdb, day):
    deep = "https://www.reddit.com/r/python/comments/abc/some_title/"
    resp = client.post("/enter?site=reddit&next=" + quote(deep, safe=""))
    assert resp.status_code == 302
    assert resp.headers["Location"] == deep          # back to the link, not home


def test_enter_rejects_offsite_next(client, rdb, day):
    # Open-redirect guard: a next pointing off the gated site falls back to home.
    for bad in ("https://evil.example.com/x", "https://www.youtube.com/watch?v=1"):
        resp = client.post("/enter?site=reddit&next=" + quote(bad, safe=""))
        assert resp.headers["Location"] == budget.SITES["reddit"]["home"]


def test_safe_next_blocks_parser_differential_bypasses():
    # urlparse-vs-browser disagreements that must NOT be treated as same-site.
    bypasses = [
        "https://evil.com\\@reddit.com/",      # backslash -> browser reads as "/"
        "https://evil.com%2f@reddit.com/",     # encoded slash + userinfo
        "https://reddit.com@evil.com/",        # userinfo trick
        "https://evil.com#@reddit.com/",
        " https://evil.com/",                  # leading space
        "https://reddit.com\t.evil.com/",      # tab injection
        "javascript:alert(1)//reddit.com",     # non-http scheme
        "//reddit.com/",                       # scheme-relative
    ]
    for b in bypasses:
        assert budget._safe_next("reddit", b) == "", b
    # ...while legitimate same-site URLs (incl. YouTube @handles) still pass.
    assert budget._safe_next("reddit", "https://old.reddit.com/r/x/")
    assert budget._safe_next("youtube", "https://www.youtube.com/@SomeChannel")


def test_enter_blocks_bypass_next_falls_back_home(client, rdb, day):
    resp = client.post("/enter?site=reddit&next=" +
                       quote("https://evil.com\\@reddit.com/", safe=""))
    assert resp.headers["Location"] == budget.SITES["reddit"]["home"]  # not evil.com


def test_gate_enter_form_carries_next(client, rdb, day):
    deep = "https://www.reddit.com/r/python/comments/abc/"
    html = client.get("/budget?site=reddit&next=" + quote(deep, safe="")).data.decode()
    assert "next=" in html                           # Enter form threads it through


def test_news_gate_renders(client, rdb, day):
    html = gate(client, "news")
    assert "Enter News" in html
    cap = budget.SITES["news"]["budget_seconds"]
    assert f"{cap // 60}:{cap % 60:02d}" in html   # the news cap, as the headline


def test_news_enter_returns_to_the_article(client, rdb, day):
    nxt = quote("https://www.cnn.com/2026/07/20/politics/story/index.html", safe="")
    resp = client.post(f"/enter?site=news&next={nxt}")
    assert "cnn.com/2026/07/20/politics" in resp.headers["Location"]   # not the home fallback


def test_cooldown_screen_shows_escalation_note(client, rdb, day):
    rdb.set("cooldown:main", time.time() - 100)
    rdb.set("cooldown_secs:main", 7200)              # an escalated (2h) wall
    html = gate(client, "reddit")
    # The wording rotates now, so assert the meaning is present rather than one phrasing —
    # and that it came from the escalated bank, not the plain one.
    assert any(v.format(label="Reddit") in html for v in budget.GATE_LINES["cooldown_escalated"])
    assert not any(v.format(label="Reddit") in html for v in budget.GATE_LINES["cooldown"])


# ---------- stats ----------

def test_stats_renders_history(client, rdb):
    today = time.strftime("%Y-%m-%d")
    rdb.set(f"usage:{today}:reddit", 720)
    rdb.set("last_charge", time.time() - 3600)
    html = client.get("/stats").data.decode()
    assert "Usage · Countdown" in html
    assert "12m" in html                         # today tile
    assert "Heartbeat alive" in html
    assert "Table view" in html


def test_stats_shows_cooldown_clustering(client, rdb):
    today = time.strftime("%Y-%m-%d")
    now = time.time()
    # Clamp both events INTO today rather than merely close to now. The previous version
    # kept them "within the last couple of minutes" to avoid straddling midnight, which is
    # precisely what breaks just after it: at 00:01 those minutes were yesterday, so events
    # filed under today's key carry yesterday's timestamps and the page drops them. Failed
    # only in the ~2 minutes after local midnight; passed in every other timezone tested.
    start = time.mktime(time.strptime(today, "%Y-%m-%d"))
    rdb.rpush(f"cooldown_events:{today}", f"{int(max(now - 120, start + 1))} reddit")
    rdb.rpush(f"cooldown_events:{today}", f"{int(max(now - 30, start + 31))} youtube")  # -> rapid
    rdb.set("last_charge", now)
    html = client.get("/stats").data.decode()
    assert "binge clustering" in html
    assert 'class="cd-n">2<' in html                 # two cooldowns today
    assert "1 rapid repeat" in html                  # within the 3h window
def test_stats_no_cooldowns_yet(client, rdb):
    rdb.set("last_charge", time.time())
    html = client.get("/stats").data.decode()
    assert "No cooldowns logged yet" in html


def test_stats_stale_heartbeat_warns(client, rdb):
    rdb.set("last_charge", time.time() - 5 * 86400)
    html = client.get("/stats").data.decode()
    assert "broken heartbeat" in html


# ---------- pi health monitor ----------

def test_health_page_renders(client):
    html = client.get("/health").data.decode()
    assert 'id="eth"' in html          # the ethernet element (goes green when up)
    assert 'id="soc"' in html          # the temp-tinted SoC
    assert "Usage stats" in html       # nav back to stats

def test_health_json_has_keys(client):
    data = client.get("/health?fmt=json").get_json()
    for k in ("model", "cpu", "mem", "disk", "net", "uptime", "services", "temp_hist"):
        assert k in data
    assert "eth0" in data["net"]
    assert set(data["cpu"]) >= {"pct", "load", "cores", "per_core"}


# ---------- devices (tailscale) ----------

def test_devices_page_renders(client):
    html = client.get("/devices").data.decode()
    assert 'id="dev-phone"' in html          # phone node
    assert 'id="dev-laptop"' in html         # laptop node
    assert "Pi health" in html               # nav

def test_devices_json_shape(client):
    data = client.get("/devices?fmt=json").get_json()
    assert {"ok", "self", "devices"} <= set(data)
    assert isinstance(data["devices"], list)   # empty is fine on a non-tailscale host

def test_feed_json_shape(client):
    data = client.get("/feed").get_json()   # off-Pi (no counters) degrades to zeros, never errors
    assert set(data) == {"enc", "unenc"}
    assert isinstance(data["enc"], int) and isinstance(data["unenc"], int)

def test_active_peer_reads_as_online():
    # A directly-connected peer can report Online=false while still Active with traffic;
    # it must NOT show as offline (else "offline, but downloading").
    p = {"HostName": "lap", "OS": "linux", "Online": False, "Active": True,
         "CurAddr": "10.0.0.1:41641", "TailscaleIPs": ["100.1.1.9"], "RxBytes": 1, "TxBytes": 2}
    dev = budget._device(p)
    assert dev["online"] is True
    assert dev["direct"] is True
    assert dev["kind"] == "computer"


# ---------- tamper-evidence: an unexplained reboot ----------

def test_boot_watch_is_quiet_on_first_run(rdb):
    """Nothing to compare against yet — must not cry wolf."""
    assert budget.boot_watch() is None
    assert rdb.get("last_boot_id") is not None      # but it does start watching

def test_boot_watch_flags_a_reboot(rdb):
    budget.boot_watch()                              # establish a baseline
    rdb.set("last_boot_id", "some-earlier-boot")     # the box rebooted
    assert budget.boot_watch() is not None
    assert len(rdb.lrange("boot_events", 0, -1)) == 1

def test_health_page_warns_and_can_be_dismissed(client, rdb):
    budget.boot_watch()
    rdb.set("last_boot_id", "some-earlier-boot")
    assert "This box restarted" in client.get("/health").data.decode()
    client.post("/boot-ack")
    assert rdb.get("unacked_boot") is None
    assert "This box restarted" not in client.get("/health").data.decode()

def test_service_restart_alone_is_not_a_reboot(rdb):
    """Restarting the app must not look like tampering — boot_id only changes on boot."""
    budget.boot_watch()
    for _ in range(3):
        assert budget.boot_watch() is None


# --- /cpu detail page ---------------------------------------------------------------

def _hist(monkeypatch, n, cores=4, v=50.0):
    """Install a known CPU history.

    collect_health() caches for 2s, so patching _CPU_HIST alone is not enough -- the
    route serves the previous test's payload and the fixture is silently ignored.
    Every one of these tests passed against real machine data before this cleared.
    """
    import collections
    monkeypatch.setattr(budget, "_CPU_HIST",
                        collections.deque([[v + c for c in range(cores)] for _ in range(n)],
                                          maxlen=budget.CPU_HIST_LEN))
    budget._HEALTH_CACHE.clear()


def test_cpu_page_renders_axis_labels_and_a_line_per_core(client, monkeypatch):
    _hist(monkeypatch, 60)
    r = client.get("/cpu")
    assert r.status_code == 200
    html = r.data.decode()
    # The whole point of the page: a percentage axis you can actually read a value off.
    for label in ("100%", "75", "50", "25", "0"):
        assert f">{label}</span>" in html
    assert html.count("<polyline") >= 4          # one per core
    for colour in budget.CPU_LINE_COLORS:
        assert colour in html


def test_cpu_page_survives_an_empty_history(client, monkeypatch):
    """First request after a restart has no samples. It must render, not 500."""
    _hist(monkeypatch, 0)
    assert client.get("/cpu").status_code == 200


def test_cpu_card_shows_a_shorter_window_than_the_detail_page(client, monkeypatch):
    """The card is a glance, the page is the record. If the card silently grew to the
    full buffer its own 'last N min' label would start lying."""
    assert budget.CPU_CARD_SAMPLES < budget.CPU_HIST_LEN
    _hist(monkeypatch, budget.CPU_HIST_LEN)
    # Assert on what the routes actually emit. Calling _cpu_lines() directly here
    # passed even with the route's slice deleted -- it tested the helper, not the page.
    def first_polyline(html):
        body = html.split('<polyline', 1)[1].split('points="', 1)[1].split('"', 1)[0]
        return len(body.split())

    assert first_polyline(client.get("/health").data.decode()) == budget.CPU_CARD_SAMPLES
    budget._HEALTH_CACHE.clear()
    assert first_polyline(client.get("/cpu").data.decode()) == budget.CPU_HIST_LEN


def test_health_card_links_to_the_cpu_page_keeping_the_referrer_crumb(client, monkeypatch):
    _hist(monkeypatch, 10)
    html = client.get("/health?from=reddit").data.decode()
    assert 'href="/cpu?from=reddit"' in html


def test_cpu_stat_rows_report_now_avg_and_peak_per_core(monkeypatch):
    rows = budget._cpu_stat_rows([[10.0, 0.0], [90.0, 0.0], [50.0, 0.0]])
    assert rows[0]["now"] == 50 and rows[0]["avg"] == 50 and rows[0]["peak"] == 90
    assert rows[0]["color"] != rows[1]["color"]


# --- the hidden attribute must actually hide ------------------------------------------

def _css_and_html(client, rdb, monkeypatch):
    """The gate with the reflection panel forced on."""
    monkeypatch.setattr(budget, "reflect_decision", lambda now=None: (True, "why?"))
    return gate(client, "reddit")


def test_hidden_attribute_is_not_defeated_by_a_class_rule(client, rdb, day, monkeypatch):
    """[hidden] is a UA-stylesheet rule and ANY author rule setting display beats it --
    author beats UA regardless of specificity. `.actions{display:flex}` therefore un-hid
    <div class="actions" id="ractions" hidden>, the reflection panel's button row, so
    "Continue anyway" was enabled before a chip was ever picked. The 15s hold only starts
    on a chip click, so it never ran; the trigger field stayed empty, so log_reflection()
    silently dropped every entry. 44 prompts over six days recorded zero reflections and
    nothing anywhere reported a problem.

    The bug is invisible to any test that greps the HTML: the markup was always correct.
    """
    import re
    html = _css_and_html(client, rdb, monkeypatch)
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))

    # Every class on an element that carries the hidden attribute.
    hidden_classes = set()
    for tag in re.findall(r"<[a-zA-Z][^>]*\bhidden\b[^>]*>", html):
        m = re.search(r'class="([^"]*)"', tag)
        if m:
            hidden_classes.update(m.group(1).split())

    # Any author rule that sets display on one of those classes.
    collisions = []
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if not re.search(r"(^|[^-\w])display\s*:", body):
            continue
        for cls in hidden_classes:
            if re.search(r"\." + re.escape(cls) + r"(?![-\w])", sel):
                collisions.append((cls, " ".join(sel.split())[:60]))

    guard = re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", css)
    assert guard or not collisions, (
        "these classes appear on [hidden] elements AND have a display rule, so the "
        "elements render anyway; add [hidden]{display:none!important}: "
        + "; ".join(f"{c} via {s}" for c, s in collisions))


def test_the_reflection_panel_button_row_is_one_of_those_elements(client, rdb, day, monkeypatch):
    """Guards the guard. The test above passes trivially if the markup stops using
    class="actions" on a hidden element -- at which point it is checking nothing, and this
    project has shipped three exemption lists that decayed exactly that way.
    """
    import re
    html = _css_and_html(client, rdb, monkeypatch)
    m = re.search(r'<div[^>]*id="ractions"[^>]*>', html)
    assert m, "the reflection panel's button row is gone; retarget this test"
    assert "hidden" in m.group(0), "#ractions no longer relies on [hidden]"
    assert "actions" in re.search(r'class="([^"]*)"', m.group(0)).group(1)


def test_the_gate_reports_the_timezone_even_when_you_cannot_enter(client, rdb, night):
    """The bootstrap deadlock. note_client_tz() is reached from the heartbeat, and the
    heartbeat only runs inside a live session -- so on the evening you land after flying
    west, the box curfews you on the old zone, you cannot start a session, and nothing
    can tell it otherwise. The gate itself has to report.

    `night` fixture: the state where you are locked out, which is the state that matters.
    """
    html = gate(client, "reddit")
    assert "resolvedOptions().timeZone" in html
    assert "/budget/heartbeat?tz=" in html


def test_the_heartbeat_records_the_zone_before_it_refuses_the_request(client, rdb):
    """The 403 is fine; dropping the zone with it is not."""
    import app as b
    assert not rdb.get("tz_pending")
    res = client.post("/heartbeat?tz=America/Los_Angeles&site=reddit")
    assert res.status_code == 403, "no active session, so this must still be refused"
    pending = rdb.get("tz_pending") or ""
    assert pending.startswith("America/Los_Angeles"), f"zone dropped on the refused path: {pending!r}"


def test_every_blocked_screen_asks_whether_it_was_worth_it(client, rdb, monkeypatch):
    """The verdict is only ever collected on a screen you land on BECAUSE a session
    ended. Three such screens never asked -- night-closed, wind-down-spent and the short
    soft-cooldown break -- so a session that ended into any of them produced no verdict
    and pending_worth expired unanswered.

    Measured on the box: worth:* last recorded 2026-09-10, against ~60 entries in the
    five days after. regret_by_trigger() reads worth:*, so the regret number shown at the
    reflection prompt -- the whole point of that prompt -- was frozen on August data and
    could never update.
    """
    import app as b

    def blocked_screens():
        # (fixture-ish state, site) -> html, for each screen that means "not right now"
        out = {}
        rdb.set("pending_worth", f"{time.time():.0f} reddit bored")

        monkeypatch.setattr(b, "phase", lambda now=None: "night")
        monkeypatch.setattr(b, "get_remaining_budget", lambda s: 0)
        out["night closed"] = gate(client, "reddit")

        monkeypatch.setattr(b, "phase", lambda now=None: "winddown")
        out["winddown spent"] = gate(client, "reddit")

        monkeypatch.setattr(b, "phase", lambda now=None: "day")
        monkeypatch.setattr(b, "get_cooldown_remaining", lambda s: 0)
        monkeypatch.setattr(b, "get_soft_cd_remaining", lambda s: 300)
        out["short break"] = gate(client, "reddit")
        return out

    screens = blocked_screens()
    # Guard the loop. Without this the test passes if blocked_screens() ever returns
    # nothing -- which is how an assertion inside a loop stops being an assertion.
    assert set(screens) == {"night closed", "winddown spent", "short break"}, sorted(screens)
    for name, html in screens.items():
        assert "Was it worth it?" in html, f"{name} never asks for the verdict"


def test_entry_screens_still_do_not_ask(client, rdb, day, monkeypatch):
    """The counterweight. It must never appear above an Enter button -- that turns a
    check-in into a toll, which is the one thing the design note forbids.
    """
    rdb.set("pending_worth", f"{time.time():.0f} reddit bored")
    html = gate(client, "reddit")
    assert "Enter" in html
    assert "Was it worth it?" not in html


def test_the_deadman_row_appears_only_when_it_is_not_pinging(client, rdb, monkeypatch):
    """The switch cannot fail silently at the far end -- stopped pings ARE the alarm --
    but it can fail to have been set up at all, or its timer can stop, and both of those
    look like calm from here. Those are the cases the row exists for.

    It must also stay hidden when healthy: a row that is always present is a row you stop
    reading, which is how the reflection prompt became a rubber stamp.
    """
    import app as b

    def health_with(audit):
        monkeypatch.setattr(b, "_audit", lambda: audit)
        # collect_health() caches its payload for 2s so the 4s page poll does not spawn a
        # handful of systemctl subprocesses per hit. Three renders inside one test land
        # well inside that window, so without this every assertion below would be made
        # against the FIRST audit dict -- the test would pass or fail for reasons having
        # nothing to do with the template.
        b._HEALTH_CACHE.clear()
        return client.get("/health").data.decode()

    base = {"manifest_files": 12, "deployed_rev": "abc", "fresh": True}

    healthy = health_with({**base, "deadman_ok": True, "deadman_age": 120})
    assert "Dead-man" not in healthy, "the row shows while the switch is healthy"

    never = health_with({**base, "deadman_ok": False, "deadman_age": -1})
    assert "Dead-man" in never and "not pinging" in never

    stale = health_with({**base, "deadman_ok": False, "deadman_age": 2400})
    assert "Dead-man" in stale and "40 min ago" in stale

    # An audit that predates the field must not be read as a broken switch: absent is
    # unknown, and this row would otherwise fire on every box until the audit next ran.
    old = health_with({**base})
    assert "Dead-man" not in old, "a missing field rendered as a failure"


def test_health_shows_the_heartbeat_and_a_loud_flatline(client, rdb, tmp_path, monkeypatch):
    """Alive: a trace with a beat per ping and no alarm. Dead: the same trace in the bad
    colour with FLATLINE spelled out. No log: nothing at all, rather than a flat line that
    would read as an outage when the switch has simply never run."""
    import app as b
    log = tmp_path / "dm.log"
    monkeypatch.setattr(b, "DEADMAN_LOG", str(log))
    now = time.time()

    def page(lines):
        log.write_text("".join(l + "\n" for l in lines)) if lines is not None else (
            log.unlink() if log.exists() else None)
        b._HEALTH_CACHE.clear()
        return client.get("/health").data.decode()

    alive = page([f"{now - i * 300:.0f} ok" for i in range(36)])
    assert 'class="ecg ecg-ok"' in alive and "FLATLINE" not in alive

    dead = page([f"{now - 30 * 60 - i * 300:.0f} ok" for i in range(10)])
    assert 'class="ecg ecg-flat"' in dead and "FLATLINE" in dead

    none = page(None)
    assert "ecg" not in none.split("<body")[1], "rendered a trace with no ping history"


def test_the_heartbeat_animates_only_compositor_properties(client, rdb, tmp_path, monkeypatch):
    """Animating a path, a stroke or a filter repaints every frame. The frost overlay did
    that and dropped this page to a crawl, so the rule is written down as a test."""
    import app as b, re
    log = tmp_path / "dm.log"
    log.write_text(f"{time.time():.0f} ok\n")
    monkeypatch.setattr(b, "DEADMAN_LOG", str(log))
    b._HEALTH_CACHE.clear()
    html = client.get("/health").data.decode()
    frames = re.findall(r"@keyframes ecg-[a-z]+\{(.*?)\}\}", html, re.S)
    assert frames, "no heartbeat keyframes found -- this test is checking nothing"
    for f in frames:
        props = set(re.findall(r"([a-z-]+)\s*:", f))
        assert props <= {"transform", "opacity"}, f"animates a repainting property: {props}"
    assert "prefers-reduced-motion" in html


# --- health findings, in plain language -------------------------------------------------

def _audit_ids():
    import os, re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "deploy", "cooldown-audit.sh")
    if not os.path.exists(path):
        path = os.path.join(here, "deploy", "cooldown-audit.sh")
    src = open(path, encoding="utf-8").read()
    return {m.group(1) for m in re.finditer(r'(?:^|[ |&])note ([a-z0-9_]+) "', src, re.M)}


def test_every_audit_finding_has_a_plain_explanation():
    """The page used to show a count and say "Details: journalctl -t cooldown-audit". Every
    finding now carries an id the page turns into what is wrong, why it matters and what to
    do. A new audit check without an entry would silently fall back to technical text, so
    this fails instead."""
    import app as b
    ids = _audit_ids()
    assert len(ids) >= 40, f"only {len(ids)} audit ids found -- this test is checking nothing"
    missing = sorted(ids - set(b.FINDINGS))
    assert not missing, f"audit findings with no plain-language entry: {missing}"


def test_no_explanation_outlives_its_check():
    """Rule 10, the other direction: an entry for a finding the audit can no longer raise
    is an exemption list quietly going stale."""
    import app as b
    orphans = sorted(set(b.FINDINGS) - _audit_ids())
    assert not orphans, f"explanations for checks that no longer exist: {orphans}"


def test_every_explanation_says_what_to_do():
    """A warning with no resolution teaches you to ignore warnings. That is the exact
    complaint that produced this table."""
    import app as b
    for fid, (cat, title, meaning, action) in b.FINDINGS.items():
        assert cat in b.FINDING_CATEGORIES, fid
        assert title and meaning and action, f"{fid} is missing part of its explanation"
        assert len(action) > 12, f"{fid}: '{action}' is not an action"


def _health_with_findings(client, monkeypatch, findings):
    import app as b
    real = b._audit
    base = dict(real() or {})
    base.update({"fresh": True, "findings": len(findings), "checked_ago": 60,
                 "findings_list": findings, "ca_constrained": True})
    monkeypatch.setattr(b, "_audit", lambda: base)
    b._HEALTH_CACHE.clear()
    return client.get("/health").data.decode()


def test_a_feature_finding_is_not_presented_as_a_security_problem(client, rdb, monkeypatch):
    """The finding that started this: a missing "was it worth it?" answer, shown as
    "1 audit finding" under a Security heading. It must say No security problems, and
    appear under Features with its resolution."""
    html = _health_with_findings(client, monkeypatch, [
        {"id": "worth_silent", "detail": "no worth verdict in 5 days across 4 cooldowns"}])
    assert "No security problems" in html
    sec = html.split(">Security<", 1)[1].split('class="srow', 1)[0]
    assert "worth it" not in sec, "a feature finding leaked into the Security row"
    assert ">Features<" in html and "Answer it next time it appears" in html
    assert "journalctl -t cooldown-audit" not in html, "still telling the owner to open a terminal"


def test_a_real_security_finding_says_what_to_do(client, rdb, monkeypatch):
    html = _health_with_findings(client, monkeypatch, [
        {"id": "ssh_password", "detail": "sshd now accepts password authentication"}])
    sec = html.split(">Security<", 1)[1].split('class="srow', 1)[0]
    assert "1</b> security" in sec
    assert "accepts passwords" in sec and "PasswordAuthentication no" in sec


def test_an_unknown_finding_is_shown_not_dropped(client, rdb, monkeypatch):
    html = _health_with_findings(client, monkeypatch, [
        {"id": "brand_new_check", "detail": "something the page has never heard of"}])
    assert "something the page has never heard of" in html


def test_errors_read_as_english(client, rdb, monkeypatch):
    """'budget_handler: ReadTimeout, 3444m ago' -- an exception name and 2.4 days written as
    minutes."""
    import app as b
    assert b._short_ago(3444 * 60) == "2 days ago"
    assert b._short_ago(30) == "just now"
    assert b._short_ago(3 * 3600) == "3 hours ago"
    assert b._short_ago(None) == "at an unknown time"
    assert "took too long" in b.plain_error("budget_handler: ReadTimeout")
    assert "ReadTimeout" in b.plain_error("budget_handler: ReadTimeout"), "keep the name for debugging"


def test_findings_survive_the_real_audit_reader_to_the_page(client, rdb, tmp_path, monkeypatch):
    """Through the REAL _audit(), from a real JSON file, to the rendered page.

    Every other findings test here replaces _audit() with a fake that already contains
    findings_list. That is exactly how a missing line in _audit() -- the reader that
    rebuilds the record field by field -- shipped to the live box: the audit wrote the
    list, the page never received it, and the tests stayed green because they had
    stepped around the one layer that was broken. This one does not."""
    import app as b, json
    f = tmp_path / "audit.json"
    f.write_text(json.dumps({
        "findings": 1, "checked": time.time() - 120, "ssh_keys": 1, "exposed_ports": "",
        "ca_constrained": True, "ca_days": 3595, "mode": "quick",
        "findings_list": [{"id": "worth_silent", "detail": "no worth verdict in 5 days"}]}))
    monkeypatch.setattr(b, "AUDIT_STATE", str(f))
    b._HEALTH_CACHE.clear()
    html = client.get("/health").data.decode()
    assert "Answer it next time it appears" in html, "the finding did not reach the page"
    assert "can't describe yet" not in html, "fell back to the unexplained-finding message"
    assert "No security problems" in html


def test_the_second_clock_card_states_the_real_threshold(client, rdb, monkeypatch):
    """The card tells the owner the warning fires below 40% of the second clock. That
    threshold is ENFORCEMENT_RATIO in addon.py, a different process, so the page cannot
    read it directly -- and a number copied into a template is how "172 processes exist"
    went stale on the health page. This fails if the two drift apart."""
    import app as b, addon
    monkeypatch.setattr(b, "shadow_comparison", lambda *a, **k: {
        "any": True, "settled": True, "days": 7, "running": "167 compared hours",
        "rows": [], "hb": 382, "sh": 450, "ratio": 1.18})
    html = client.get("/stats").data.decode()
    pct = round(addon.ENFORCEMENT_RATIO * 100)
    assert f"<b>{pct}%</b>" in html, f"the card no longer states the real {pct}% threshold"
    assert "the injection could go" not in html, "the stale conclusion is back"
    assert "give it a few days" not in html.lower()
