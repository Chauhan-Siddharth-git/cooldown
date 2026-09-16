#!/usr/bin/env python3
"""Read out the budget experiment pre-registered in PLAN.md on 2026-09-16.

    tools/readout-budget-experiment.py          # run it on or after 2026-10-01

WRITTEN BEFORE THE DATA EXISTED, deliberately. A pre-registration that names a threshold
but leaves the analysis to be written afterwards is only half a pre-registration: the
thresholds stay fixed while the *method* quietly bends toward whatever the numbers turn out
to be. Every choice here -- which days, which statistic, which test, which cutoffs -- was
fixed on 2026-09-16 with no idea which way it would go.

If you find yourself editing this file on readout day, that is the thing it exists to stop.
Read the output, then argue with it in prose.

The question: sixty-one baseline days showed a median of 62.4 min/day and no trend across
five different friction configurations. Does usage track what the budget PERMITS, or what
the friction discourages? On 2026-09-16 the pool went 15 min -> 10 (per-site 10 -> 7).
"""
import json, random, statistics as st, subprocess, sys, time

PI = "pi@<box>"
SITES = ["reddit", "youtube", "news", "spotify", "puzzmo"]
BASELINE = ("2026-07-18", "2026-09-16")   # 09-16 is the transition day and is EXCLUDED below
EXPERIMENT = ("2026-09-17", "2026-09-30")
TRANSITION = "2026-09-16"                 # cap changed mid-afternoon; a cooldown fired that
                                          # was an artefact of the change, not behaviour
H1_MEDIAN_BELOW = 55.0                    # budget-bound
H2_BAND = (55.0, 70.0)                    # friction-bound / inelastic
SHUFFLES = 20000
SEED = 7                                  # fixed so the p-value is reproducible


def fetch():
    """usage:{day}:{site} for every day in range, summed per day, in minutes."""
    script = '''
import redis, time, json
r = redis.Redis(decode_responses=True)
SITES = %r
rows = {}
now = time.time()
for d in range(120, -1, -1):
    day = time.strftime("%%Y-%%m-%%d", time.localtime(now - d * 86400))
    tot = sum(float(r.get("usage:%%s:%%s" %% (day, s)) or 0) for s in SITES)
    cds = r.llen("cooldown_events:" + day)
    soft = r.llen("soft_pauses:" + day)
    if tot or cds or soft:
        rows[day] = {"min": round(tot / 60, 2), "cooldowns": cds, "soft": soft}
print(json.dumps(rows))
''' % (SITES,)
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", PI,
                          "/home/pi/cooldown/venv/bin/python - "],
                         input=script, capture_output=True, text=True)
    if out.returncode or not out.stdout.strip():
        sys.exit(f"could not read the box: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def window(rows, lo, hi, drop=()):
    return {d: v for d, v in rows.items() if lo <= d <= hi and d not in drop}


def permutation(a, b, shuffles=SHUFFLES):
    """One-sided: how often does chance give a difference at least this negative?"""
    random.seed(SEED)
    obs = st.mean(a) - st.mean(b)
    pool, n, hits = a + b, len(a), 0
    for _ in range(shuffles):
        random.shuffle(pool)
        if st.mean(pool[:n]) - st.mean(pool[n:]) <= obs:
            hits += 1
    return obs, hits / shuffles


def main():
    rows = fetch()
    base = window(rows, *BASELINE, drop=(TRANSITION,))
    exp = window(rows, *EXPERIMENT)
    if len(exp) < 10:
        print(f"Only {len(exp)} experiment days on record ({EXPERIMENT[0]}..{EXPERIMENT[1]}).")
        print("The window is not finished. Reading out early is how a null becomes a trend.")
        return 1

    bu = [v["min"] for v in base.values()]
    eu = [v["min"] for v in exp.values()]
    diff, p = permutation(eu, bu)

    print(f"BUDGET EXPERIMENT READOUT   pre-registered 2026-09-16, pool 15min -> 10min\n")
    print(f"  {'':12} {'days':>5} {'median':>8} {'mean':>8} {'cooldowns/d':>12} {'soft/d':>8}")
    for label, w in (("baseline", base), ("experiment", exp)):
        u = [v["min"] for v in w.values()]
        print(f"  {label:12} {len(w):5d} {st.median(u):8.1f} {st.mean(u):8.1f}"
              f" {sum(v['cooldowns'] for v in w.values())/len(w):12.2f}"
              f" {sum(v['soft'] for v in w.values())/len(w):8.2f}")

    med = st.median(eu)
    print(f"\n  change in mean: {diff:+.1f} min/day     permutation p (one-sided) = {p:.3f}")
    print(f"  experiment median: {med:.1f} min/day\n")

    if med < H1_MEDIAN_BELOW:
        v = ("H1 BUDGET-BOUND", f"median {med:.1f} is below the pre-registered {H1_MEDIAN_BELOW}",
             "Usage follows the dial. The friction features are decoration on a number, and\n"
             "  the number is the product. Changing the pool is the lever worth having.")
    elif H2_BAND[0] <= med <= H2_BAND[1]:
        v = ("H2 FRICTION-BOUND / INELASTIC", f"median {med:.1f} sits in the {H2_BAND} band",
             "A third of the pool was removed and the behaviour did not move. Neither confound\n"
             "  explains that, which makes this the CLEAN result. The answer is not in this repo.")
    else:
        v = ("NEITHER", f"median {med:.1f} is outside both pre-registered ranges",
             "Unpredicted. Say so plainly rather than choosing whichever hypothesis it is\n"
             "  nearer to -- that is the move pre-registration exists to prevent.")
    print(f"  VERDICT: {v[0]}\n    ({v[1]})\n    {v[2]}")

    print("\n  CONFOUNDS, named on 2026-09-16 and not to be discovered now:")
    print("    The window opened during travel and during heavy work on this project, both of")
    print("    which plausibly suppress usage on their own. So a DROP is ambiguous. A null is")
    print("    the clean result. Cooldowns/day rise under both hypotheses and decide nothing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
