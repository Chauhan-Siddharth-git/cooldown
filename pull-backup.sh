#!/usr/bin/env bash
# Pull the Pi's usage-history backups down to this laptop.
#
#   ./pull-backup.sh            fetch any new backups into ./backups/
#   ./pull-backup.sh --latest   fetch only the most recent one
#
# The point of the whole exercise: a Raspberry Pi's SD card is the thing most likely
# to die, and it takes the history with it. This puts a copy somewhere else.
set -euo pipefail
HOST="${COOLDOWN_HOST:-pi@cooldown-pi}"          # override: COOLDOWN_HOST=pi@1.2.3.4 ./pull-backup.sh
REMOTE_DIR=/var/backups/cooldown
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/backups"

mkdir -p "$LOCAL_DIR"; chmod 700 "$LOCAL_DIR"
echo "Pulling from $HOST:$REMOTE_DIR -> $LOCAL_DIR"

if ! ssh -o ConnectTimeout=10 "$HOST" "test -d $REMOTE_DIR"; then
  echo "No backup directory on the box yet. On the Pi, run:" >&2
  echo "  sudo systemctl start cooldown-backup.service" >&2
  exit 1
fi

if [ "${1:-}" = "--latest" ]; then
  f=$(ssh "$HOST" "ls -1 $REMOTE_DIR/cooldown-*.json.gz | tail -1")
  scp "$HOST:$f" "$LOCAL_DIR/"
else
  # -u so an interrupted run just resumes; no deletes, so the laptop keeps history
  # the Pi has already rotated away.
  rsync -av --ignore-existing "$HOST:$REMOTE_DIR/cooldown-*.json.gz" "$LOCAL_DIR/" 2>/dev/null \
    || scp "$HOST:$REMOTE_DIR/cooldown-*.json.gz" "$LOCAL_DIR/"
fi
chmod 600 "$LOCAL_DIR"/*.json.gz 2>/dev/null || true
echo
echo "Local copies ($(ls -1 "$LOCAL_DIR" | wc -l)):"
ls -lh "$LOCAL_DIR" | tail -5
echo
echo "Read one without Redis:  zcat backups/<file> | python3 -m json.tool | head"
echo "Restore onto a box:      venv/bin/python backup.py --restore <file>"
