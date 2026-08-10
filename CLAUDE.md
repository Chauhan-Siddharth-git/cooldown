# Working in this repo

Self-hosted anti-doomscroll gateway. `addon.py` is a mitmproxy addon that intercepts
gated sites; `app.py` is a Flask app holding the budget logic and the dashboard; Redis
holds state. **The box decrypts its owner's own traffic** — that is the entire security
story, and every design decision here is downstream of it.

Two deployment shapes: native on a Raspberry Pi (`deploy/*.service`, `install.sh`) and
Docker (`docker-compose.yml`, plus a privileged Tailscale variant). Changes to the app
usually need checking against both.

## The boundaries that matter

| Boundary | What lives there |
|---|---|
| **Gated-site origin** (`https://www.reddit.com/budget/*`) | The gate, the session endpoints, `/feed`. **Seven endpoints, and that is a budget.** Any script on that site — including its ads — is same-origin with all of it. |
| **Box origin** (`http://<box>:5000`) | The dashboard: `/stats`, `/health`, `/devices`, `/remaining`, `/boot-ack`. Cross-origin from every gated site, which **is** the control (F9). Do not put these back behind the proxy. |
| **Loopback** `127.0.0.1:5000` | Flask. The addon reaches it here; this listener must never depend on the tailnet. |
| **Privileged** | Exactly one sudo rule: an `iptables` counter read. Services run as `cooldownapp` / `cooldownproxy`, no shell, no password. |
| **The trust anchor** | The CA at `/var/lib/cooldown/mitmproxy`, mode 700. Never leaves the box, never enters git. |

## Before you change anything

- **New Flask route** → classify it in `addon.BUDGET_ENDPOINTS` (gated origin — justify it)
  or `addon.MOVED_TO_BOX` (box origin). Unclassified routes fail the invariants.
- **New listener** → bind loopback + `tailscale0`. Never `0.0.0.0`; `COOLDOWN_LISTEN` is
  the Docker-only override, safe there only because compose publishes to host loopback.
- **Comparing a hostname** → `addon.host_matches()`. Nowhere else.
- **New `req.get`/`req.post` in `addon.py`** → the path must be validated against the
  closed endpoint set *before* it reaches a URL. Never concatenate.
- **Touching the gate template** → it is readable by any script on the gated site.
  Nothing worth stealing goes in it (`feed_tok`, never `ui_tok`).
- **Adding a gated site** → `news_domains.py` or `SITES`, then regenerate the decrypt
  allowlist (`python3 deploy/gen_allow_hosts.py`) and restart the proxy. Three places
  must agree: `app.py`, `addon.py`, and the unit's `--allow-hosts`.

## The shadow meter: read out 2026-08-10, still running

`addon.shadow_note()` measures foreground time a second way — passively, by watching the
requests gated sites make on their own, with nothing injected. It writes only
`shadow_usage:*`, never budget state, and `/stats` shows both columns side by side.

**Result after 7 days (332 heartbeat minutes, 42 paired hours): passive tracks the
heartbeat at 1.03x**, daily 0.96x–1.08x. Both original predictions were wrong. The
undercount hypothesis — client-side feeds mean reading is invisible to the network — is
dead: zero hours where the heartbeat charged and passive saw nothing. Error is
one-directional (6.1 min of YouTube over-count, likely background audio); it never goes
blind. Verified non-circular: `/budget/*` returns before `shadow_note()`, checked by
sending a heartbeat POST through `request()` and watching the buffer stay empty.

**Do not read that as "the injection can go."** Two blockers, neither about accuracy:

- **Passive cannot observe visibility and never will** — that fact lives in the browser.
  It agrees only because sites throttle themselves when hidden, which is third-party
  behaviour nobody here controls. If YouTube changes background polling, passive
  over-counts and nothing announces it. The heartbeat *knows*; passive *infers*.
- **The injection also draws the UI** — countdown, wind-down bar, last-minute warning.
  Passive replaces the measuring only.

Next step if resumed: shadow-*enforcement* (let passive drive a parallel budget and log
when it would have cut you off), which tests the thing you would rely on. Aggregate
minutes do not. A visibility-only beacon — a few lines, no per-second POSTs, no UI —
would keep ground truth while shrinking the injected surface to almost nothing.

**It is still an experiment with an end.** If nobody acts on the above, delete it:
`shadow_note`, its two constants, the call site in `request()`, `shadow_comparison()`
and the `/stats` card. Roughly 90 lines.

## Commands

```bash
python3 -m pytest tests/ -q              # 541 tests; needs local redis (uses db 15)
python3 -m pytest tests/test_invariants.py -q   # the structural properties
git config core.hooksPath .githooks      # enable the pre-commit checks (once per clone)
./start.sh                               # local dev: loopback-only proxy, separate dev CA
./deploy.sh code|units|status            # native Pi deployment over SSH
```

After deploying, verify against the **box**, not the harness. Minimum: the dashboard 302s
away from every gated origin, the gate still renders, `/health` answers 200 on the box's
own address.

## If you are here to do a security review

Read this part first, then **read the code before reading `SECURITY-CASESTUDY.md`.**

1. **Treat every finding marked FIXED as unverified.** The case study is a findings log,
   not a clearance certificate. F9 was marked FIXED twice while still exploitable, and its
   written reasoning is what made two reviewers stop looking. F13 (a HIGH) sat in code two
   reviews had read line by line.
2. **Run the invariants first.** If they pass, the known classes are clean — spend your
   time on surface they do not cover, not on re-deriving F4.
3. **Walk boundaries, not features.** One question finds more than a file-by-file read:
   *what can a script on a gated origin reach, and what is browser-controlled input
   concatenated into?* That question produced four findings in one sitting.
4. **Prove it before reporting it.** A working PoC or it is a hypothesis.
5. **Fix the class, then grep for siblings.** Half of the third review's findings were an
   earlier fix repeated one call site away.
6. **Mutation-test anything you add.** Reintroduce the bug; confirm something fails. A
   check that cannot fail is decoration — this caught three defects in the pre-commit
   hook itself.
7. **Update every doc that asserts the old behaviour**, or it becomes the next reviewer's
   anchor.

**When to review at all:** on a new endpoint, origin, listener, privileged operation, or
new reliance on browser behaviour — those five account for all twenty findings. Plus after
any deploy that changes services, firewall rules or accounts. Plus twice a year for rot
(CVEs, pending updates, CA age, `ss -tlnp`). Not on a calendar otherwise.

## What this codebase gets wrong, historically

Substring matching where suffix matching was meant · security controls keyed to the
instance found rather than the class · convenience launchers skipping the hardened flags ·
silent fail-open in the enforcement path (a blocked heartbeat means time stops being
charged, and nothing reports it) · docs that outlive the code and become the next
reviewer's premise.
