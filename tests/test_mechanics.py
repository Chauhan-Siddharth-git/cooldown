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


def test_refill_runs_during_winddown(rdb, winddown):
    # Wind-down regenerates too (night does not): the ramping cap — not a frozen bucket —
    # is what winds you down, so spent refills at the normal rate up toward the shrinking
    # ceiling. 5 min past grace credits the usual amount; get_remaining_budget then bounds
    # the result by the (time-proportional) wind-down cap.
    rdb.set("spent:main", 600)
    rdb.set("last_heartbeat:main", time.time() - budget.REGEN_DELAY - 300)
    assert abs(budget.get_spent("reddit") - (600 - 300 * RATE)) < 2


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


# ---------- soft pauses + cluster brake ----------

def test_soft_pause_is_logged(client, rdb, day, session):
    session("reddit", last_gap=15)
    rdb.set("spent:main", 595)                    # +15 maxes reddit's 600, bucket has room
    hb(client)
    events = rdb.lrange(f"soft_pauses:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1
    assert events[0].endswith(" reddit")
    assert rdb.get("cooldown:main") is None        # still no hard cooldown

def test_full_drain_does_not_log_soft_pause(client, rdb, day, session):
    session("youtube", last_gap=15)
    rdb.set("spent:main", 890)                     # +15 drains the whole 900 bucket
    hb(client, "youtube")
    assert rdb.get(f"soft_pauses:{time.strftime('%Y-%m-%d')}") is None
    assert rdb.get("cooldown:main") is not None

def _sp(rdb, now, site, ago):
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    rdb.rpush(f"soft_pauses:{day}", f"{now - ago:.0f} {site}")

def test_recent_soft_pause_count_is_per_site_and_windowed(rdb):
    now = time.time()
    _sp(rdb, now, "reddit", 600)     # 10 min ago  -> in
    _sp(rdb, now, "reddit", 3600)    # 1h ago      -> in
    _sp(rdb, now, "reddit", 9000)    # 2.5h ago    -> out of window
    _sp(rdb, now, "news", 300)       # different site
    assert budget.recent_soft_pause_count("reddit", now) == 2
    assert budget.recent_soft_pause_count("news", now) == 1

def test_cluster_cooldown_fires_on_third_not_second(rdb):
    now = time.time()
    _sp(rdb, now, "reddit", 1800)
    _sp(rdb, now, "reddit", 600)
    assert budget.maybe_cluster_cooldown("reddit", now) is False   # only 2 in the window
    assert budget.get_soft_cd_remaining("reddit") == 0
    _sp(rdb, now, "reddit", 0)                                     # the 3rd re-max
    assert budget.maybe_cluster_cooldown("reddit", now) is True
    assert 0 < budget.get_soft_cd_remaining("reddit") <= budget.CLUSTER_COOLDOWN_SECONDS

def test_cluster_ignores_stale_cluster(rdb):
    now = time.time()
    for ago in (9000, 8800, 8600):   # three, but all older than the 2h window
        _sp(rdb, now, "reddit", ago)
    _sp(rdb, now, "reddit", 0)        # one fresh -> only 1 counts in window
    assert budget.maybe_cluster_cooldown("reddit", now) is False


# ---------- reflection prompt: why you reached, and whether naming it helped ----------

def test_log_reflection_ignores_junk(rdb):
    budget.log_reflection("tired", "pass")
    budget.log_reflection("nonsense", "pass")      # not a real trigger
    budget.log_reflection("tired", "sideways")     # not a real outcome
    events = rdb.lrange(f"reflect:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1
    assert events[0].endswith(" tired pass")

def test_reflection_summary_counts_and_rate(rdb):
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    for t, a in [("tired","pass"),("tired","pass"),("tired","enter"),
                 ("bored","enter"),("bored","enter"),("stressed","pass")]:
        rdb.rpush(f"reflect:{day}", f"{now:.0f} {t} {a}")
    w = budget.reflection_summary()
    assert w["total"] == 6 and w["passes"] == 3 and w["rate"] == 50
    assert w["rows"][0]["key"] == "tired"          # most frequent first
    assert w["rows"][0]["n"] == 3 and w["rows"][0]["passed"] == 2

def test_reflection_summary_window_excludes_old(rdb):
    now = time.time()
    old_day = time.strftime("%Y-%m-%d", time.localtime(now - 40 * 86400))
    rdb.rpush(f"reflect:{old_day}", f"{now - 40*86400:.0f} tired pass")
    assert budget.reflection_summary(days=30)["total"] == 0

def test_enter_records_the_trigger_you_pushed_past(client, rdb, day):
    client.post("/enter?site=reddit", data={"trigger": "bored"})
    events = rdb.lrange(f"reflect:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1 and events[0].endswith(" bored enter")

def test_reflect_endpoint_records_a_pass(client, rdb):
    assert client.post("/reflect", data={"trigger": "habit"}).status_code == 200
    events = rdb.lrange(f"reflect:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert events[0].endswith(" habit pass")

def test_enter_without_a_trigger_records_nothing(client, rdb, day):
    client.post("/enter?site=reddit")               # entered without using the prompt
    assert rdb.lrange(f"reflect:{time.strftime('%Y-%m-%d')}", 0, -1) == []


# ---------- the reflection prompt resists habituation ----------

def test_reflect_never_on_first_entry_of_the_day(rdb):
    show, _ = budget.reflect_decision()               # no entries logged yet
    assert show is False

def test_reflect_appears_on_later_entries(rdb):
    day = time.strftime("%Y-%m-%d")
    seen = set()
    for n in range(1, 40):                            # across many entry counts
        rdb.set(f"entries:{day}", n)
        seen.add(budget.reflect_decision()[0])
    assert seen == {True, False}                      # unpredictable, not always-on

def test_reflect_decision_is_stable_across_reloads(rdb):
    # Reloading the gate must not re-roll the prompt away.
    rdb.set(f"entries:{time.strftime('%Y-%m-%d')}", 3)
    assert len({budget.reflect_decision()[0] for _ in range(10)}) == 1

def test_reflect_question_rotates(rdb):
    day0 = time.time()
    qs = {budget.reflect_decision(now=day0 + d * 86400)[1] for d in range(40)}
    assert len(qs) > 1                                # wording varies, no fixed script
    assert qs <= set(budget.REFLECT_QUESTIONS)

def test_entering_counts_toward_the_days_entries(client, rdb, day):
    key = f"entries:{time.strftime('%Y-%m-%d')}"
    assert rdb.get(key) is None
    client.post("/enter?site=reddit")
    assert rdb.get(key) == "1"
    assert rdb.ttl(key) > 0                           # self-prunes
