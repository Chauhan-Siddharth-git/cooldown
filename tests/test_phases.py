"""Pure time helpers: in_night / phase / effective_cap / secs_until_hour / clock.

All of these take an explicit `now`, so they're tested un-patched with epochs
built for specific local wall-clock times.
"""
import app as budget
from conftest import local_epoch


def test_in_night_window():
    for h in (23, 0, 2, 6):
        assert budget.in_night(local_epoch(h)), f"{h}:00 should be night"
    for h in (7, 12, 20, 22):
        assert not budget.in_night(local_epoch(h)), f"{h}:00 should not be night"


def test_phase_boundaries():
    assert budget.phase(local_epoch(14)) == "day"
    assert budget.phase(local_epoch(21, 59)) == "day"       # just before the ramp
    assert budget.phase(local_epoch(22, 1)) == "winddown"   # inside the 1h ramp
    assert budget.phase(local_epoch(22, 59)) == "winddown"
    assert budget.phase(local_epoch(23)) == "night"
    assert budget.phase(local_epoch(3)) == "night"
    assert budget.phase(local_epoch(7)) == "day"            # curfew ends


def test_effective_cap_day():
    now = local_epoch(14)
    assert budget.effective_cap("reddit", now) == 600
    assert budget.effective_cap("youtube", now) == 900
    assert budget.effective_cap("spotify", now) == 600


def test_effective_cap_night_is_shared_buffer():
    now = local_epoch(2)
    for site in ("reddit", "youtube", "spotify"):
        assert budget.effective_cap(site, now) == budget.NIGHT_BUDGET_SECONDS


def test_effective_cap_winddown_ramps_linearly():
    mid = local_epoch(22, 30)   # halfway down the 1h ramp
    assert budget.effective_cap("reddit", mid) == 450     # (600+300)/2
    assert budget.effective_cap("youtube", mid) == 600    # (900+300)/2
    late = local_epoch(22, 57)  # 3 min out -> close to the night buffer
    assert 300 <= budget.effective_cap("reddit", late) <= 320


def test_winddown_cap_never_below_night_buffer():
    just_before_23 = local_epoch(22, 59)
    for site in ("reddit", "youtube", "spotify"):
        assert budget.effective_cap(site, just_before_23) >= budget.NIGHT_BUDGET_SECONDS


def test_secs_until_hour():
    assert budget.secs_until_hour(23, local_epoch(22)) == 3600
    assert budget.secs_until_hour(7, local_epoch(6)) == 3600
    # wraps past midnight: 23:30 -> next 23:00 is 23.5h away
    assert budget.secs_until_hour(23, local_epoch(23, 30)) == 23 * 3600 + 1800


def test_clock_format():
    assert budget.clock(0) == "0:00"
    assert budget.clock(65) == "1:05"
    assert budget.clock(600) == "10:00"
    assert budget.clock(3700) == "1:01:40"
