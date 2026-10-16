"""The exit-node bypass detector: phone online on Tailscale, nothing through the box.

PLAN.md has listed it as the largest hole since July: deselect the exit node and every
gate is gone, silently. addon.py records each device's last connection; app.py compares
that with Tailscale's view and asks -- once -- whether the exit node is off.
"""
import time
from types import SimpleNamespace

import pytest

import addon
import app as budget

PHONE_IP = "100.64.0.7"
NOW = time.mktime((2026, 10, 20, 14, 0, 0, 0, 0, -1))


def status(phone_online=True, laptop_online=True):
    return {"Peer": {
        "a": {"HostName": "iphone-13-pro", "OS": "iOS", "TailscaleIPs": [PHONE_IP],
              "Online": phone_online},
        "b": {"HostName": "s-thinkpad", "OS": "linux", "TailscaleIPs": ["100.64.0.8"],
              "Online": laptop_online},
    }}


@pytest.fixture()
def box(rdb, monkeypatch):
    monkeypatch.setattr(addon, "r", rdb)
    sent = []
    monkeypatch.setattr(budget, "send_alert", lambda text, priority=None: sent.append(text))
    monkeypatch.setattr(budget, "_ts_status", lambda: status())
    rdb.set("bypass_watch_since", f"{NOW - 86400:.0f}")
    return SimpleNamespace(r=rdb, sent=sent)


def seen(rdb, ip, ts):
    rdb.hset("client_last_seen", ip, f"{ts:.0f}")


# ---------- addon.py: recording ----------

def test_every_connection_is_recorded_and_writes_are_batched(rdb, monkeypatch):
    monkeypatch.setattr(addon, "r", rdb)
    monkeypatch.setattr(addon, "_CLIENT_SEEN", {})
    monkeypatch.setattr(addon, "_CLIENT_SEEN_AT", [0.0])
    hook = addon.BudgetAddon().client_connected
    hook(SimpleNamespace(peername=("100.64.0.7", 50001)))
    assert rdb.hget("client_last_seen", "100.64.0.7"), "the first connection is written"
    first = rdb.hget("client_last_seen", "100.64.0.7")
    t = time.time()
    for i in range(50):                                # a burst inside one minute
        addon.note_client("100.64.0.7", t + 1 + i)
    assert rdb.hget("client_last_seen", "100.64.0.7") == first, "written per connection"
    addon.note_client("100.64.0.7", t + 120)
    assert rdb.hget("client_last_seen", "100.64.0.7") == f"{t + 120:.0f}"


def test_ipv4_mapped_addresses_are_stored_as_the_tailnet_address(rdb, monkeypatch):
    monkeypatch.setattr(addon, "r", rdb)
    monkeypatch.setattr(addon, "_CLIENT_SEEN", {})
    monkeypatch.setattr(addon, "_CLIENT_SEEN_AT", [0.0])
    addon.note_client("::ffff:100.64.0.7", time.time())
    assert rdb.hget("client_last_seen", "100.64.0.7")


def test_a_broken_redis_never_breaks_a_connection(monkeypatch):
    class Dead:
        def __getattr__(self, _):
            def f(*a, **k):
                raise ConnectionError("down")
            return f
    monkeypatch.setattr(addon, "r", Dead())
    monkeypatch.setattr(addon, "_CLIENT_SEEN", {})
    monkeypatch.setattr(addon, "_CLIENT_SEEN_AT", [0.0])
    addon.BudgetAddon().client_connected(SimpleNamespace(peername=("100.64.0.7", 1)))
    assert "100.64.0.7" in addon._CLIENT_SEEN, "the sighting was lost, not held"
    assert addon._CLIENT_SEEN_AT[0] == 0.0, "a failed write must be retried, not marked done"


# ---------- app.py: deciding ----------

def test_online_with_nothing_through_the_box_asks_once(box, day):
    seen(box.r, PHONE_IP, NOW - 2 * 3600)
    assert budget.check_bypass(NOW) == 1
    assert budget.check_bypass(NOW + 600) == 0, "asked again for the same lapse"
    msg = box.sent[0]
    assert "Exit node off?" in msg
    assert PHONE_IP not in msg and "iphone" not in msg.lower(), msg


def test_traffic_coming_back_ends_the_lapse(box, day):
    seen(box.r, PHONE_IP, NOW - 2 * 3600)
    budget.check_bypass(NOW)
    seen(box.r, PHONE_IP, NOW + 60)                    # exit node back on
    assert budget.check_bypass(NOW + 120) == 0
    assert budget.check_bypass(NOW + 60 + 2 * 3600) == 1, "a later lapse is a new one"
    assert len(box.sent) == 2


def test_recent_traffic_says_nothing(box, day):
    seen(box.r, PHONE_IP, NOW - 30 * 60)
    assert budget.check_bypass(NOW) == 0


def test_an_offline_phone_says_nothing(box, day, monkeypatch):
    """No signal, a flight and a flat battery all look like this."""
    monkeypatch.setattr(budget, "_ts_status", lambda: status(phone_online=False))
    seen(box.r, PHONE_IP, NOW - 5 * 3600)
    assert budget.check_bypass(NOW) == 0


def test_only_phones_are_watched(box, day):
    seen(box.r, PHONE_IP, NOW - 60)
    seen(box.r, "100.64.0.8", NOW - 5 * 3600)          # the laptop, quiet for hours
    assert budget.check_bypass(NOW) == 0


def test_nothing_is_said_at_night(box, night):
    seen(box.r, PHONE_IP, NOW - 5 * 3600)
    assert budget.check_bypass(NOW) == 0


def test_a_phone_never_seen_counts_from_when_watching_began(rdb, monkeypatch, day):
    sent = []
    monkeypatch.setattr(budget, "send_alert", lambda text, priority=None: sent.append(text))
    monkeypatch.setattr(budget, "_ts_status", lambda: status())
    assert budget.check_bypass(NOW) == 0                # starts the watch
    assert budget.check_bypass(NOW + 30 * 60) == 0
    assert budget.check_bypass(NOW + 2 * 3600) == 1, "exempt forever if never seen"


def test_tailscale_unreadable_says_nothing_and_does_not_raise(box, day, monkeypatch):
    monkeypatch.setattr(budget, "_ts_status", lambda: (_ for _ in ()).throw(OSError("no")))
    assert budget.check_bypass(NOW) == 0


def test_it_runs_on_a_schedule():
    assert any(j.func is budget.check_bypass for j in budget.scheduler.get_jobs())
