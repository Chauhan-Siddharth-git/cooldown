#!/usr/bin/env python3
"""Serve the screenshot pages with mock data, so docs/screenshots/ can be regenerated.

    python3 tools/mock-screenshots.py          # then open the printed URLs

Why this exists rather than just photographing the real box: a live screenshot carries
the tailnet address, device names and real usage into a public repository. README promises
that every screenshot uses mock data, and this is what makes that promise keepable.

Nothing here touches the live database. It uses a scratch Redis db and refuses to start if
that number is 0.
"""
import os
import sys
import time

SCRATCH_DB = int(os.environ.get("SHOT_DB", "13"))
PORT = int(os.environ.get("SHOT_PORT", "5055"))
if SCRATCH_DB == 0:
    sys.exit("scratch db must not be 0 — that is the live database")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import redis                                     # noqa: E402
import app as budget                             # noqa: E402

budget.r = redis.Redis(host="localhost", port=6379, db=SCRATCH_DB, decode_responses=True)
budget.r.flushdb()

# Documentation addresses only. 100.64.0.x is the first block of the CGNAT range the
# tailnet lives in, so it looks right without being anybody's.
FAKE_BOX = "100.64.0.5"

HEALTH = {
    "model": "Raspberry Pi 4 Model B Rev 1.5",
    "cpu": {"pct": 12.4, "per_core": [14.0, 9.0, 17.0, 9.5], "load": [0.42, 0.38, 0.31],
            "cores": 4,
            # A plausible-looking four minutes: mostly idle with one burst, so the chart
            # has shape instead of a flat line.
            "hist": [[8 + (i % 7) * 2, 6 + (i % 5) * 3, 9 + (i % 4) * 4, 7 + (i % 6) * 2]
                     for i in range(60)]},
    "temp_c": 52.3,
    "temp_hist": [48 + (i % 9) for i in range(90)],
    "mem": {"used_mb": 388, "total_mb": 1845, "pct": 21},
    "disk": {"used_gb": 5.4, "total_gb": 30.9, "avail_gb": 24.0, "pct": 18},
    "uptime": "6d 4h",
    "net": {"eth0": {"up": True, "speed": "1000M"},
            "tailscale0": {"up": True, "speed": None}, "wlan0": None},
    "power": {"ok": True},
    "services": {"cooldown-app": "active", "cooldown-proxy": "active",
                 "cooldown-redirect": "active", "redis-server": "active",
                 "tailscaled": "active"},
    "services_ok": True,
    "errors": {"total": 0, "proxy": 0, "top": [], "last": "", "last_ago": None},
    "enforcement": {"ok": True, "browsing": False, "nav_ago": None, "charge_ago": None},
    "updates": {"fresh": True, "pending": 3, "security": 1, "reboot_required": False,
                "boot_ok": True, "stuck_jobs": 0,
                "packages": ["openssl", "libssl3", "curl"], "pkg_total": 3,
                "checked_human": "8 min ago", "next_install": "tonight at 3:14 am",
                "auto_reboot": "04:00", "last_result": "success",
                "last_run_ago": 61200, "checked_ago": 480},
    "audit": {"fresh": True, "findings": 0, "ssh_keys": 1, "shell_accounts": "pi root",
              "exposed_ports": "", "tampered_files": 0, "journal_persistent": True,
              "checked_human": "34 min ago", "ca_days": 3596, "ts_days": 128,
              "backup_age": 0, "backup_restores": 1, "full_ago": "2 days ago",
              "mode": "full", "checked_ago": 2040},
    # name/mb/pct, copied from _top_processes(). Written as mem_mb/cpu first, which
    # rendered an empty table -- the third mock shape guessed wrong rather than read.
    "procs": [{"name": "mitmdump", "mb": 96, "pct": 3.2},
              {"name": "tailscaled", "mb": 71, "pct": 3.5},
              {"name": "python", "mb": 52, "pct": 0.4},
              {"name": "systemd-journal", "mb": 26, "pct": 0.3},
              {"name": "redis-server", "mb": 12, "pct": 0.2}],
    "proc_total": 168,
    # Field names taken from _listening_ports(), not guessed: port/scope/rank/what.
    # An earlier version used "addr" and the panel silently rendered nothing.
    "ports": [{"port": 22, "what": "ssh", "scope": "all interfaces", "rank": 0},
              {"port": 8080, "what": "proxy · transparent", "scope": "all interfaces", "rank": 0},
              {"port": 8081, "what": "proxy · regular", "scope": "all interfaces", "rank": 0},
              {"port": 5000, "what": "dashboard", "scope": FAKE_BOX, "rank": 1},
              {"port": 6379, "what": "redis", "scope": "localhost", "rank": 2}],
}

def _dev(name, os_, kind, ip, online, down, up, seen=0):
    """Shape copied from _device() — collect_devices() returns a dict with a "devices"
    list, not a bare list, and the page indexes into it by key."""
    return {"name": name, "os": os_, "kind": kind, "ip": ip, "online": online,
            "direct": online, "relay": "" if online else "sfo",
            "down_bytes": down, "up_bytes": up,
            "down_bps": 41000 if online else 0, "up_bps": 8200 if online else 0,
            "down_h": "312 MB", "up_h": "44 MB", "last_seen": seen}


DEVICES = {
    "ok": True,
    "self": {"name": "cooldownpi", "ip": FAKE_BOX},
    "devices": [
        _dev("pixel-8", "android", "phone", "100.64.0.11", True, 327000000, 46000000),
        _dev("work-laptop", "linux", "computer", "100.64.0.12", True, 1200000000, 210000000),
        _dev("living-room-tv", "linux", "other", "100.64.0.13", False, 88000000, 3000000, 7200),
    ],
}


def seed():
    """Enough history that /stats and the gate look like a used system, not a fresh one."""
    now = time.time()
    for back in range(0, 14):
        day = time.strftime("%Y-%m-%d", time.localtime(now - back * 86400))
        for site, base in (("reddit", 900), ("youtube", 1500), ("news", 240)):
            budget.r.set(f"usage:{day}:{site}", base - (back * 37) % 400)
    budget.r.set("spent:main", 640)
    budget.r.set("last_charge", now - 45)


def main():
    seed()
    budget.collect_health = lambda *a, **k: dict(HEALTH)
    if hasattr(budget, "collect_devices"):
        budget.collect_devices = lambda *a, **k: dict(DEVICES)
    # Pin the rotating copy so a screenshot is reproducible rather than whatever the
    # day's seed happened to pick.
    if hasattr(budget, "gate_line"):
        budget.gate_line = lambda *a, **k: ("Foreground time only — the clock ticks "
                                            "while you're looking. Make it count.")

    print(f"\n  mock data in redis db {SCRATCH_DB}; live db 0 untouched\n")
    for path, name in (("/budget?site=reddit", "countdown.png / reflection.png"),
                       ("/health", "health.png"),
                       ("/devices", "devices.png"),
                       ("/stats", "(not currently in the README)")):
        print(f"    http://127.0.0.1:{PORT}{path:24s} -> {name}")
    print("\n  Ctrl-C when done. Suggested viewport: 900x1400 for the dashboards,")
    print("  420x900 for the gate pages (they are phone-first).\n")
    budget.app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
