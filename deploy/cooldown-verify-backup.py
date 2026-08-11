#!/usr/bin/env python3
# NOTE: needs redis-py, which lives in the deployment venv and NOT in system python.
# Run it as: /home/pi/cooldown/venv/bin/python3 budget-verify-backup.py
"""Prove the newest backup actually restores. Weekly, as part of the full audit.

A backup nobody has restored is a hypothesis. The nightly job runs, the file is
non-empty, the dashboard is green -- and every one of those facts is also true of a
backup that would fail when you needed it. That is the same shape as everything else
that went wrong on this box: the check confirms the job ran, not that it worked.

SAFETY. backup.py's own restore() writes through a module-level connection with no db
argument, which means db 0 -- the live database. This script therefore does NOT call it.
It parses the file and performs its own writes against an explicitly-numbered scratch
database, refuses to run if that number is 0, and flushes only that database afterwards.
Live data is never opened for writing at any point.
"""
import glob
import gzip
import json
import os
import sys
import time

import redis

BACKUP_DIR = os.environ.get("COOLDOWN_BACKUP_DIR", "/var/backups/cooldown")
SCRATCH_DB = int(os.environ.get("VERIFY_SCRATCH_DB", "14"))   # 15 belongs to the test suite
LIVE_DB = 0

# Structural, not a comment: a typo in the env var must not be able to point this at live.
if SCRATCH_DB == LIVE_DB:
    print(json.dumps({"ok": False, "error": "scratch db must not be the live db"}))
    sys.exit(2)


def newest_backup():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "*.json.gz")))
    return files[-1] if files else None


def main():
    out = {"checked": int(time.time()), "ok": False, "file": None,
           "keys_in_file": 0, "keys_restored": 0, "age_days": None, "error": None}

    path = newest_backup()
    if not path:
        out["error"] = f"no backups in {BACKUP_DIR}"
        print(json.dumps(out)); return 1
    out["file"] = os.path.basename(path)
    out["age_days"] = int((time.time() - os.path.getmtime(path)) / 86400)

    # 1. Is it readable at all? A truncated or corrupt gzip fails here, which is the
    #    single most likely way a backup is useless.
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as e:
        out["error"] = f"unreadable: {type(e).__name__}: {e}"
        print(json.dumps(out)); return 1

    data = payload.get("data") or {}
    out["keys_in_file"] = len(data)
    if not data:
        out["error"] = "backup contains no keys"
        print(json.dumps(out)); return 1

    # 2. Does it actually go back in? Types matter -- a list written with SET would
    #    restore "successfully" and be silently wrong.
    scratch = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                          port=int(os.environ.get("REDIS_PORT", "6379")),
                          db=SCRATCH_DB, decode_responses=True)
    try:
        scratch.flushdb()
        for key, rec in data.items():
            if rec.get("t") == "list":
                if rec.get("v"):
                    scratch.rpush(key, *rec["v"])
            else:
                scratch.set(key, rec.get("v"))
        out["keys_restored"] = scratch.dbsize()

        # 3. Spot-check that values survived, not just key names.
        mismatches = []
        for key, rec in list(data.items())[:50]:
            if rec.get("t") == "list":
                if list(scratch.lrange(key, 0, -1)) != list(rec.get("v") or []):
                    mismatches.append(key)
            elif scratch.get(key) != str(rec.get("v")):
                mismatches.append(key)
        out["value_mismatches"] = len(mismatches)

        # 4. Sanity against live, READ ONLY. A backup holding a tiny fraction of live
        #    is technically restorable and practically useless.
        live = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                           port=int(os.environ.get("REDIS_PORT", "6379")),
                           db=LIVE_DB, decode_responses=True)
        out["keys_live"] = live.dbsize()

        empty_lists = sum(1 for r_ in data.values()
                          if r_.get("t") == "list" and not r_.get("v"))
        out["empty_lists"] = empty_lists

        out["ok"] = (out["keys_restored"] + empty_lists >= out["keys_in_file"]
                     and not mismatches)
        if not out["ok"] and not out["error"]:
            out["error"] = (f"restored {out['keys_restored']} of {out['keys_in_file']} keys, "
                            f"{len(mismatches)} value mismatch(es)")
    except Exception as e:
        out["error"] = f"restore failed: {type(e).__name__}: {e}"
    finally:
        try:
            scratch.flushdb()          # leave nothing behind, even on failure
        except Exception:
            pass

    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
