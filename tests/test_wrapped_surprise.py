"""The year in review is a once-a-year surprise, not a page you can open whenever.

It opens on the anniversary of first_run, stays open a week, and is otherwise a 404 that
says nothing about when. It surfaces as a popup over the dashboard and one push that
morning, and never on the gate, which any script on a gated site can read.
"""
import time

import pytest

import app as budget

SETUP = "2026-07-06"


def at(y, m, d, hour=12):
    """A box-local timestamp. Tests run with no tz_accounting, so local_acct is the box's
    clock, the same one time.mktime reads."""
    return time.mktime((y, m, d, hour, 0, 0, 0, 0, -1))


@pytest.fixture()
def setup(rdb):
    rdb.set("first_run", SETUP)
    return rdb


@pytest.mark.parametrize("when,expect", [
    (at(2026, 7, 6), None),               # the setup day itself is not an anniversary
    (at(2026, 10, 16), None),             # a few months into the first year
    (at(2027, 7, 5), None),               # the day before
    (at(2027, 7, 6, 0), 1),               # the first anniversary, from its first hour
    (at(2027, 7, 12, 23), 1),             # day 7, its last hour
    (at(2027, 7, 13), None),              # day 8: locked again
    (at(2028, 1, 1), None),               # nothing in between
    (at(2028, 7, 6), 2),                  # and back the next year
])
def test_it_opens_on_the_anniversary_for_a_week(setup, when, expect):
    w = budget.wrapped_window(when)
    assert (w[0] if w else None) == expect, time.ctime(when)


def test_no_setup_date_means_nothing_opens(rdb):
    assert budget.wrapped_window(at(2027, 7, 6)) is None
    rdb.set("first_run", "not a date")
    assert budget.wrapped_window(at(2027, 7, 6)) is None
    rdb.set("first_run", "2026-13-40")             # parses, but is no date at all
    assert budget.wrapped_window(at(2027, 7, 6)) is None


def test_a_leap_day_setup_still_gets_its_anniversary(rdb):
    rdb.set("first_run", "2028-02-29")
    assert budget.wrapped_window(at(2029, 2, 28))[0] == 1
    assert budget.wrapped_window(at(2029, 3, 7)) is None


def test_a_late_december_window_runs_into_january(rdb):
    rdb.set("first_run", "2026-12-29")
    assert budget.wrapped_window(at(2028, 1, 2))[0] == 1
    assert budget.wrapped_window(at(2028, 1, 5)) is None


def _make_today(rdb, years_ago):
    t = budget.local_acct(time.time())
    day = min(t.tm_mday, 28) if t.tm_mon == 2 else t.tm_mday
    rdb.set("first_run", f"{t.tm_year - years_ago}-{t.tm_mon:02d}-{day:02d}")


def test_closed_it_is_a_plain_404_that_gives_nothing_away(rdb, client):
    """Not "locked", not a date: the same response as a URL that never existed."""
    _make_today(rdb, years_ago=0)                     # set up today: the first year
    resp = client.get("/wrapped")
    never = client.get("/no-such-page-at-all")
    assert resp.status_code == 404
    assert resp.get_data() == never.get_data()
    for page in ("/stats", "/digest", "/health"):
        html = client.get(page).get_data(as_text=True)
        assert "/wrapped" not in html, f"{page} still points at it"
        assert "cd-yr" not in html, f"{page} shows the popup outside the window"


def test_open_it_pops_up_over_the_dashboard_until_seen(rdb, client):
    _make_today(rdb, years_ago=1)
    for page in ("/stats", "/digest", "/health", "/devices"):
        assert 'class="cd-yr"' in client.get(page).get_data(as_text=True), page
    assert client.get("/wrapped").status_code == 200       # opening it marks it seen
    html = client.get("/stats").get_data(as_text=True)
    assert 'class="cd-yr"' not in html, "the popup came back after it was opened"
    assert 'href="/wrapped' in client.get("/digest").get_data(as_text=True), \
        "the digest should keep a link for the rest of the week"


def test_the_popup_never_reaches_the_gate(rdb, client, day):
    """The gate is served on the gated site's origin, where its scripts can read it."""
    _make_today(rdb, years_ago=1)
    html = client.get("/budget?site=reddit").get_data(as_text=True)
    assert "cd-yr" not in html and "/wrapped" not in html


def test_the_push_goes_once_per_anniversary_with_no_address(setup, monkeypatch):
    sent = []
    monkeypatch.setattr(budget, "send_alert", lambda text, priority=None: sent.append(text))
    assert budget.announce_wrapped(at(2027, 7, 5)) is False     # not yet
    assert budget.announce_wrapped(at(2027, 7, 6)) is True
    assert budget.announce_wrapped(at(2027, 7, 6, 18)) is False # same day, second reset
    assert budget.announce_wrapped(at(2027, 7, 8)) is False     # rest of the week
    assert len(sent) == 1
    assert "http" not in sent[0] and "100." not in sent[0], sent[0]
    assert budget.announce_wrapped(at(2028, 7, 6)) is True      # next year
    assert len(sent) == 2


def test_a_box_that_was_off_on_the_day_still_announces_that_week(setup, monkeypatch):
    sent = []
    monkeypatch.setattr(budget, "send_alert", lambda text, priority=None: sent.append(text))
    assert budget.announce_wrapped(at(2027, 7, 9)) is True
    assert len(sent) == 1


def test_the_daily_reset_is_what_announces_it(setup, monkeypatch):
    sent = []
    monkeypatch.setattr(budget, "send_alert", lambda text, priority=None: sent.append(text))
    budget.daily_reset(at(2027, 7, 6, 7))
    assert len(sent) == 1
