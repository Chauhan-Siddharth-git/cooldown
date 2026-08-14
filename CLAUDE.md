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
| **Gated-site origin** (`https://www.reddit.com/budget/*`) | The gate, the session endpoints, `/feed`, `/worth`. **Eight endpoints, and that is a budget — now fully spent** (cap set at 8 on 2026-08-02 with the set at 7; `/worth` took the last slot on 2026-08-03, and the cap has never been raised). Any script on that site — including its ads — is same-origin with all of it. |
| **Box origin** (`http://<box>:5000`) | The dashboard: `/stats`, `/health`, `/devices`, `/remaining`, `/boot-ack`. Cross-origin from every gated site, which **is** the control for *reading* (F9). It is not a control for *writing* — Flask refuses cross-origin writes itself (F25). Do not put these back behind the proxy. |
| **Loopback** `127.0.0.1:5000` | Flask. The addon reaches it here; this listener must never depend on the tailnet. |
| **Privileged** | Exactly one sudo rule: an `iptables` counter read. Services run as `cooldownapp` / `cooldownproxy`, no shell, no password. |
| **The trust anchor** | The CA at `/var/lib/cooldown/mitmproxy`, mode 700. Never leaves the box, never enters git. |

## Before you change anything

- **New Flask route** → classify it in `addon.BUDGET_ENDPOINTS` (gated origin — justify it)
  or `addon.MOVED_TO_BOX` (box origin). Unclassified routes fail the invariants. If it
  changes state, declare a non-GET method: the box-side check keys off `url_map`, so
  declaring `methods=['POST']` is what enrols it (F25). A state-changing GET-only route
  is guarded by nothing — don't write one.
- **New listener** → bind loopback + `tailscale0`. Never `0.0.0.0`; `COOLDOWN_LISTEN` is
  the Docker-only override, safe there only because compose publishes to host loopback.
- **Comparing a hostname** → `addon.host_matches()`. Nowhere else.
- **New `req.get`/`req.post` in `addon.py`** → the path must be validated against the
  closed endpoint set *before* it reaches a URL. Never concatenate.
- **Touching the gate template** → it is readable by any script on the gated site.
  Nothing worth stealing goes in it (`feed_tok`, never `ui_tok`).
- **Adding a gated site** → `news_domains.py` or `SITES`, then regenerate the decrypt
  allowlist (`python3 deploy/gen_allow_hosts.py`) and restart the proxy. **Four** places
  must agree: `app.py`, `addon.py`, the unit's `--allow-hosts`, and **the CA's name
  constraints** (F26). The fourth one costs: the CA can only vouch for the domains it was
  built with, so a new site needs `./rotate-ca.sh` and a re-trust on **every device**.
  Adding a site used to be a proxy restart. Batch them. The failure mode if you skip it is
  quiet in the wrong way — the proxy intercepts a site it cannot produce a valid
  certificate for, so that site breaks with a cert warning and nothing else says why.

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

**The injection cannot go, and that question is closed.** Two reasons, neither about
accuracy:

- **Passive cannot observe visibility and never will** — that fact lives in the browser.
  It agrees only because sites throttle themselves when hidden, which is third-party
  behaviour nobody here controls. If YouTube changes background polling, passive
  over-counts and nothing announces it. The heartbeat *knows*; passive *infers*.
- **The injection also draws the UI** — countdown, wind-down bar, last-minute warning.
  Passive replaces the measuring only.

**So the meter was repurposed rather than retired: it now feeds the dead-man's switch.**
`enforcement_looks_dead()` no longer guesses from page-load timing; it compares the two
meters over the last two hours and warns when the heartbeat falls below 40% of passive.
That is the job passive is actually suited for — it is a poor stopwatch and a good
witness, and a witness only needs to notice a large disagreement. Its 0.72x–1.28x
hourly scatter is nowhere near the threshold, and nothing is ever *charged* from it.

**This makes the shadow meter load-bearing.** Deleting it no longer costs 90 lines and
nothing else: the switch would go blind. It fails safe rather than loud (no passive data
→ `enforcement_looks_dead()` returns False, tested), so nothing breaks — but F20 would
stop being detectable. If you delete it, delete the ratio switch too, or give it another
second clock.

## Commands

```bash
python3 -m pytest tests/ -q              # needs a local redis (uses db 15)
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
5. **Finish finding before you start fixing.** Observe, then explain, then repair — and
   treat the gap between the first two as the work. On 2026-08-10 the observation was
   "both apt timers have no next elapse". The explanation reached for was a clock jump on
   a board with no RTC; the repair, written in the same motion, cleared the timers' stamp
   files. It was wrong. The real cause was two levels down — a first-boot wizard blocking
   `multi-user.target`, so the jobs queued forever — and the fix made the dashboard look
   healthier while changing nothing, which is worse than leaving it broken. Once the fix
   is written you are invested in the diagnosis and stop interrogating it. Before
   greenlighting any repair, answer: *what did I observe that rules out the alternatives?*
   If the answer is a mechanism that sounds plausible rather than something seen, stop.
   (Exception: anything live — an exposed service, an unknown SSH key, signs of intrusion
   — gets fixed now. This is about diagnosis discipline, not a waiting period.)
6. **Absence of signal is not a good signal.** The most common error in this project's
   history is reading an empty result as a clean one: zero failed logins from a journal
   that kept nothing, no `SYSCALL` records from a kernel that emits none, a mutation
   reported as uncaught because the file was never staged, a truncated `ss` listing read
   as the whole picture. Before running a check, say what a *broken* result would look
   like. If you cannot, the check cannot tell you anything.
7. **Fix the class, then grep for siblings.** Half of the third review's findings were an
   earlier fix repeated one call site away.
8. **Mutation-test anything you add.** Reintroduce the bug; confirm something fails. A
   check that cannot fail is decoration — this caught three defects in the pre-commit
   hook itself.
9. **Update every doc that asserts the old behaviour**, or it becomes the next reviewer's
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
