"""Fixtures for Cooldown tests.

Tests run against the LOCAL redis on db 15 (flushed around every test) so they can
never touch dev state in db 0 — and never the Pi, which has its own redis. Time-of-day
phases are controlled by monkeypatching `phase` (and `_hours_now` where the wind-down
ramp needs a clock); pure helpers like in_night()/effective_cap() take an explicit
`now` and are tested un-patched with synthetic epochs.
"""
import os
import sys
import time

import pytest
import redis

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as budget  # noqa: E402


@pytest.fixture()
def rdb(monkeypatch):
    r = redis.Redis(host="localhost", port=6379, db=15, decode_responses=True)
    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        pytest.skip("needs a local redis (tests use db 15)")
    r.flushdb()
    monkeypatch.setattr(budget, "r", r)
    yield r
    r.flushdb()


@pytest.fixture()
def client(rdb):
    budget.app.config["TESTING"] = True
    return budget.app.test_client()


@pytest.fixture()
def day(monkeypatch):
    monkeypatch.setattr(budget, "phase", lambda now=None: "day")


@pytest.fixture()
def night(monkeypatch):
    monkeypatch.setattr(budget, "phase", lambda now=None: "night")


@pytest.fixture()
def winddown(monkeypatch):
    monkeypatch.setattr(budget, "phase", lambda now=None: "winddown")
    monkeypatch.setattr(budget, "_hours_now", lambda now=None: 22.5)


@pytest.fixture()
def session(rdb):
    """Create a live session; last_gap sets how long ago the pool was last charged."""
    def make(site="reddit", mode="active", last_gap=15):
        tok = f"test-{site}"
        rdb.set(f"active_token:{site}", tok)
        rdb.setex(f"session:{tok}", 120, mode)
        rdb.set("last_heartbeat:main", time.time() - last_gap)
        return tok
    return make


def local_epoch(hour, minute=0):
    """Epoch for today at hour:minute local time — for pure now-taking helpers."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                        hour, minute, 0, lt.tm_wday, lt.tm_yday, -1))
