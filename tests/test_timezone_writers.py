"""Day keys are written in the zone they are read in.

The 2026-09-16 migration moved the READERS to the accounting zone and left the writers on
the box's clock: the heartbeat's usage: and usage_hour:, the entries: counter, and all of
addon.py. Displays drifted by up to three hours whenever you were away from the box's
zone. Fixed 2026-10-16. Each test below writes at a moment whose date differs between the
two clocks, so a writer on the wrong one cannot pass by coincidence.
"""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import addon
import app as budget

FAR = "Pacific/Kiritimati"                       # UTC+14
T = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc).timestamp()


def far_day(ts):
    return datetime.fromtimestamp(ts, ZoneInfo(FAR)).strftime("%Y-%m-%d")


def far_hour(ts):
    return datetime.fromtimestamp(ts, ZoneInfo(FAR)).strftime("%H")


def box_day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


@pytest.fixture()
def far(rdb, monkeypatch):
    if far_day(T) == box_day(T):
        pytest.skip("this machine's clock agrees with UTC+14 at the test moment")
    monkeypatch.setattr(addon, "r", rdb)
    rdb.set("tz_accounting", FAR)
    budget._tz_bust()
    addon._zones_bust()
    monkeypatch.setattr(budget.time, "time", lambda: T)
    return rdb


def test_the_heartbeat_files_usage_under_the_accounting_day(far, client, day):
    far.set("active_token:reddit", "tok")
    far.set("session:tok", "active")
    far.set("last_heartbeat:main", T - 10)
    assert client.post("/heartbeat?site=reddit").status_code == 200
    assert float(far.get(f"usage:{far_day(T)}:reddit")) > 9
    assert far.get(f"usage:{box_day(T)}:reddit") is None, "filed under the box's date"
    assert far.get(f"usage_hour:{far_day(T)}:{far_hour(T)}:reddit") is not None


def test_entering_counts_under_the_accounting_day(far, client, day):
    client.post("/enter?site=reddit")
    assert far.get(f"entries:{far_day(T)}") == "1"
    assert far.get(f"entries:{box_day(T)}") is None


def test_the_passive_meter_writes_in_the_accounting_zone(far):
    addon._SHADOW.clear()
    for i in range(4):
        addon.shadow_note("reddit", T + i * 5)
    addon._shadow_flush("reddit", addon._SHADOW["reddit"])
    assert far.keys("shadow_hour:*") == [f"shadow_hour:{far_day(T)}:{far_hour(T)}:reddit"]


def test_both_halves_of_the_switch_read_what_was_written(far):
    """The dead-man's switch compares usage_hour (app.py) with shadow_hour (addon.py).
    Writer and reader on different clocks would leave it comparing empty hours."""
    hour_ago = T - 3600
    far.set(f"usage_hour:{far_day(hour_ago)}:{far_hour(hour_ago)}:reddit", 600)
    far.set(f"shadow_hour:{far_day(hour_ago)}:{far_hour(hour_ago)}:reddit", 660)
    assert addon._meter_totals(T) == (600.0, 660.0)
    assert budget._meter_totals(T) == (600.0, 660.0)


def test_the_block_window_follows_the_policy_zone(far, monkeypatch):
    far.set("tz_policy", FAR)
    addon._zones_bust()
    h = int(far_hour(T))
    monkeypatch.setattr(addon, "BLOCK_FROM_HOUR", h)
    monkeypatch.setattr(addon, "BLOCK_TO_HOUR", h + 1)
    assert addon.blocked_now("4chan.org", T) is True


def test_no_zone_set_means_the_box_clock(rdb, monkeypatch):
    """Unchanged behaviour where nothing is configured -- the default every install has."""
    monkeypatch.setattr(addon, "r", rdb)
    addon._zones_bust()
    assert addon._shadow_day(T) == box_day(T)
