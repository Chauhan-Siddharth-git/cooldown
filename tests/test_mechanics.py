"""The budget state machine: shared bucket, passive refill + grace window,
cooldown lifecycle, heartbeat charging/blocking, daily reset."""
import time

import app as budget

POOL = budget.pool_max_budget("main")   # seconds; derived, never hardcoded
SLICE = budget.SITES["reddit"]["budget_seconds"]   # a per-site cap SMALLER than the pool,
# which is what makes the soft-pause path reachable at all. Derived for the same reason as
# POOL: the 2026-09-16 budget experiment changed both numbers, and a test that hardcodes a
# config value reports a deliberate change as a regression.
RATE = POOL / budget.REFILL_FULL_SECONDS   # seconds of budget restored per idle second


# ---------- shared bucket ----------

def test_shared_bucket_per_site_caps(rdb, day):
    """Spend exactly the small slice: the small-cap sites are done, the big-cap one is not.
    Written against the caps rather than against the numbers they happened to be, because
    the 2026-09-16 experiment changed every one of them."""
    big = budget.SITES["youtube"]["budget_seconds"]
    assert SLICE < big, "no site has a cap below the pool -- the soft-pause path is gone"
    rdb.set("spent:main", SLICE)
    assert budget.get_remaining_budget("reddit") == 0
    assert budget.get_remaining_budget("spotify") == 0
    assert round(budget.get_remaining_budget("youtube")) == big - SLICE


def test_pool_max_budget_is_largest_cap(monkeypatch):
    """Reads the pool size from config rather than hardcoding it, so a deliberate change
    to the budget is not indistinguishable from a regression.

    The second half matters more than the first. Until 2026-09-16 YouTube's cap (15 min)
    was strictly the largest, so `== 900` happened to prove max() was being used. Every
    cap is now 10, which makes that assertion true of max(), min(), first() or any other
    selector -- vacuous in exactly the way rule 10 warns about. So the property is tested
    against a pool where the caps actually differ."""
    caps = [s["budget_seconds"] for k, s in budget.SITES.items() if budget.pool(k) == "main"]
    assert budget.pool_max_budget("main") == max(caps)

    bigger = {k: (v | {"budget_seconds": 99 * 60} if k == "puzzmo" else v)
              for k, v in budget.SITES.items()}
    monkeypatch.setattr(budget, "SITES", bigger)
    assert budget.pool_max_budget("main") == 99 * 60, "not selecting the largest"


def test_news_shares_main_bucket(rdb, day):
    assert budget.pool("news") == "main"
    assert "news" in budget.pool_sites("main")       # switching to news is NOT an escape hatch
    rdb.set("spent:main", SLICE)                      # shared spend
    assert budget.get_remaining_budget("news") == 0   # news slice gone with the rest
    big = budget.SITES["youtube"]["budget_seconds"]
    assert round(budget.get_remaining_budget("youtube")) == big - SLICE  # bigger cap has room


def test_puzzmo_shares_the_bucket_at_the_small_slice(rdb, day):
    assert budget.SITES["puzzmo"]["budget_seconds"] == SLICE
    assert budget.pool("puzzmo") == "main"           # same shared bucket
    rdb.set("spent:main", SLICE - 50)
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
    rdb.set("spent:main", POOL)
    rdb.set("cooldown:main", time.time())
    rdb.set("last_heartbeat:main", time.time() - 3600)
    assert budget.get_spent("reddit") == POOL


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


# ---------- soft-pause cluster brake ----------

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


def test_escalated_cooldown_counts_down_full_duration(rdb, day):
    # A 2h escalated cooldown that started 30m ago still has ~90m left (not ~30m).
    rdb.set("cooldown:main", time.time() - 1800)
    rdb.set("cooldown_secs:main", 7200)
    rem = budget.get_cooldown_remaining("reddit")
    assert 5395 <= rem <= 5400


def test_heartbeat_full_drain_logs_cooldown_event(client, rdb, day, session):
    session("youtube", last_gap=15)
    rdb.set("spent:main", POOL - 10)
    hb(client, "youtube")
    events = rdb.lrange(f"cooldown_events:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1
    assert events[0].endswith(" youtube")


def test_cooldown_expiry_in_day_restores_budget(rdb, day):
    rdb.set("spent:main", POOL)
    rdb.set("cooldown:main", time.time() - budget.COOLDOWN_SECONDS - 5)
    assert budget.get_cooldown_remaining("reddit") == 0
    assert rdb.get("cooldown:main") is None
    assert rdb.get("spent:main") is None          # budget restored


def test_cooldown_expiry_at_night_does_not_restore(rdb, night):
    rdb.set("spent:main", POOL)
    rdb.set("cooldown:main", time.time() - budget.COOLDOWN_SECONDS - 5)
    assert budget.get_cooldown_remaining("reddit") == 0
    assert rdb.get("spent:main") == str(POOL)         # no fresh night buffer


# ---------- heartbeat ----------

def hb(client, site="reddit"):
    return client.post(f"/heartbeat?site={site}")


def test_heartbeat_charges_gap(client, rdb, day, session):
    session("reddit", last_gap=15)
    resp = hb(client)
    assert resp.status_code == 200
    assert 14 <= float(rdb.get("spent:main")) <= 16
    assert SLICE - 16 <= resp.get_json()["remaining"] <= SLICE - 14


def test_heartbeat_caps_a_large_gap_rather_than_ignoring_it(client, rdb, day, session):
    """Renamed from test_heartbeat_ignores_large_gap, which asserted `spent is None` --
    "away time is free". That was the design intent and it was the vulnerability: a
    client pacing its pings just outside the window was away, free, and never logged
    out. Away now costs one cap, which is the smallest charge that makes pacing pointless."""
    session("reddit", last_gap=budget.HEARTBEAT_MAX_GAP + 30)
    assert hb(client).status_code == 200
    spent = float(rdb.get("spent:main"))
    assert abs(spent - budget.HEARTBEAT_MAX_GAP) < 1, spent


def test_heartbeat_without_session_is_blocked(client, rdb, day):
    assert hb(client).status_code == 403


def test_heartbeat_site_cap_blocks_without_cooldown(client, rdb, day, session):
    session("reddit", last_gap=15)
    rdb.set("spent:main", SLICE - 5)                    # +15 crosses reddit's slice
    assert hb(client).status_code == 403
    assert rdb.get("cooldown:main") is None       # bucket not drained: no wall
    assert rdb.get("active_token:reddit") is None # but this session is over


def test_soft_pause_is_logged(client, rdb, day, session):
    session("reddit", last_gap=15)
    rdb.set("spent:main", SLICE - 5)                    # +15 maxes reddit's slice, bucket has room
    hb(client)
    events = rdb.lrange(f"soft_pauses:{time.strftime('%Y-%m-%d')}", 0, -1)
    assert len(events) == 1
    assert events[0].endswith(" reddit")          # "<epoch> reddit"
    assert rdb.get("cooldown:main") is None        # still no hard cooldown


def test_full_drain_does_not_log_soft_pause(client, rdb, day, session):
    session("youtube", last_gap=15)
    rdb.set("spent:main", POOL - 10)                     # +15 drains the whole 900 bucket
    hb(client, "youtube")
    assert rdb.get(f"soft_pauses:{time.strftime('%Y-%m-%d')}") is None  # that's a cooldown, not a soft pause
    assert rdb.get("cooldown:main") is not None


def test_heartbeat_full_drain_starts_cooldown(client, rdb, day, session):
    session("youtube", last_gap=15)
    rdb.set("spent:main", POOL - 10)                    # +15 crosses the 900 wall
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


def test_a_paced_ping_cannot_run_a_session_for_free(client, rdb, day, session):
    """D1 / the pacing hole. A client that pings just outside HEARTBEAT_MAX_GAP kept its
    session alive and was charged nothing, indefinitely: the gap was discarded rather
    than capped, and last_heartbeat advanced regardless of whether anything was charged.

    Proven live before the fix -- 4 pings 31s apart over 124s, spent=0.0, HTTP 200
    throughout. Charging the cap makes paced pinging strictly worse than honest pinging,
    which is the property that matters.
    """
    session("reddit", last_gap=0)
    for _ in range(3):                          # three genuine 31s gaps
        rdb.set("last_heartbeat:main", time.time() - 31)
        assert hb(client).status_code == 200
    spent = float(rdb.get("spent:main") or 0)
    assert spent >= 3 * budget.HEARTBEAT_MAX_GAP - 1, f"paced pinging charged only {spent}s"


def test_a_backwards_clock_cannot_refund_spent_time(client, rdb, day, session):
    """Found while writing the test above. `gap <= HEARTBEAT_MAX_GAP` is also true for a
    NEGATIVE gap, so a last_heartbeat in the future subtracts from spent. Not exotic on
    this hardware: the Pi has no RTC, its clock jumps at every boot, and F21 was caused
    by exactly that. Reproduced at -30.96s before the fix."""
    session("reddit", last_gap=0)
    rdb.set("spent:main", 100)
    rdb.set("last_heartbeat:main", time.time() + 60)   # clock stepped backwards
    assert hb(client).status_code == 200
    assert float(rdb.get("spent:main")) >= 100, "a future last_heartbeat refunded spent time"


def test_charged_gap_caps_and_floors(client):
    """Unit-level, because the two protections are redundant at the route level and the
    integration test therefore cannot tell them apart: removing the floor still yields a
    negative number that `if gap > 0` discards. Mutation testing showed that -- the
    floor could be deleted with every route test still green. Tested here directly so
    each half can fail on its own."""
    g = budget.charged_gap
    now = 1_000_000.0
    assert g(now, now - 10) == 10                              # ordinary ping, unchanged
    assert g(now, now - 300) == budget.HEARTBEAT_MAX_GAP       # capped, not discarded
    assert g(now, now + 60) == 0                               # future timestamp floored
    assert g(now, now) == 0


def test_the_two_timing_constants_cannot_drift_apart(client):
    """The hole existed because SESSION_IDLE_TTL (120) exceeded HEARTBEAT_MAX_GAP (30),
    leaving a band where a ping refreshed the session but bought free time. Capping
    closes it for any ratio, but the relationship is still the thing a future edit could
    break, so it is asserted rather than remembered."""
    assert budget.SESSION_IDLE_TTL >= budget.HEARTBEAT_MAX_GAP


# --- travelling: the curfew follows you, the books do not (yet) -----------------------

from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _Z

LA, NY = "America/Los_Angeles", "America/New_York"


def _epoch(zone, y, m, d, hh, mm=0):
    return _dt(y, m, d, hh, mm, tzinfo=_Z(zone)).timestamp()


def test_zone_is_adopted_only_after_it_holds(rdb):
    t0 = _epoch(NY, 2026, 9, 15, 12)
    assert budget.note_client_tz(LA, t0) is None            # first sighting: pending only
    assert budget.policy_tz() == budget.HOME_TZ
    # Still short of the window.
    assert budget.note_client_tz(LA, t0 + budget.TZ_ADOPT_AFTER - 60) is None
    assert budget.policy_tz() == budget.HOME_TZ
    # And past it.
    assert budget.note_client_tz(LA, t0 + budget.TZ_ADOPT_AFTER + 1) == LA
    assert budget.policy_tz() == LA


def test_flapping_between_zones_never_accumulates(rdb):
    """The impulsive case: a zone that keeps changing must never age into an adoption."""
    t0 = _epoch(NY, 2026, 9, 15, 12)
    for i in range(12):
        budget.note_client_tz(LA if i % 2 else "Asia/Tokyo", t0 + i * 3600)
    assert budget.policy_tz() == budget.HOME_TZ


def test_a_forged_or_unknown_zone_is_dropped(rdb):
    t0 = _epoch(NY, 2026, 9, 15, 12)
    for bad in ("", "Mars/Olympus", "../../etc/passwd", "UTC+25", None):
        assert budget.note_client_tz(bad, t0) is None
    assert budget.policy_tz() == budget.HOME_TZ
    assert not rdb.get("tz_pending")


def test_one_adoption_per_cooldown(rdb):
    t0 = _epoch(NY, 2026, 9, 15, 12)
    budget.note_client_tz(LA, t0)
    assert budget.note_client_tz(LA, t0 + budget.TZ_ADOPT_AFTER + 1) == LA
    t1 = t0 + budget.TZ_ADOPT_AFTER + 2
    budget.note_client_tz("Asia/Tokyo", t1)
    assert budget.note_client_tz("Asia/Tokyo", t1 + budget.TZ_ADOPT_AFTER + 1) is None
    assert budget.policy_tz() == LA


def test_curfew_follows_the_adopted_zone(rdb, monkeypatch):
    """7pm in California is 10pm in Boston. The box's clock says wind-down; your body
    says early evening, and your body is the one going to bed."""
    when = _epoch(LA, 2026, 9, 15, 19, 30)                   # 19:30 PDT == 22:30 EDT
    monkeypatch.setattr(budget, "HOME_TZ", NY)
    budget._tz_bust()
    assert budget.phase(when) == "winddown"                  # the bug, as reported
    rdb.set("tz_policy", LA); budget._tz_bust()
    assert budget.phase(when) == "day"                       # ...and fixed
    assert not budget.in_night(when)


def test_adopting_a_zone_does_not_hand_out_a_fresh_budget(rdb, monkeypatch):
    """The trap. reset_day() keys off the local hour and catch_up_reset() answers a
    disagreement by DELETING spent:{pool}. If the accounting zone moved with the policy
    zone, flying west would pay out a full budget on landing and another at 07:00 local.
    """
    monkeypatch.setattr(budget, "HOME_TZ", NY)
    budget._tz_bust()
    when = _epoch(NY, 2026, 9, 15, 8)                        # 08:00 EDT == 05:00 PDT
    rdb.set("last_reset", budget.reset_day(when))
    rdb.set("spent:main", POOL)

    rdb.set("tz_policy", LA); budget._tz_bust()              # adopted mid-morning
    assert budget.accounting_tz() == NY, "the books must not move on adoption"
    assert budget.reset_day(when) == rdb.get("last_reset")
    assert budget.catch_up_reset(when) is False, "a reset fired on a timezone change"
    assert float(rdb.get("spent:main")) == POOL


def test_the_books_move_once_at_the_next_reset_and_do_not_re_fire(rdb, monkeypatch):
    monkeypatch.setattr(budget, "HOME_TZ", NY)
    rdb.set("tz_policy", LA); budget._tz_bust()
    when = _epoch(LA, 2026, 9, 16, 7, 5)                     # just past 07:00 PDT
    budget.daily_reset(when)
    assert budget.accounting_tz() == LA                      # moved, exactly once
    assert rdb.get("last_reset") == budget.reset_day(when)   # re-stamped in the NEW zone
    assert budget.catch_up_reset(when) is False              # so nothing fires twice


def test_a_reboot_is_recorded_at_the_time_it_HAPPENED(rdb, monkeypatch):
    """Not at the time it was noticed. boot_watch() only runs when /health renders, so
    the two can be hours apart: a 04:00 kernel-update reboot went unnoticed until 09:45
    and the banner reported 09:45 -- five and three quarter hours late, and inside the
    window the owner was awake and using the machine.

    That is the worst error this feature can make. Its job is to let you say "yes, I
    unplugged it" or "no, I didn't", and a wrong timestamp makes an explainable reboot
    look unexplainable and an unexplainable one look like something you might have done.
    """
    import app as b
    real_boot = 1_789_536_012.0                   # 04:00-ish
    noticed_at = real_boot + 5.75 * 3600          # ...noticed at 09:45
    monkeypatch.setattr(b, "_boot_time", lambda: real_boot)
    monkeypatch.setattr(b, "_first_line", lambda p: "boot-id-AAAA")
    monkeypatch.setattr(b, "send_alert", lambda *a, **k: True)
    monkeypatch.setattr(b.time, "time", lambda: noticed_at)

    rdb.set("last_boot_id", "boot-id-PREVIOUS")   # so this is not the first-ever run
    b.boot_watch()

    assert float(rdb.get("unacked_boot")) == real_boot, "banner shows when it was noticed"
    assert float(rdb.lrange("boot_events", -1, -1)[0]) == real_boot, "history shows the same"


def test_an_unreadable_proc_stat_still_records_the_reboot(rdb, monkeypatch):
    """Fall back to now() rather than losing the event: a late timestamp is bad, no
    tamper-evidence at all is worse."""
    import app as b
    monkeypatch.setattr(b, "_boot_time", lambda: None)
    monkeypatch.setattr(b, "_first_line", lambda p: "boot-id-BBBB")
    monkeypatch.setattr(b, "send_alert", lambda *a, **k: True)
    rdb.set("last_boot_id", "boot-id-PREVIOUS")
    b.boot_watch()
    assert rdb.get("unacked_boot"), "the reboot was dropped when /proc/stat was unreadable"


def test_the_first_ever_run_still_does_not_cry_wolf(rdb, monkeypatch):
    import app as b
    monkeypatch.setattr(b, "_boot_time", lambda: 1_789_536_012.0)
    monkeypatch.setattr(b, "_first_line", lambda p: "boot-id-CCCC")
    monkeypatch.setattr(b, "send_alert", lambda *a, **k: True)
    rdb.delete("last_boot_id")
    b.boot_watch()
    assert not rdb.get("unacked_boot")
    assert rdb.get("last_boot_id") == "boot-id-CCCC"


# --- planned reboots -------------------------------------------------------------------

def _sched_file(tmp_path, usec, mode="reboot"):
    p = tmp_path / "scheduled"
    p.write_text(f"USEC={int(usec * 1_000_000)}\nWARN_WALL=1\nMODE={mode}\n")
    return str(p)


def test_a_scheduled_reboot_is_announced_once_before_it_happens(rdb, tmp_path, monkeypatch):
    """The whole point: the box says a reboot is coming BEFORE it happens, off the box.
    ntfy timestamps server-side, so an announcement cannot be backdated by someone who
    later reads ALERT_URL off the card -- the ordering is the evidence, not the secrecy.
    """
    import app as b
    when = time.time() + 2400
    monkeypatch.setattr(b, "REBOOT_SCHEDULE_FILE", _sched_file(tmp_path, when))
    sent = []
    monkeypatch.setattr(b, "send_alert", lambda t: sent.append(t) or True)

    assert b.announce_planned_reboot() == f"{when:.0f}"
    assert len(sent) == 1 and "PLANNED reboot" in sent[0]
    assert rdb.get("planned_reboot") == f"{when:.0f}"
    # Polled every 5 minutes, so re-announcing the same reboot would send ~9 duplicates.
    assert b.announce_planned_reboot() is None
    assert len(sent) == 1


def test_nothing_scheduled_announces_nothing(rdb, tmp_path, monkeypatch):
    import app as b
    sent = []
    monkeypatch.setattr(b, "send_alert", lambda t: sent.append(t) or True)
    monkeypatch.setattr(b, "REBOOT_SCHEDULE_FILE", str(tmp_path / "does-not-exist"))
    assert b.announce_planned_reboot() is None
    # A scheduled POWEROFF is not a reboot and must not be announced as one.
    monkeypatch.setattr(b, "REBOOT_SCHEDULE_FILE",
                        _sched_file(tmp_path, time.time() + 600, mode="poweroff"))
    assert b.announce_planned_reboot() is None
    assert sent == []


def test_an_announced_reboot_raises_no_banner_and_no_alert(rdb, monkeypatch):
    """The half that restores the signal. Before this, every kernel update sent
    'unexplained reboot' -- identical in shape to what a theft would send -- so the
    channel was noise and the one message that mattered would have read the same."""
    import app as b
    boot = time.time()
    rdb.set("planned_reboot", f"{boot - 12:.0f}")     # announced, boot 12s later
    rdb.set("last_boot_id", "PREVIOUS")
    monkeypatch.setattr(b, "_boot_time", lambda: boot)
    monkeypatch.setattr(b, "_first_line", lambda p: "NEW-BOOT-ID")
    sent = []
    monkeypatch.setattr(b, "send_alert", lambda t: sent.append(t) or True)

    b.boot_watch()
    assert not rdb.get("unacked_boot"), "a planned reboot raised the banner"
    assert sent == [], "a planned reboot sent an alert"
    assert rdb.lrange("boot_events", -1, -1)[0].endswith(" planned"), "not recorded as planned"
    assert not rdb.get("planned_reboot"), "the announcement must excuse exactly one boot"


def test_an_unannounced_reboot_still_alarms(rdb, monkeypatch):
    """Guards the guard. If this passed too, the feature would be a mute button."""
    import app as b
    boot = time.time()
    rdb.set("last_boot_id", "PREVIOUS")
    monkeypatch.setattr(b, "_boot_time", lambda: boot)
    monkeypatch.setattr(b, "_first_line", lambda p: "NEW-BOOT-ID")
    sent = []
    monkeypatch.setattr(b, "send_alert", lambda t: sent.append(t) or True)

    b.boot_watch()
    assert rdb.get("unacked_boot") == f"{boot:.0f}"
    assert len(sent) == 1 and "UNPLANNED" in sent[0]
    assert not rdb.lrange("boot_events", -1, -1)[0].endswith(" planned")


def test_an_announcement_does_not_excuse_a_distant_reboot(rdb, monkeypatch):
    """Bounded on both sides. A stale plan must not cover a reboot hours later, and must
    not cover one that happened BEFORE the announcement was made."""
    import app as b
    base = time.time()
    rdb.set("planned_reboot", f"{base:.0f}")
    assert b.planned_reboot_covers(base + 12)                       # the real one
    assert b.planned_reboot_covers(base + b.PLANNED_BOOT_GRACE - 1)
    assert not b.planned_reboot_covers(base + b.PLANNED_BOOT_GRACE + 60)   # too late
    assert not b.planned_reboot_covers(base - 600)                         # before it
    rdb.delete("planned_reboot")
    assert not b.planned_reboot_covers(base)


def test_boot_history_still_reads_entries_written_before_the_tag(rdb):
    """Existing boot_events are bare epochs. Tagging must not make the old ones vanish
    from the history -- that would erase the record this feature exists to keep."""
    import app as b
    old, new = time.time() - 86400, time.time() - 60
    rdb.rpush("boot_events", f"{old:.0f}")
    rdb.rpush("boot_events", f"{new:.0f} planned")
    rows = b.boot_history()
    assert len(rows) == 2, rows
    assert rows[0]["planned"] is True and rows[1]["planned"] is False


# --- the alert channel itself ----------------------------------------------------------

def test_alert_state_separates_broken_from_merely_unproven(rdb, monkeypatch):
    """Two different problems. "Failing" is knowable now; "unproven" only means nothing
    has needed saying. Reporting the second as the first is how a check becomes noise,
    and reporting the first as the second would hide a dead channel."""
    import app as b
    monkeypatch.setattr(b, "ALERT_URL", "https://example.invalid/t")
    now = time.time()

    rdb.delete("alert_last")
    assert b.alert_state(now)["state"] == "never used"

    # Deliberately a minute off the day boundary. "%.0f" ROUNDS, so a timestamp built as
    # exactly now-3d can be stored up to half a second late, and flooring the difference
    # then yields 2 -- which failed in about a third of runs and looked like flakiness in
    # the code rather than arithmetic in the test.
    rdb.set("alert_last", f"{now - 3 * 86400 - 60:.0f} ok")
    s = b.alert_state(now)
    assert s["ok"] and s["state"] == "ok" and s["age_days"] == 3

    rdb.set("alert_last", f"{now - 12 * 86400 - 60:.0f} ok")
    s = b.alert_state(now)
    assert not s["ok"] and s["state"] == "unproven", s

    rdb.set("alert_last", f"{now - 60:.0f} http 500")
    s = b.alert_state(now)
    assert not s["ok"] and s["state"] == "failing" and "500" in s["detail"]

    # Unreadable must not read as healthy.
    rdb.set("alert_last", "garbage")
    assert not b.alert_state(now)["ok"]


def test_no_alert_url_is_reported_not_silently_fine(rdb, monkeypatch):
    """A box with no alert URL cannot tell you anything off-box. That is a configuration
    choice, but it must be visible rather than indistinguishable from a working one."""
    import app as b
    monkeypatch.setattr(b, "ALERT_URL", "")
    s = b.alert_state()
    assert s["configured"] is False and not s["ok"]


def test_the_probe_sends_weekly_and_does_not_buzz(rdb, monkeypatch):
    """A canary that woke the phone every week would be turned off, and a turned-off
    canary is worse than none: its silence still reads as health."""
    import app as b
    monkeypatch.setattr(b, "ALERT_URL", "https://example.invalid/t")
    sent = []
    monkeypatch.setattr(b, "send_alert", lambda t, priority=None: sent.append((t, priority)))
    now = time.time()

    assert b.alert_probe(now) is True
    assert len(sent) == 1 and sent[0][1] == "min", sent
    # Runs hourly; must not send hourly.
    assert b.alert_probe(now + 3600) is False
    assert b.alert_probe(now + 6 * 86400) is False
    assert b.alert_probe(now + 8 * 86400) is True
    assert len(sent) == 2


def _drain_alert_threads(timeout=3):
    """Wait for send_alert's worker to finish before the test ends.

    It runs on a daemon thread and its LAST act is writing alert_last. Letting that thread
    outlive the test means the write lands in whatever database the next test is using --
    which made test_alert_state_separates_broken_from_merely_unproven fail in roughly
    three runs out of eight, looking exactly like a bug in the code under test. Joining by
    thread name keeps this in the tests, where the problem is, rather than adding a handle
    to production code that only tests would use.
    """
    import threading
    for t in threading.enumerate():
        if t.name == "cooldown-alert":
            t.join(timeout)
            assert not t.is_alive(), "alert thread did not finish; it would pollute the next test"


def test_the_probe_goes_through_the_same_path_a_real_alert_uses(rdb, monkeypatch):
    """A probe that used a different URL, method or header would prove something adjacent
    to the thing that has to work. This asserts it reaches the real sender."""
    import app as b
    monkeypatch.setattr(b, "ALERT_URL", "https://example.invalid/t")
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["priority"] = req.headers.get("Priority")
        raise RuntimeError("stop here; the request shape is what matters")
    monkeypatch.setattr(b.urllib.request, "urlopen", fake_urlopen)
    rdb.delete("alert_probe_at")
    b.alert_probe()
    _drain_alert_threads()
    assert seen.get("url") == "https://example.invalid/t", seen
    assert seen.get("priority") == "min", seen


def test_a_real_alert_carries_no_priority_header(rdb, monkeypatch):
    """Only the probe is silent. An unplanned reboot must arrive at normal priority."""
    import app as b
    monkeypatch.setattr(b, "ALERT_URL", "https://example.invalid/t")
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["priority"] = req.headers.get("Priority")
        raise RuntimeError("stop")
    monkeypatch.setattr(b.urllib.request, "urlopen", fake_urlopen)
    b.send_alert("cooldown: UNPLANNED reboot")
    _drain_alert_threads()
    assert seen.get("priority") is None, seen


def test_some_slice_stays_smaller_than_the_pool(monkeypatch):
    """The soft pause exists only while at least one site's cap is BELOW the pool. If
    every cap equals pool_max_budget(), hitting a site's cap always drains the pool and
    every breather becomes a full hard cooldown across all five sites.

    Nothing asserted this, and it was nearly lost twice in one afternoon: first by cutting
    YouTube's cap alone (it IS the pool, so that levelled everything), then by the
    perfectly reasonable request to make every site a round 10:00. Both are invisible in
    a diff -- they look like a number changing.

    It is worth pinning because the path is load-bearing, not theoretical: 129 soft pauses
    over 51 days, 127 of them Reddit, ~2.5/day against ~1.9 hard cooldowns/day, and two
    days that week where it fired 4 and 5 times with no hard cooldown at all.
    """
    pool = budget.pool_max_budget("main")
    caps = {k: v["budget_seconds"] for k, v in budget.SITES.items() if budget.pool(k) == "main"}
    assert caps, "no sites in the main pool -- this test is checking nothing"
    assert min(caps.values()) < pool, (
        f"every cap equals the pool ({pool}s), so get_soft_cd_remaining() can never fire "
        f"and every site-cap hit becomes a 60-minute lockout of all of them: {caps}")

    # ...and the reachable state really does produce a soft pause rather than a cooldown.
    small = min(caps, key=lambda k: caps[k])
    assert budget.SITES[small]["budget_seconds"] < pool


# --- the dead-man heartbeat trace ------------------------------------------------------

def _dm_log(tmp_path, monkeypatch, lines):
    import app as b
    p = tmp_path / "deadman.log"
    p.write_text("".join(l + "\n" for l in lines))
    monkeypatch.setattr(b, "DEADMAN_LOG", str(p))
    return b


def test_a_steady_rhythm_is_alive(tmp_path, monkeypatch):
    now = time.time()
    b = _dm_log(tmp_path, monkeypatch, [f"{now - i * 300:.0f} ok" for i in range(36)])
    t = b.deadman_trace(now)
    assert t["count"] == 36 and not t["flat"] and t["age"] < 60


def test_twelve_quiet_minutes_is_a_flatline(tmp_path, monkeypatch):
    """Asserted against the threshold, not an exact age. The first version checked
    age >= 780 on a ping built as now-780 with %.0f -- which ROUNDS, so the stored ping
    could land half a second late and int() floor the age to 779. Flaky about half the
    time, and it was the same mistake made in the alert-channel tests a week earlier."""
    now = time.time()
    b = _dm_log(tmp_path, monkeypatch, [f"{now - 13 * 60 - i * 300:.0f} ok" for i in range(20)])
    t = b.deadman_trace(now)
    assert t["flat"], "13 minutes without a ping must read as a flatline"
    assert t["age"] > b.DEADMAN_FLATLINE_AFTER


def test_a_failed_ping_is_a_gap_not_a_beat(tmp_path, monkeypatch):
    """A ping that could not reach healthchecks.io did not happen as far as the far end is
    concerned, so the trace must not draw it -- otherwise the picture would claim a
    heartbeat the evidence does not have."""
    now = time.time()
    b = _dm_log(tmp_path, monkeypatch, [f"{now - 60:.0f} failed", f"{now - 360:.0f} failed",
                                        f"{now - 900:.0f} ok"])
    t = b.deadman_trace(now)
    assert t["count"] == 1 and t["flat"], t


def test_no_log_means_no_trace_rather_than_a_healthy_one(tmp_path, monkeypatch):
    import app as b
    monkeypatch.setattr(b, "DEADMAN_LOG", str(tmp_path / "absent.log"))
    assert b.deadman_trace() is None


def test_timer_jitter_does_not_draw_a_missed_beat(tmp_path, monkeypatch):
    """Why beats are drawn at true times instead of in five-minute buckets. The timer
    drifts by up to 30s; with fixed buckets, pings at 4:40 and 5:20 past the hour land in
    the same slot and leave the next one empty -- a missed beat that never happened. Every
    ping here is on time give or take 30s, so every one must become a beat."""
    now = time.time()
    jitter = [0, 25, -28, 15, -22, 29, -30, 10]
    pings = [f"{now - i * 300 + jitter[i % len(jitter)]:.0f} ok" for i in range(36)]
    b = _dm_log(tmp_path, monkeypatch, pings)
    t = b.deadman_trace(now)
    svg = str(b.deadman_svg(t, now))
    # One R peak per beat: count the spikes that reach full amplitude.
    peak_y = f"{64 * 0.64 - 64 * 0.52:.1f}"
    assert svg.count("," + peak_y) == t["count"] == 36, "a jittery on-time ping was dropped"


def test_the_trace_path_never_runs_backwards(tmp_path, monkeypatch):
    """Two pings closer together than one beat width would otherwise produce an x that
    goes backwards, and the line would scribble over itself."""
    import re
    now = time.time()
    b = _dm_log(tmp_path, monkeypatch, [f"{now - 60:.0f} ok", f"{now - 61:.0f} ok",
                                        f"{now - 62:.0f} ok", f"{now - 400:.0f} ok"])
    svg = str(b.deadman_svg(b.deadman_trace(now), now))
    d = re.search(r'd="M([^"]+)"', svg).group(1)
    xs = [float(p.split(",")[0]) for p in d.replace("L", " ").split()]
    assert xs == sorted(xs), "the path doubles back on itself"



def test_nothing_is_drawn_outside_the_frame(tmp_path, monkeypatch):
    """The newest ping is almost always within the last five minutes, so its beat sits
    against the right edge. Unclamped it drew past the frame on essentially every render.
    Monotonicity alone does not catch that -- the closing point can follow the overflow --
    so the bound is asserted directly."""
    import re
    now = time.time()
    b = _dm_log(tmp_path, monkeypatch, [f"{now - 5:.0f} ok", f"{now - 305:.0f} ok"])
    svg = str(b.deadman_svg(b.deadman_trace(now), now, width=600))
    d = re.search(r'd="M([^"]+)"', svg).group(1)
    xs = [float(p.split(",")[0]) for p in d.replace("L", " ").split()]
    assert max(xs) <= 600.0 and min(xs) >= 0.0, f"trace leaves the frame: {min(xs)}..{max(xs)}"


def test_the_meter_comparison_still_works_once_the_meter_is_old(rdb):
    """The shadow meter started 2026-08-03. The comparison built its hour list starting at
    the meter's FIRST hour and stopped at the first hour older than the window -- so once
    the meter was more than `days` old, the first hour it examined was already too old and
    it stopped having collected nothing. From about 2026-08-10 it returned zero hours, and
    the stats page answered that with "too early to draw conclusions, give it a few days":
    a permanent failure dressed as a temporary one, for six weeks.

    Every earlier test ran with a freshly started meter, which is the one case that works.
    """
    import app as b
    now = time.time()
    rdb.set("shadow_started", f"{now - 60 * 86400:.0f}")          # a long-running meter
    for i in range(2, 30):                                         # use inside the window
        t = time.localtime(now - i * 3600)
        d, h = time.strftime("%Y-%m-%d", t), time.strftime("%H", t)
        rdb.set(f"usage_hour:{d}:{h}:reddit", 600)
        rdb.set(f"shadow_hour:{d}:{h}:reddit", 660)
    c = b.shadow_comparison(days=7, now=now)
    hours = int(c["running"].split()[0])
    assert hours > 100, f"an old meter compared only {hours} hours -- the window was skipped"
    assert c["hb"] > 0 and c["sh"] > 0, c
    assert c["settled"], "a week of data must not read as too early to tell"
