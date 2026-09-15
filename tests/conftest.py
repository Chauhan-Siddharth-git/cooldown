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


@pytest.fixture(scope="session", autouse=True)
def _exclusive_test_db():
    """Refuse to run two suites against db 15 at once.

    Both repos' suites use db 15 and every `rdb` flushes it, so a concurrent run deletes
    the other's fixtures mid-test. It does not fail cleanly: it produces a big pile of
    unrelated assertion errors that look exactly like a real regression, and it has cost
    this project a debugging session twice -- once reading it as 35 public-repo failures,
    once as a 21-test flake that was really a background job still finishing. One clear
    message beats twenty misleading ones.

    The lock lives in db 14 because db 15 is the one being flushed.
    """
    lock = redis.Redis(host="localhost", port=6379, db=14, decode_responses=True)
    try:
        lock.ping()
    except redis.exceptions.ConnectionError:
        yield                                  # no redis: the rdb fixture skips anyway
        return
    token = f"pid{os.getpid()}@{time.time():.0f}"
    # ex= so a killed run cannot wedge the lock forever; longer than any real suite.
    if not lock.set("suite_lock", token, nx=True, ex=1800):
        pytest.exit(
            f"redis db 15 is already in use by another test run ({lock.get('suite_lock')}). "
            "Wait for it, or clear a stale lock with: redis-cli -n 14 DEL suite_lock",
            returncode=1)
    try:
        yield
    finally:
        if lock.get("suite_lock") == token:
            lock.delete("suite_lock")


@pytest.fixture()
def rdb(monkeypatch):
    r = redis.Redis(host="localhost", port=6379, db=15, decode_responses=True)
    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        pytest.skip("needs a local redis (tests use db 15)")
    r.flushdb()
    monkeypatch.setattr(budget, "r", r)
    # The timezone lookup is cached in a module-level dict with a 5s TTL, which is right
    # in production and wrong across tests: a test that adopts America/Los_Angeles left
    # every test running in the next five seconds on that zone, which surfaced as a flat
    # 4-hour offset in the phase and stats suites. Bust it at both ends.
    budget._tz_bust()
    yield r
    r.flushdb()
    budget._tz_bust()


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
