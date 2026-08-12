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
  `deploy/cooldown-proxy.service`). One source of truth would prevent a whole class
  of "site silently not gating" bugs.
- **Bind mitmproxy to the Tailscale interface** instead of `0.0.0.0`.
- **Swap the Flask dev server** for a production WSGI server (e.g. waitress).
- **Refresh the YouTube declutter selectors** when they inevitably drift.

## Testing notes

Tests run against a **local Redis on db 15** (isolated from real state) with an
injectable clock, so they never touch a live deployment. See `tests/conftest.py`.

## Security checks

Turn the pre-commit hook on once per clone:

```bash
git config core.hooksPath .githooks
```

It runs two things, and both exist because of specific mistakes this project made —
see [SECURITY-CASESTUDY.md](SECURITY-CASESTUDY.md):

- **Pattern rules**, against the lines your commit *adds* only, so existing code never
  nags you: substring host matching (F4/F18), an all-interfaces bind (F1), a launcher
  missing `--allow-hosts` (F16), privileged installs staged through a predictable
  `/tmp` path (F17), `render_template_string` on runtime data, and staged key material
  or auth keys.
- **`tests/test_invariants.py`**, which enforces what a grep can't see: every Flask
  route is classified as gated-origin or box-origin, every reachable mutating endpoint
  rejects cross-site requests *at both origins* (F25 — asserting it at one door only is
  how the check passed green while the other door was open), and no request path can
  redirect the proxy's internal call off `127.0.0.1:5000`.

**If an invariant fails, a decision was skipped — not the test being fussy.** Adding a
route without deciding which origin serves it is exactly the mistake F9 made twice.

One rule has an escape hatch, because `mitmdump` is occasionally run for something other
than proxying: put `# hook-exempt: no-allow-hosts` in the file with a reason (see the
CA-generation step in `install.sh`). Prefer that over `--no-verify` — it stays in the
diff and it only disables one rule for one file.

These are project-specific on purpose. A stock scanner found none of the twenty
findings; these rules would have caught four of them, all of which were repeats of an
earlier fix.
