"""The budget state machine: shared bucket, passive refill + grace window,
cooldown lifecycle, heartbeat charging/blocking, daily reset."""
import time

import app as budget

RATE = 0.25  # pool_max_budget("main") / REFILL_FULL_SECONDS = 900/3600


# ---------- shared bucket ----------

def test_shared_bucket_per_site_caps(rdb, day):
    rdb.set("spent:main", 600)
    assert budget.get_remaining_budget("reddit") == 0
    assert budget.get_remaining_budget("spotify") == 0
    assert round(budget.get_remaining_budget("youtube")) == 300


def test_pool_max_budget_is_largest_cap():
    assert budget.pool_max_budget("main") == 900


def test_news_shares_main_bucket(rdb, day):
    assert budget.pool("news") == "main"
    assert "news" in budget.pool_sites("main")       # switching to news is NOT an escape hatch
    rdb.set("spent:main", 600)                        # shared spend
    assert budget.get_remaining_budget("news") == 0   # news 10-min slice gone with the rest
    assert round(budget.get_remaining_budget("youtube")) == 300  # bigger cap still has room


def test_puzzmo_shares_bucket_with_10min_cap(rdb, day):
    assert budget.SITES["puzzmo"]["budget_seconds"] == 600
    assert budget.pool("puzzmo") == "main"           # same shared bucket
    rdb.set("spent:main", 550)
    assert round(budget.get_remaining_budget("puzzmo")) == 50
    assert "puzzmo" in budget.pool_sites("main")


# ---------- passive refill + grace ----------

def test_no_refill_inside_grace(rdb, day):
    rdb.set("spent:main", 600)
    rdb.set("last_heartbeat:main", time.time() - budget.REGEN_DELAY + 60)  # 14 min idle
    assert budget.get_spent("reddit") == 600


def test_refill_past_grace(rdb, day):
    rdb.set("spent:main", 600)
    rdb.set("last_heartbeat:main", time.time() - budget.REGEN_DELAY - 300)  # 5 min past
    assert abs(budget.get_spent("reddit") - (600 - 300 * RATE)) < 2


def test_refill_cursor_no_double_credit(rdb, day):
    now = time.time()
    rdb.set("spent:main", 525)
    rdb.set("last_heartbeat:main", now - budget.REGEN_DELAY - 360)
    rdb.set("refilled_through:main", now - 60)   # already credited up to 1 min ago
    assert abs(budget.get_spent("reddit") - (525 - 60 * RATE)) < 2


def test_no_refill_during_active_session(rdb, day, session):
    session("reddit", last_gap=budget.REGEN_DELAY + 600)
    rdb.set("spent:main", 600)
    assert budget.get_spent("reddit") == 600


def test_no_refill_during_cooldown(rdb, day):
    rdb.set("spent:main", 900)
    rdb.set("cooldown:main", time.time())
    rdb.set("last_heartbeat:main", time.time() - 3600)
    assert budget.get_spent("reddit") == 900


def test_no_refill_outside_day(rdb, night):
    rdb.set("spent:main", 300)
    rdb.set("last_heartbeat:main", time.time() - 3600)
    assert budget.get_spent("reddit") == 300


def test_refill_floors_at_zero(rdb, day):
    rdb.set("spent:main", 30)
    rdb.set("last_heartbeat:main", time.time() - budget.REGEN_DELAY - 7200)
    assert budget.get_spent("reddit") == 0


# ---------- cooldown lifecycle ----------

def test_cooldown_counts_down(rdb, day):
    rdb.set("cooldown:main", time.time() - 100)
    rem = budget.get_cooldown_remaining("reddit")
    assert 3495 <= rem <= 3500


def test_start_cooldown_logs_event(rdb, day):
    budget.start_cooldown("main", "reddit")
    events = rdb.lrange(f"cooldown_events:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1
    assert events[0].endswith(" reddit")            # "<epoch> <site>"
    assert rdb.get("cooldown:main") is not None
    assert rdb.ttl(f"cooldown_events:{time.strftime('%Y-%m-%d')}") > 0  # self-prunes


def test_start_cooldown_is_idempotent(rdb, day):
    budget.start_cooldown("main", "reddit")
    first = rdb.get("cooldown:main")
    budget.start_cooldown("main", "youtube")        # already cooling down -> no-op
    events = rdb.lrange(f"cooldown_events:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1                          # not double-logged
    assert rdb.get("cooldown:main") == first         # timer not reset


def test_lone_cooldown_uses_base_duration(rdb, day):
    budget.start_cooldown("main", "reddit")
    assert rdb.get("cooldown_secs:main") == str(budget.COOLDOWN_LADDER[0])   # 1h base


def test_clustered_cooldowns_escalate(rdb, day):
    now = time.time()
    durations = []
    for i in range(3):                              # three re-binges within the window
        rdb.delete("cooldown:main")                 # let a fresh cooldown start each time
        budget.start_cooldown("main", "youtube", now=now + i)
        durations.append(int(rdb.get("cooldown_secs:main")))
    assert durations == budget.COOLDOWN_LADDER[:3]  # 1h -> 1.5h -> 2h


def test_cooldown_escalation_caps(rdb, day):
    now = time.time()
    for i in range(6):                              # more re-binges than the ladder is long
        rdb.delete("cooldown:main")
        budget.start_cooldown("main", "youtube", now=now + i)
    assert int(rdb.get("cooldown_secs:main")) == budget.COOLDOWN_LADDER[-1]  # capped


def test_spread_out_cooldowns_stay_at_base(rdb, day):
    now = time.time()
    budget.start_cooldown("main", "reddit", now=now - 5 * 3600)   # 5h ago, outside window
    rdb.delete("cooldown:main")
    budget.start_cooldown("main", "reddit", now=now)
    assert int(rdb.get("cooldown_secs:main")) == budget.COOLDOWN_LADDER[0]   # no clustering


def test_recent_cooldown_count_window(rdb, day):
    now = time.time()
    rdb.rpush(f"cooldown_events:{time.strftime('%Y-%m-%d', time.localtime(now))}",
              f"{now - 3600:.0f} reddit",           # 1h ago -> inside window
              f"{now - 5 * 3600:.0f} reddit")       # 5h ago -> outside window
    assert budget.recent_cooldown_count(now) == 1


def test_escalated_cooldown_counts_down_full_duration(rdb, day):
    # A 2h escalated cooldown that started 30m ago still has ~90m left (not ~30m).
    rdb.set("cooldown:main", time.time() - 1800)
    rdb.set("cooldown_secs:main", 7200)
    rem = budget.get_cooldown_remaining("reddit")
    assert 5395 <= rem <= 5400


def test_heartbeat_full_drain_logs_cooldown_event(client, rdb, day, session):
    session("youtube", last_gap=15)
    rdb.set("spent:main", 890)
    hb(client, "youtube")
    events = rdb.lrange(f"cooldown_events:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1
    assert events[0].endswith(" youtube")


def test_cooldown_expiry_in_day_restores_budget(rdb, day):
    rdb.set("spent:main", 900)
    rdb.set("cooldown:main", time.time() - budget.COOLDOWN_SECONDS - 5)
    assert budget.get_cooldown_remaining("reddit") == 0
    assert rdb.get("cooldown:main") is None
    assert rdb.get("spent:main") is None          # budget restored


def test_cooldown_expiry_at_night_does_not_restore(rdb, night):
    rdb.set("spent:main", 900)
    rdb.set("cooldown:main", time.time() - budget.COOLDOWN_SECONDS - 5)
    assert budget.get_cooldown_remaining("reddit") == 0
    assert rdb.get("spent:main") == "900"         # no fresh night buffer


# ---------- heartbeat ----------

def hb(client, site="reddit"):
    return client.post(f"/heartbeat?site={site}")


def test_heartbeat_charges_gap(client, rdb, day, session):
    session("reddit", last_gap=15)
    resp = hb(client)
    assert resp.status_code == 200
    assert 14 <= float(rdb.get("spent:main")) <= 16
    assert 584 <= resp.get_json()["remaining"] <= 586


def test_heartbeat_ignores_large_gap(client, rdb, day, session):
    session("reddit", last_gap=budget.HEARTBEAT_MAX_GAP + 30)
    assert hb(client).status_code == 200
    assert rdb.get("spent:main") is None          # away time is free


def test_heartbeat_without_session_is_blocked(client, rdb, day):
    assert hb(client).status_code == 403


def test_heartbeat_site_cap_blocks_without_cooldown(client, rdb, day, session):
    session("reddit", last_gap=15)
    rdb.set("spent:main", 595)                    # +15 crosses reddit's 600
    assert hb(client).status_code == 403
    assert rdb.get("cooldown:main") is None       # bucket not drained: no wall
    assert rdb.get("active_token:reddit") is None # but this session is over


def test_heartbeat_full_drain_starts_cooldown(client, rdb, day, session):
    session("youtube", last_gap=15)
    rdb.set("spent:main", 890)                    # +15 crosses the 900 wall
    assert hb(client, "youtube").status_code == 403
    assert rdb.get("cooldown:main") is not None


def test_heartbeat_night_buffer_blocks_without_cooldown(client, rdb, night, session):
    session("reddit", last_gap=15)
    rdb.set("night_spent:main", 290)              # +15 crosses the 300 night buffer
    assert hb(client).status_code == 403
    assert rdb.get("cooldown:main") is None       # night never starts a cooldown


def test_heartbeat_night_charges_night_counter_not_day(client, rdb, night, session):
    session("reddit", last_gap=15)
    rdb.set("spent:main", 500)                     # day bucket untouched by night use
    hb(client)
    assert 14 <= budget.night_spent("main") <= 16  # night buffer charged
    assert rdb.get("spent:main") == "500"          # day counter left alone


def test_study_heartbeat_logs_study_time(client, rdb, day, session):
    session("youtube", mode="study")
    rdb.set("last_study_beat", time.time() - 12)
    hb(client, "youtube")
    logged = float(rdb.get(f"study_usage:{time.strftime('%Y-%m-%d')}"))
    assert 11 <= logged <= 13
    assert rdb.get("spent:main") is None            # measured, never charged
    assert rdb.ttl(f"study_usage:{time.strftime('%Y-%m-%d')}") > 0  # self-pruning


def test_study_heartbeat_ignores_large_gap(client, rdb, day, session):
    session("youtube", mode="study")
    rdb.set("last_study_beat", time.time() - 300)   # away longer than HEARTBEAT_MAX_GAP
    hb(client, "youtube")
    assert rdb.get(f"study_usage:{time.strftime('%Y-%m-%d')}") is None


def test_study_session_is_never_charged(client, rdb, day, session):
    session("youtube", mode="study", last_gap=15)
    resp = hb(client, "youtube")
    assert resp.get_json()["status"] == "study"
    assert rdb.get("spent:main") is None


def test_heartbeat_records_usage_history(client, rdb, day, session):
    session("reddit", last_gap=15)
    hb(client)
    today = time.strftime("%Y-%m-%d")
    assert 14 <= float(rdb.get(f"usage:{today}:reddit")) <= 16
    assert rdb.ttl(f"usage:{today}:reddit") > 0   # self-pruning
    assert rdb.get("last_charge") is not None


# ---------- daily reset ----------

def test_daily_reset_clears_state_but_keeps_history(rdb, day, session):
    session("reddit")
    rdb.set("spent:main", 500)
    rdb.set("night_spent:main", 120)
    rdb.set("cooldown:main", time.time())
    rdb.set("cooldown_secs:main", 7200)
    rdb.set("refilled_through:main", time.time())
    rdb.set("usage:2026-07-01:reddit", 480)
    budget.daily_reset()
    for key in ("spent:main", "night_spent:main", "cooldown:main", "cooldown_secs:main",
                "last_heartbeat:main", "refilled_through:main", "active_token:reddit"):
        assert rdb.get(key) is None, key
    assert rdb.get("usage:2026-07-01:reddit") == "480"   # history survives
