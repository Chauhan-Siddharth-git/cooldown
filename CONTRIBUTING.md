# Contributing

Cooldown is a personal project shared in the hope it's useful. PRs and issues welcome;
no formality required.

## Ground rules

- **Never commit CA material or secrets.** `.gitignore` blocks the obvious paths —
  don't work around it. See [SECURITY.md](SECURITY.md).
- **Run the tests** before submitting: `python -m pytest tests/`. If you touch the
  budget/time logic, add or update a test — that state machine is subtle and the
  suite is what keeps it honest.
- Keep changes small and focused; match the surrounding style.

## Good first issues

- **Consolidate the duplicated config.** The gated-site list lives in three places
  (`SITES` in `app.py`, `SITES` in `addon.py`, and `--allow-hosts` in
  `deploy/budget-proxy.service`). One source of truth would prevent a whole class
  of "site silently not gating" bugs.
- **Bind mitmproxy to the Tailscale interface** instead of `0.0.0.0`.
- **Swap the Flask dev server** for a production WSGI server (e.g. waitress).
- **Refresh the YouTube declutter selectors** when they inevitably drift.

## Testing notes

Tests run against a **local Redis on db 15** (isolated from real state) with an
injectable clock, so they never touch a live deployment. See `tests/conftest.py`.
