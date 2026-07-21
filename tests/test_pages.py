"""Gate + stats rendering per state, and the enter/study routes."""
import time
from urllib.parse import quote

import app as budget


def gate(client, site="reddit"):
    return client.get(f"/budget?site={site}").data.decode()


# ---------- gate states ----------

def test_day_enter_page(client, rdb, day):
    html = gate(client, "youtube")
    assert "Enter YouTube" in html
    assert "15:00" in html                       # full budget as the headline
    assert "Study mode" in html
    assert "/budget/stats" in html               # footer link
    assert "Budget" not in html                  # renamed to Countdown


def test_day_site_spent_steers_no_cooldown(client, rdb, day):
    rdb.set("spent:main", 600)
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
    assert "Study mode" in html                  # study stays reachable at night
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


# ---------- enter / study ----------

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


def test_study_locked_to_playlist_and_always_open(client, rdb, night):
    rdb.set("spent:main", 900)                   # everything drained, at night
    resp = client.post("/study?site=youtube")
    assert "playlist?list=" in resp.headers["Location"]
    tok = rdb.get("active_token:youtube")
    assert tok and rdb.get(f"session:{tok}") == "study"


def test_study_is_youtube_only(client, rdb, day):
    resp = client.post("/study?site=reddit")
    assert "/budget" in resp.headers["Location"]


def test_news_gate_renders(client, rdb, day):
    html = gate(client, "news")
    assert "Enter News" in html
    assert "10:00" in html                       # 10-min cap headline
    assert "/budget/study" not in html           # news has no study mode


def test_news_enter_returns_to_the_article(client, rdb, day):
    nxt = quote("https://www.cnn.com/2026/07/20/politics/story/index.html", safe="")
    resp = client.post(f"/enter?site=news&next={nxt}")
    assert "cnn.com/2026/07/20/politics" in resp.headers["Location"]   # not the home fallback


def test_cooldown_screen_promotes_study(client, rdb, day):
    rdb.set("cooldown:main", time.time() - 100)      # YouTube in cooldown
    html = gate(client, "youtube")
    assert "Study while you wait" in html            # promoted CTA copy
    assert "study-cta" in html                       # primary styling
    assert "one tap away" in html                    # nudge in the message


def test_cooldown_screen_shows_escalation_note(client, rdb, day):
    rdb.set("cooldown:main", time.time() - 100)
    rdb.set("cooldown_secs:main", 7200)              # an escalated (2h) wall
    html = gate(client, "reddit")
    assert "Back-to-back sessions get a longer break" in html


def test_reddit_cooldown_has_no_study_button(client, rdb, day):
    rdb.set("cooldown:main", time.time() - 100)      # Reddit has no study mode
    html = gate(client, "reddit")
    assert "Study while you wait" not in html
    assert "/budget/study" not in html               # no study form at all for reddit


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
    # Keep both events within the last couple minutes so they can't straddle midnight
    # into the previous local day (which would drop them from "today").
    rdb.rpush(f"cooldown_events:{today}", f"{int(now - 120)} reddit")    # ~2 min ago
    rdb.rpush(f"cooldown_events:{today}", f"{int(now - 30)} youtube")    # ~30s ago -> rapid
    rdb.set("last_charge", now)
    html = client.get("/stats").data.decode()
    assert "binge clustering" in html
    assert 'class="cd-n">2<' in html                 # two cooldowns today
    assert "1 rapid repeat" in html                  # within the 3h window


def test_stats_shows_study_minutes(client, rdb):
    today = time.strftime("%Y-%m-%d")
    rdb.set(f"study_usage:{today}", 1800)            # 30 min of study today
    rdb.set("last_charge", time.time())
    html = client.get("/stats").data.decode()
    assert "Study mode — the point of all this" in html
    assert "30m" in html                             # today's study readout


def test_stats_study_zero_nudges(client, rdb):
    rdb.set("last_charge", time.time())
    html = client.get("/stats").data.decode()
    assert "No study-mode time logged this week" in html


def test_stats_no_cooldowns_yet(client, rdb):
    rdb.set("last_charge", time.time())
    html = client.get("/stats").data.decode()
    assert "No cooldowns logged yet" in html


def test_stats_stale_heartbeat_warns(client, rdb):
    rdb.set("last_charge", time.time() - 5 * 86400)
    html = client.get("/stats").data.decode()
    assert "broken heartbeat" in html
