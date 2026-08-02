#!/usr/bin/env python3
"""Back up the history Redis holds, to a plain JSON file you can read without Redis.

    backup.py                 write a new backup (this is what the nightly timer runs)
    backup.py --list          show what's been kept
    backup.py --restore FILE  put a backup back (refuses to clobber existing keys
                              unless you add --force)

Only *history* is saved — the record of what you actually did:

    usage:{day}:{site}     seconds charged, per day per site
    cooldown_events:{day}  when the hard wall fired
    soft_pauses:{day}      when a single site hit its cap
    reflect:{day}          why you reached for it, and whether naming it stopped you
    study_usage:{day}      study-mode seconds (feature ships off, kept for old data)

Live state (sessions, the current spend, a running cooldown) is deliberately skipped:
it's meaningless an hour later, and restoring a stale cooldown would be worse than
useless. Redis's own dump.rdb already survives a clean reboot — this exists for the
case that actually kills Raspberry Pis, which is the SD card dying, so the point is
to get a copy OFF the box (see pull-backup.sh).
"""
import argparse
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redis  # noqa: E402

PATTERNS = ["usage:*", "cooldown_events:*", "soft_pauses:*", "reflect:*", "study_usage:*"]
BACKUP_DIR = os.environ.get("COOLDOWN_BACKUP_DIR", "/var/backups/cooldown")
KEEP = 30

r = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)


def collect():
    """Read every history key, preserving whether it was a counter or a log."""
    data = {}
    for pat in PATTERNS:
        for key in r.scan_iter(match=pat, count=500):
            kind = r.type(key)
            if kind == "list":
                data[key] = {"t": "list", "v": r.lrange(key, 0, -1)}
            elif kind == "string":
                data[key] = {"t": "str", "v": r.get(key)}
            # anything else isn't ours; skip rather than guess
    return data


def write_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    data = collect()
    payload = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": os.uname().nodename,
        "keys": len(data),
        "data": data,
    }
    path = os.path.join(BACKUP_DIR, f"cooldown-{time.strftime('%Y-%m-%d')}.json.gz")
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:      # write-then-rename so a
        json.dump(payload, f, indent=1)                     # crash can't leave a torn file
    os.replace(tmp, path)
    os.chmod(path, 0o600)

    # Ask Redis to flush its own snapshot too, so the box's local copy is current.
    try:
        r.bgsave()
    except redis.ResponseError:
        pass                                                # a save was already running

    keep = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("cooldown-"))
    for old in keep[:-KEEP]:
        os.remove(os.path.join(BACKUP_DIR, old))
    print(f"{path}  ({len(data)} keys, {os.path.getsize(path)} bytes)")
    return path


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        print(f"no backups yet ({BACKUP_DIR} doesn't exist)")
        return
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("cooldown-"))
    if not files:
        print("no backups yet")
        return
    for f in files:
        p = os.path.join(BACKUP_DIR, f)
        print(f"  {f}  {os.path.getsize(p):>7} bytes")
    print(f"{len(files)} backup(s) in {BACKUP_DIR}")


def restore(path, force=False):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        payload = json.load(f)
    data = payload.get("data", {})
    existing = [k for k in data if r.exists(k)]
    if existing and not force:
        print(f"refusing to overwrite {len(existing)} key(s) that already exist, e.g. "
              f"{existing[:3]}\nRe-run with --force if that's really what you want.")
        return 1
    n = 0
    for key, rec in data.items():
        if force:
            r.delete(key)
        if rec["t"] == "list":
            if rec["v"]:
                r.rpush(key, *rec["v"])
        else:
            r.set(key, rec["v"])
        r.expire(key, 100 * 86400)          # same self-pruning window as live data
        n += 1
    print(f"restored {n} keys from {path} (created {payload.get('created')})")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore", metavar="FILE")
    ap.add_argument("--force", action="store_true", help="with --restore: overwrite existing keys")
    a = ap.parse_args()
    if a.list:
        list_backups()
    elif a.restore:
        sys.exit(restore(a.restore, a.force))
    else:
        write_backup()
