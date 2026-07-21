from flask import Flask, jsonify, redirect, render_template_string, request
from urllib.parse import urlparse
import os
import redis
import time
import uuid
from apscheduler.schedulers.background import BackgroundScheduler
from news_domains import NEWS_DOMAINS

# No CORS: every endpoint is same-origin (the gate pages and the injected heartbeat
# both live on the gated host). A wildcard Access-Control-Allow-Origin only widened
# the attack surface. CSRF on the mutating POSTs is enforced at the proxy boundary
# (addon.py rejects cross-site requests to /budget/*).
app = Flask(__name__)
# Redis lives on localhost for the native/Pi deploy; in Docker it's a separate
# service, so honor REDIS_HOST/REDIS_PORT (defaults preserve native behaviour).
r = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)

# Per-site config. Add a site here and the proxy + budget logic pick it up.
#
# SINGLE SHARED BUCKET: every site draws from ONE spent counter (the "main" pool),
# but each keeps its OWN cap (budget_seconds). So spending anywhere drains the shared
# bucket, and a site is usable only while spent < its own cap. With Reddit=10m and
# YouTube=15m: burn 10m on Reddit and Reddit is out, but YouTube still shows 5m left.
# Redis pool state is keyed by the group ("main"): spent:{pool}, cooldown:{pool},
# last_heartbeat:{pool}. Per-site session state stays keyed by site (active_token:{site}).
SITES = {
    "reddit": {
        "home": "https://www.reddit.com",
        "budget_seconds": 10 * 60,
        "label": "Reddit",
        "emoji": "🤙",
        "group": "main",
    },
    "youtube": {
        "home": "https://www.youtube.com",
        "budget_seconds": 15 * 60,
        "label": "YouTube",
        "emoji": "🎬",
        "group": "main",
    },
    "spotify": {
        "home": "https://open.spotify.com",
        "budget_seconds": 10 * 60,
        "label": "Spotify",
        "emoji": "🎧",
        "group": "main",
    },
    "puzzmo": {
        "home": "https://www.puzzmo.com/today/",
        "budget_seconds": 10 * 60,
        "label": "Puzzmo",
        "emoji": "🧩",
        "group": "main",
    },
    # News is a CATEGORY, not one site: it matches the whole NEWS_DOMAINS list and
    # shares the "main" bucket, so switching between news sites (or from Reddit to a
    # news site) never buys fresh time — one distraction allowance for all of it.
    # "home" is only a rare fallback (Enter almost always returns you to the article
    # you were opening); a neutral non-news page keeps it from being an escape hatch.
    "news": {
        "home": "https://www.google.com",
        "budget_seconds": 10 * 60,
        "label": "News",
        "emoji": "📰",
        "group": "main",
    },
}
DEFAULT_SITE = "reddit"

RAPID_REPEAT_WINDOW = 3 * 60 * 60  # "a few hours" — a cooldown starting within this of
                                   # the previous one is a "rapid repeat" (binge clustering).
                                   # Also the window escalating cooldowns look back over.
# Escalating cooldowns: a lone cooldown is 1 hour, but back-to-back re-binges (each new
# cooldown starting within RAPID_REPEAT_WINDOW of the previous) get a progressively longer
# wall. The index is how many prior cooldowns already sit in that trailing window, so a
# spread-out day always stays at the 1-hour base; only clustering escalates.
COOLDOWN_LADDER = [60 * 60, 90 * 60, 120 * 60, 180 * 60]  # 1h · 1.5h · 2h · 3h (capped)
COOLDOWN_SECONDS = COOLDOWN_LADDER[0]   # base / back-compat default
SESSION_IDLE_TTL = 120         # 2 min without a foreground ping = session expires
HEARTBEAT_MAX_GAP = 30         # gaps between pings larger than this aren't charged (idle/away)
# Passive refill: while nothing in the pool is being actively used, spent ticks back
# down so partial use recovers over time. Rate is set so a fully-drained bucket (the
# largest cap) refills to full after this many seconds fully idle (~1 hour).
REFILL_FULL_SECONDS = 60 * 60
# ...but refill only kicks in after this long OFF the sites (grace window). Briefly
# waiting gives back nothing, so you can't wait a minute and sip another scroll;
# genuinely stepping away for a while still recovers time. Anti-binge lever.
REGEN_DELAY = 15 * 60

# Night mode (soft bedtime curfew). During [NIGHT_START_HOUR, NIGHT_END_HOUR) local
# time the shared bucket is capped small (NIGHT_BUDGET_SECONDS) AND refill is OFF — so
# you get one brief buffer if you truly need it, then the sites stay closed until the
# morning reset (which is moved to NIGHT_END_HOUR). No hard lock, so Tailscale-off is
# still the escape hatch; this is friction, not a wall.
NIGHT_START_HOUR = 23          # 11pm local — full night mode begins
NIGHT_END_HOUR = 7             # 7am local  (also when the daily reset fires)
NIGHT_BUDGET_SECONDS = 5 * 60
# Wind-down: for this long BEFORE night, each site's cap ramps linearly from its daytime
# budget down to the night buffer (and refill turns off), easing you toward lights-out
# instead of a sudden 11pm wall. Study mode stays available at all hours regardless.
WINDDOWN_SECONDS = 60 * 60

# YouTube "study mode" allowlist. Entering study mode grants a FREE session (no
# budget charge, ignores cooldown) that the proxy LOCKS to these playlists —
# search / home feed / Shorts / other channels bounce back to the course. To add a
# course: open its playlist on YouTube and copy the value after "list=" in the URL.
# Keep this list in sync with STUDY_PLAYLISTS in addon.py.
STUDY_PLAYLISTS = ["REPLACE_WITH_YOUR_PLAYLIST_ID"]  # allow-listed YouTube playlist IDs for Study mode; [] disables it

BUDGET_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>{{ label }} · Countdown</title>
    {% if refresh %}<meta http-equiv="refresh" content="{{ refresh }}">{% endif %}
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --go:#3ecf7c; --wait:#f0a63a; --sleep:#7aa2ff;
        }
        *{box-sizing:border-box}
        html,body{height:100%;margin:0}
        body{
            background:radial-gradient(1200px 620px at 50% -15%, #181c24, var(--bg));
            color:var(--fg);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;
            display:flex;align-items:center;justify-content:center;
            min-height:100dvh;padding:24px;
            padding-bottom:max(24px,env(safe-area-inset-bottom));
        }
        .card{
            position:relative;overflow:hidden;
            width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);
            border-radius:20px;padding:38px 28px 30px;text-align:center;
            box-shadow:0 24px 70px rgba(0,0,0,.55);
            --accent:var(--wait);
        }
        .card.go{--accent:var(--go)} .card.wait{--accent:var(--wait)} .card.sleep{--accent:var(--sleep)}
        .card::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent)}
        .kicker{
            display:flex;align-items:center;justify-content:center;gap:8px;
            font-size:11.5px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
            color:var(--muted);margin-bottom:20px;
        }
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--accent)}
        .big{
            font-size:64px;font-weight:700;letter-spacing:-2px;line-height:1;margin:2px 0 0;
            color:var(--accent);font-variant-numeric:tabular-nums;
        }
        h1{font-size:19px;font-weight:600;margin:16px 0 0;letter-spacing:-.2px}
        p{color:var(--muted);font-size:14.5px;line-height:1.55;margin:10px auto 0;max-width:30ch}
        .actions{margin-top:28px;display:flex;flex-direction:column;gap:10px}
        button{
            width:100%;padding:15px;font-size:16px;font-weight:600;border:none;
            border-radius:12px;cursor:pointer;-webkit-tap-highlight-color:transparent;
            transition:transform .05s ease,opacity .15s ease;
        }
        button:active{transform:scale(.985)}
        .enter{background:var(--go);color:#06120b}
        .study{background:transparent;color:var(--muted);border:1px solid var(--line)}
        .study:active{opacity:.7}
        /* Promoted to primary on the cooldown screens — the productive door out. */
        .study-cta{background:var(--sleep);color:#0a1020;border:none;font-weight:600}
        .blocked{background:#1c2028;color:var(--muted);cursor:default}
        .hint{font-size:12px;color:#5f6773;margin-top:2px}
        .foot{display:block;margin-top:18px;font-size:12px;color:#5f6773;text-decoration:none}
    </style>
</head>
<body>
    <div class="card {{ mood }}">
        <div class="kicker"><span class="dot"></span>{{ overline }}</div>
        {% if countdown %}<div id="cd" class="big" data-secs="{{ countdown }}">·</div>
        {% elif headline %}<div class="big">{{ headline }}</div>{% endif %}
        {% if title %}<h1>{{ title }}</h1>{% endif %}
        <p>{{ message }}</p>
        <div class="actions">
            {% if can_enter %}
            <form action="/budget/enter?site={{ site }}{% if next_url %}&next={{ next_url|urlencode }}{% endif %}" method="post">
                <button class="enter" type="submit">Enter {{ label }}</button>
            </form>
            {% elif button_text %}
            <button class="blocked" disabled>{{ button_text }}</button>
            {% endif %}
            {% if show_study %}
            <form action="/budget/study?site={{ site }}" method="post">
                <button class="study{% if study_primary %} study-cta{% endif %}" type="submit">{% if study_primary %}Study while you wait{% else %}Study mode{% endif %}</button>
            </form>
            <div class="hint">{% if study_primary %}Turn the break into Security+ progress — locked to the course, no scrolling.{% else %}Locked to the course playlist — no scrolling.{% endif %}</div>
            {% endif %}
        </div>
        <a class="foot" href="/budget/stats">Usage stats</a>
    </div>
    {% if countdown %}
    <script>
    (function(){
        var el=document.getElementById("cd");
        // Anchor to an absolute deadline and derive the remaining time from the wall
        // clock every tick. A plain `s--` counter drifts whenever the browser throttles
        // timers (backgrounded tab, locked phone), so it lagged reality until a manual
        // refresh. Computing (deadline - now) is self-correcting; hitting zero reloads
        // to re-sync with the server's authoritative value.
        var deadline=Date.now()+parseInt(el.dataset.secs,10)*1000;
        function fmt(n){var h=Math.floor(n/3600),m=Math.floor(n%3600/60),x=n%60,p=function(v){return String(v).padStart(2,"0")};
            return h?h+":"+p(m)+":"+p(x):m+":"+p(x);}
        function tick(){ var s=Math.round((deadline-Date.now())/1000);
            if(s<=0){location.reload();return;} el.textContent=fmt(s); }
        tick(); setInterval(tick,1000);
        // Recompute immediately on return, don't wait for the next (throttled) tick.
        document.addEventListener("visibilitychange",function(){ if(!document.hidden) tick(); });
    })();
    </script>
    {% endif %}
</body>
</html>
"""

STATS_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Usage · Countdown</title>
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --faint:#5f6773; --grid:#232732;
            --s1:#3987e5; --s2:#199e70; --s3:#c98500; --s4:#a678de;   /* reddit / youtube / spotify / puzzmo */
            --good:#0ca30c; --warn:#ec835a;
        }
        *{box-sizing:border-box}
        body{
            margin:0;background:var(--bg);color:var(--fg);
            font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;
            padding:28px 16px max(28px,env(safe-area-inset-bottom));
            display:flex;justify-content:center;
        }
        .wrap{width:100%;max-width:560px}
        .kicker{
            display:flex;align-items:center;gap:8px;justify-content:center;
            font-size:11.5px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
            color:var(--muted);margin-bottom:18px;
        }
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--s1)}
        .tiles{display:flex;gap:10px;margin-bottom:12px}
        .tile{
            flex:1;background:var(--card);border:1px solid var(--line);border-radius:14px;
            padding:14px 12px;text-align:center;
        }
        .tile .v{font-size:26px;font-weight:700;letter-spacing:-.5px;line-height:1.1}
        .tile .v.down{color:var(--good)} .tile .v.up{color:var(--warn)}
        .tile .k{font-size:11px;color:var(--faint);margin-top:5px;letter-spacing:.04em;text-transform:uppercase}
        .card{
            background:var(--card);border:1px solid var(--line);border-radius:16px;
            padding:20px 16px 14px;
        }
        .card h2{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 16px;letter-spacing:.02em}
        .chart{display:flex;align-items:flex-end;gap:6px;height:150px;border-bottom:1px solid var(--grid);padding-bottom:0}
        .day{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:2px;height:100%;position:relative;border-radius:4px 4px 0 0}
        .seg{width:100%;min-height:2px}
        .seg.r{background:var(--s1)} .seg.y{background:var(--s2)} .seg.s{background:var(--s3)} .seg.p{background:var(--s4)}
        .day .seg:first-child{border-radius:4px 4px 0 0}
        .day .tip{
            display:none;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);
            background:#1c2028;border:1px solid var(--line);border-radius:8px;padding:8px 10px;
            font-size:12px;line-height:1.6;white-space:nowrap;z-index:5;color:var(--fg);
            box-shadow:0 8px 24px rgba(0,0,0,.5);pointer-events:none;
        }
        .day:hover .tip{display:block}
        .tip b{font-weight:600}
        .tip .d{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px}
        .xlabels{display:flex;gap:6px;margin-top:6px}
        .xlabels span{flex:1;text-align:center;font-size:10px;color:var(--faint)}
        .legend{display:flex;gap:16px;justify-content:center;margin-top:14px;font-size:12px;color:var(--muted)}
        .legend .d{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:0}
        .live{font-size:12px;color:var(--faint);text-align:center;margin-top:16px}
        .live.stale{color:var(--warn)}
        details{margin-top:14px}
        summary{font-size:12px;color:var(--faint);cursor:pointer;text-align:center;list-style:none}
        table{width:100%;border-collapse:collapse;margin-top:10px;font-size:12.5px}
        th,td{padding:5px 6px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
        th{color:var(--faint);font-weight:600;border-bottom:1px solid var(--line)}
        td:first-child,th:first-child{text-align:left}
        tr.today td{color:var(--fg)}
        .back{display:block;text-align:center;margin-top:20px;font-size:12.5px;color:var(--faint);text-decoration:none}
        .cd-n{font-size:22px;font-weight:650;color:var(--fg);font-variant-numeric:tabular-nums}
        .cd-row{color:var(--muted);font-size:13px}
        .cd-sub{color:var(--faint);font-size:12.5px;margin-top:6px}
        .cd-warn{color:var(--warn)}
        .study-card{border-color:var(--sleep)}
        .study-row{display:flex;gap:28px;align-items:baseline}
        .study-n{font-size:26px;font-weight:700;color:var(--sleep);font-variant-numeric:tabular-nums}
        .study-k{font-size:12px;color:var(--faint);margin-left:7px;text-transform:uppercase;letter-spacing:.04em}
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>Usage · Last 14 days</div>

    <div class="tiles">
        <div class="tile"><div class="v">{{ today_min }}m</div><div class="k">Today</div></div>
        <div class="tile"><div class="v">{{ week_avg }}m</div><div class="k">7-day avg</div></div>
        <div class="tile">
            <div class="v {{ trend_cls }}">{{ trend }}</div><div class="k">vs prior week</div>
        </div>
    </div>

    <div class="card study-card">
        <h2>Study mode — the point of all this</h2>
        <div class="study-row">
            <div><span class="study-n">{{ study_today_min }}m</span><span class="study-k">today</span></div>
            <div><span class="study-n">{{ study_week_min }}m</span><span class="study-k">last 7 days</span></div>
        </div>
        {% if study_week_min == 0 %}
        <div class="cd-sub">No study-mode time logged this week. The course playlist is one tap from any gate — and free.</div>
        {% endif %}
    </div>

    <div class="card">
        <h2>Minutes on screen per day</h2>
        <div class="chart">
        {% for d in days %}
            <div class="day">
                {% if d.p_pct %}<div class="seg p" style="height:{{ d.p_pct }}%"></div>{% endif %}
                {% if d.s_pct %}<div class="seg s" style="height:{{ d.s_pct }}%"></div>{% endif %}
                {% if d.y_pct %}<div class="seg y" style="height:{{ d.y_pct }}%"></div>{% endif %}
                {% if d.r_pct %}<div class="seg r" style="height:{{ d.r_pct }}%"></div>{% endif %}
                <div class="tip"><b>{{ d.label_full }}</b><br>
                    <span class="d" style="background:var(--s1)"></span>Reddit {{ d.r_min }}m<br>
                    <span class="d" style="background:var(--s2)"></span>YouTube {{ d.y_min }}m<br>
                    <span class="d" style="background:var(--s3)"></span>Spotify {{ d.s_min }}m<br>
                    <span class="d" style="background:var(--s4)"></span>Puzzmo {{ d.p_min }}m<br>
                    <b>{{ d.total_min }}m total</b>
                </div>
            </div>
        {% endfor %}
        </div>
        <div class="xlabels">{% for d in days %}<span>{{ d.label }}</span>{% endfor %}</div>
        <div class="legend">
            <span><span class="d" style="background:var(--s1)"></span>Reddit</span>
            <span><span class="d" style="background:var(--s2)"></span>YouTube</span>
            <span><span class="d" style="background:var(--s3)"></span>Spotify</span>
            <span><span class="d" style="background:var(--s4)"></span>Puzzmo</span>
        </div>
        <details>
            <summary>Table view</summary>
            <table>
                <tr><th>Day</th><th>Reddit</th><th>YouTube</th><th>Spotify</th><th>Puzzmo</th><th>Total</th></tr>
                {% for d in days %}
                <tr {% if loop.last %}class="today"{% endif %}>
                    <td>{{ d.label_full }}</td><td>{{ d.r_min }}m</td><td>{{ d.y_min }}m</td>
                    <td>{{ d.s_min }}m</td><td>{{ d.p_min }}m</td><td>{{ d.total_min }}m</td>
                </tr>
                {% endfor %}
            </table>
        </details>
    </div>

    <div class="card">
        <h2>Cooldowns — binge clustering</h2>
        {% if cd.week_n %}
        <div class="cd-row"><span class="cd-n">{{ cd.today_n }}</span> today{% if cd.today_times %} · {{ cd.today_times|join(', ') }}{% endif %}</div>
        <div class="cd-sub">
            {% if cd.today_rapid %}<b class="cd-warn">{{ cd.today_rapid }} rapid repeat{{ 's' if cd.today_rapid != 1 else '' }}</b> today — a new cooldown within {{ cd.hours }}h of the last{% else %}No rapid repeats today (within {{ cd.hours }}h){% endif %}
        </div>
        <div class="cd-sub">7 days: {{ cd.week_n }} cooldown{{ 's' if cd.week_n != 1 else '' }}, {{ cd.week_rapid }} rapid repeat{{ 's' if cd.week_rapid != 1 else '' }}. Each rapid repeat draws a longer wall — clustering, not the daily total, is what escalates the cooldown.</div>
        {% else %}
        <div class="cd-sub">No cooldowns logged yet. Once you hit the full-bucket wall a few times, the clustering pattern shows up here.</div>
        {% endif %}
    </div>

    <div class="live {{ 'stale' if stale else '' }}">{{ live_line }}</div>
    <a class="back" href="/budget">← Back to the gate</a>
</div>
</body>
</html>
"""

def resolve_site(s):
    return s if s in SITES else DEFAULT_SITE

def pool(site):
    # Budget pool key. Sites sharing a "group" draw from one spent/cooldown counter
    # (keyed by the group name); otherwise each site is its own pool, keyed by site.
    return SITES[site].get("group", site)

def pool_sites(p):
    return [s for s in SITES if pool(s) == p]

def pool_max_budget(p):
    # The bucket is "full" at the largest cap among its sites — that's the amount
    # that must be spent to drain it completely (and trigger cooldown), and the
    # amount the refill restores over REFILL_FULL_SECONDS.
    return max(SITES[s]["budget_seconds"] for s in pool_sites(p))

def pool_has_active_session(p):
    for s in pool_sites(p):
        token = r.get(f"active_token:{s}")
        if token and r.get(f"session:{token}") == "active":
            return True
    return False

def in_night(now=None):
    # True during the full bedtime window (local time), handling the midnight wrap.
    h = time.localtime(now).tm_hour
    if NIGHT_START_HOUR <= NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= h < NIGHT_END_HOUR
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR

def _hours_now(now=None):
    lt = time.localtime(now)
    return lt.tm_hour + lt.tm_min / 60 + lt.tm_sec / 3600

def phase(now=None):
    # "night" (full curfew) | "winddown" (ramp in the run-up to curfew) | "day".
    if in_night(now):
        return "night"
    hours_until_night = (NIGHT_START_HOUR - _hours_now(now)) % 24
    return "winddown" if hours_until_night < WINDDOWN_SECONDS / 3600 else "day"

def effective_cap(site, now=None):
    # The site's budget cap right now. Day = its normal cap; night = the small shared
    # buffer; wind-down = a linear ramp from the day cap down to that buffer.
    ph = phase(now)
    if ph == "night":
        return NIGHT_BUDGET_SECONDS
    if ph == "winddown":
        hours_until_night = (NIGHT_START_HOUR - _hours_now(now)) % 24
        frac = max(0.0, min(1.0, hours_until_night / (WINDDOWN_SECONDS / 3600)))
        day = SITES[site]["budget_seconds"]
        return NIGHT_BUDGET_SECONDS + (day - NIGHT_BUDGET_SECONDS) * frac
    return SITES[site]["budget_seconds"]

def secs_until_hour(target_hour, now=None):
    # Seconds from now until the next occurrence of target_hour:00 local time.
    lt = time.localtime(now)
    cur = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    d = target_hour * 3600 - cur
    return d + 86400 if d <= 0 else d

def clock(secs):
    # m:ss (or h:mm:ss) for a headline time display.
    secs = int(secs)
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def apply_refill(p):
    # Slow passive refill of spent while the pool is idle, but only AFTER a grace
    # window (REGEN_DELAY) of no use — so briefly waiting can't top you back up for
    # another sip; genuinely stepping away for a while still recovers time. Skipped
    # while the pool is actively in use (real viewing time isn't discounted), during
    # cooldown (the hard wall must not leak away), and outside daytime (night + wind-down
    # budgets must NOT regenerate — that would fight the ramp / refill the night buffer).
    if pool_has_active_session(p) or r.get(f"cooldown:{p}") or phase() != "day":
        return
    spent = float(r.get(f"spent:{p}") or 0)
    if spent <= 0:
        return
    last = r.get(f"last_heartbeat:{p}")
    now = time.time()
    if not last:
        r.set(f"last_heartbeat:{p}", now)
        return
    # Only idle time PAST the grace window earns refill. `refilled_through` is the
    # timestamp up to which we've already credited; it advances continuously once the
    # grace has elapsed (so refill is smooth, not bursty), and is naturally superseded
    # when fresh use bumps last_heartbeat forward and resets the grace. last_heartbeat
    # itself is left untouched here — it stays the charge baseline for /heartbeat.
    grace_end = float(last) + REGEN_DELAY
    cursor = r.get(f"refilled_through:{p}")
    start = max(grace_end, float(cursor)) if cursor else grace_end
    if now <= start:
        return
    rate = pool_max_budget(p) / REFILL_FULL_SECONDS
    r.set(f"spent:{p}", max(0, spent - (now - start) * rate))
    r.set(f"refilled_through:{p}", now)

def get_spent(site):
    p = pool(site)
    apply_refill(p)
    return float(r.get(f"spent:{p}") or 0)

def night_spent(p):
    return float(r.get(f"night_spent:{p}") or 0)

def get_remaining_budget(site):
    p = pool(site)
    if phase() == "night":
        # Night has its OWN small buffer on a separate counter, independent of the
        # day's spend — a used-up day must not eat your night allowance (nor the
        # reverse). Non-regenerating; cleared at the 7am reset so each night is fresh.
        return max(0, NIGHT_BUDGET_SECONDS - night_spent(p))
    return max(0, effective_cap(site) - get_spent(site))

def get_cooldown_remaining(site):
    p = pool(site)
    cooldown_start = r.get(f"cooldown:{p}")
    if not cooldown_start:
        return 0
    duration = float(r.get(f"cooldown_secs:{p}") or COOLDOWN_SECONDS)  # escalated per-cooldown
    elapsed = time.time() - float(cooldown_start)
    remaining = duration - elapsed
    if remaining <= 0:
        # Don't restore budget outside daytime: a daytime cooldown expiring during
        # wind-down or night must NOT hand out a fresh buffer. Leave spent/cooldown
        # as-is (the tightened cap keeps you gated); the 7am reset clears everything.
        if phase() != "day":
            return 0
        # Cooldown is over — clear it AND restore the budget so the
        # next visit can enter again. Without resetting spent:{pool} the
        # /budget page would immediately re-trigger a fresh cooldown.
        r.delete(f"cooldown:{p}")
        r.delete(f"cooldown_secs:{p}")
        r.delete(f"spent:{p}")
        r.delete(f"refilled_through:{p}")
        return 0
    return remaining

def recent_cooldown_count(now):
    """How many pool cooldowns already started within the trailing RAPID_REPEAT_WINDOW
    (before `now`). This is the escalation index: 0 = a lone/spread-out cooldown (base
    duration), higher = a cluster of rapid re-binges (progressively longer wall). Scans
    today and yesterday since the window can straddle midnight.
    """
    cutoff = now - RAPID_REPEAT_WINDOW
    count = 0
    for i in (1, 0):
        key_day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for raw in r.lrange(f"cooldown_events:{key_day}", 0, -1):
            try:
                ts = float(raw.split()[0])
            except (ValueError, IndexError):
                continue
            if cutoff <= ts < now:
                count += 1
    return count

def start_cooldown(p, site, now=None):
    """Begin the pool's hard cooldown and log a timestamped event — once.

    Idempotent: if a cooldown is already running, do nothing (don't reset the
    timer, don't double-log). Duration escalates when cooldowns *cluster*: the
    event log (each entry "<epoch> <site>", per-day, self-pruning after ~100 days)
    is scanned so a rapid re-binge draws a longer wall from COOLDOWN_LADDER, while a
    spread-out day stays at the 1-hour base. The chosen duration is stored alongside
    the start so get_cooldown_remaining counts down the right amount.
    """
    if r.get(f"cooldown:{p}"):
        return
    now = now if now is not None else time.time()
    idx = min(recent_cooldown_count(now), len(COOLDOWN_LADDER) - 1)
    duration = COOLDOWN_LADDER[idx]
    r.set(f"cooldown:{p}", now)
    r.set(f"cooldown_secs:{p}", duration)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    r.rpush(f"cooldown_events:{day}", f"{now:.0f} {site}")
    r.expire(f"cooldown_events:{day}", 100 * 86400)

def _safe_next(site, nxt):
    """Validate a return-URL: http(s), no embedded credentials, host on the SAME
    gated site (home's registrable domain or a subdomain). Returns the URL if safe,
    else "".

    Hardened against parser-differential open redirects — cases where urlparse and
    the browser disagree on the host, e.g. "https://evil.com\\@reddit.com/" parses
    as host=reddit.com in Python (so a naive check allows it) while the browser
    reads "\\" as "/" and navigates to evil.com. We reject the characters that drive
    those differentials (backslashes, whitespace, control chars) and any userinfo
    "@" in the authority, then require a same-site host.
    """
    if not nxt:
        return ""
    if any(c in nxt for c in "\\ \t\r\n") or any(ord(c) < 0x20 or ord(c) == 0x7f for c in nxt):
        return ""
    try:
        u = urlparse(nxt)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https"):
        return ""
    if u.username is not None or u.password is not None or "@" in (u.netloc or ""):
        return ""
    host = u.hostname
    if not host:
        return ""
    # Which domains count as "same site" for the return URL. News is a category, so
    # any host in NEWS_DOMAINS (or a subdomain) is valid — that's how Enter returns you
    # to the specific article you opened. Other sites match their home's domain.
    if site == "news":
        domains = NEWS_DOMAINS
    else:
        home_host = urlparse(SITES[site]["home"]).hostname or ""
        domains = [".".join(home_host.split(".")[-2:])] if home_host else []   # www.reddit.com -> reddit.com
    return nxt if any(host == d or host.endswith("." + d) for d in domains) else ""

def render_gate(site, label, *, overline, message, title="", mood="wait",
                can_enter=False, button_text="", headline="",
                countdown=0, show_study=False, study_primary=False, refresh=0, next_url=""):
    # One template, many states. `overline` is the uppercase kicker; `countdown` (secs)
    # renders a live ticking timer that reloads at zero; `headline` renders a big static
    # time; `mood` picks the accent colour (go/wait/sleep). `next_url`, when set, makes
    # the Enter button return to the original link instead of the site home.
    # `study_primary` promotes the Study button to the main CTA — used on the cooldown
    # screens, turning the enforced break into a one-tap redirect to the course.
    return render_template_string(BUDGET_PAGE,
        site=site, label=label, overline=overline, title=title, message=message, mood=mood,
        can_enter=can_enter, button_text=button_text, headline=headline,
        countdown=int(countdown), show_study=show_study, study_primary=study_primary,
        refresh=refresh, next_url=next_url)

@app.route('/budget')
def budget_page():
    site = resolve_site(request.args.get("site"))
    label = SITES[site]["label"]

    p = pool(site)
    study_ok = (site == "youtube" and bool(STUDY_PLAYLISTS))
    ph = phase()
    # The addon passes the original URL the user was heading to, so Enter can return
    # there instead of the site home. Validated in /enter (must be on the same site).
    nxt = _safe_next(site, request.args.get("next", ""))

    # Night / wind-down own the gate (before any leftover daytime cooldown). Refill is
    # off in both; night is a small fixed buffer, wind-down a shrinking one. No cooldown
    # machinery here (Tailscale-off still escapes), and study mode stays available.
    if ph in ("night", "winddown"):
        remaining = get_remaining_budget(site)   # night-aware (own buffer) vs winddown ramp
        if ph == "night":
            if remaining <= 0:
                return render_gate(site, label, overline=f"{label} · Bedtime", mood="sleep",
                    countdown=secs_until_hour(NIGHT_END_HOUR), show_study=study_ok,
                    title="Get some sleep",
                    message=f"{label} is closed for the night. It reopens at {NIGHT_END_HOUR} AM.")
            return render_gate(site, label, overline=f"{label} · Night mode", mood="sleep",
                headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
                message=f"A small buffer, then closed till {NIGHT_END_HOUR} AM. No refill overnight — spend it wisely.")
        # wind-down
        if remaining <= 0:
            return render_gate(site, label, overline=f"{label} · Winding down", mood="wait",
                countdown=secs_until_hour(NIGHT_START_HOUR), show_study=study_ok,
                title="Paused for now",
                message="Easing toward bedtime — back briefly at night mode, then closed. Time for something calmer.")
        return render_gate(site, label, overline=f"{label} · Winding down", mood="wait",
            headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
            message="Your time is shrinking toward bedtime, and there's no refill now.")

    # Daytime.
    cooldown_remaining = get_cooldown_remaining(site)
    if cooldown_remaining > 0:
        escalated = float(r.get(f"cooldown_secs:{p}") or COOLDOWN_SECONDS) > COOLDOWN_SECONDS
        msg = ("Back-to-back sessions get a longer break — it reopens when the timer hits zero."
               if escalated else
               "That was your session. It reopens when the timer hits zero.")
        if study_ok:
            msg += " Put the break to work — the course is one tap away."
        return render_gate(site, label, overline=f"{label} · Cooldown", mood="wait",
            countdown=cooldown_remaining, show_study=study_ok, study_primary=study_ok,
            title="Take a break", message=msg)

    spent = get_spent(site)
    remaining = max(0, SITES[site]["budget_seconds"] - spent)

    # Whole bucket drained -> start the hard cooldown.
    if spent >= pool_max_budget(p):
        start_cooldown(p, site)
        msg = "Cooling down — back when the timer hits zero."
        if study_ok:
            msg += " Or turn the break into progress: the course is one tap away."
        return render_gate(site, label, overline=f"{label} · Time's up", mood="wait",
            countdown=get_cooldown_remaining(site), show_study=study_ok, study_primary=study_ok,
            title="Whole bucket spent", message=msg)

    # This site's slice used up, but the bucket still has time for a bigger-cap site.
    if remaining <= 0:
        others = [SITES[s]["label"] for s in pool_sites(p)
                  if s != site and get_remaining_budget(s) > 0]
        steer = f" Still time on {' & '.join(others)}." if others else ""
        return render_gate(site, label, overline=f"{label} · Spent", mood="wait",
            title=f"{label} is done for now", button_text=f"{label} used up",
            show_study=study_ok, refresh=15,
            message=f"You've used your {label} share of the bucket.{steer} It trickles back if you step away.")

    # Enter.
    return render_gate(site, label, overline=f"{label} · Time left", mood="go",
        headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
        message="Foreground time only — the clock ticks while you're looking. Make it count.")

@app.route('/enter', methods=['POST'])
def enter():
    site = resolve_site(request.args.get("site"))
    remaining = get_remaining_budget(site)
    cooldown = get_cooldown_remaining(site)

    if remaining <= 0 or cooldown > 0:
        return redirect(f'/budget?site={site}')

    token = str(uuid.uuid4())
    r.setex(f"session:{token}", SESSION_IDLE_TTL, "active")
    r.set(f"active_token:{site}", token)
    r.set(f"last_heartbeat:{pool(site)}", time.time())

    # Return to the original link the user clicked (validated same-site), else home.
    return redirect(_safe_next(site, request.args.get("next", "")) or SITES[site]["home"])

@app.route('/study', methods=['POST'])
def study():
    site = resolve_site(request.args.get("site"))
    # Study mode is YouTube-only and deliberately bypasses budget AND cooldown —
    # the lock-to-playlist enforcement (in the proxy + injected JS) is what keeps
    # it honest, so there's no time accounting here.
    if site != "youtube" or not STUDY_PLAYLISTS:
        return redirect(f'/budget?site={site}')
    # Study mode (locked to the course playlist) stays available at all hours — including
    # wind-down and overnight — since it's the productive escape, not a doomscroll path.

    token = str(uuid.uuid4())
    r.setex(f"session:{token}", SESSION_IDLE_TTL, "study")
    r.set(f"active_token:{site}", token)
    r.set("last_study_beat", time.time())   # baseline so the first heartbeat gap counts

    return redirect(f"https://www.youtube.com/playlist?list={STUDY_PLAYLISTS[0]}")

@app.route('/exit', methods=['POST', 'GET'])
def exit_session():
    # Ends the current session (study or budgeted) and returns to the gate.
    # Used by the in-page "Exit study mode" button; clearing the session is what
    # lets the next navigation fall through to the budget gate.
    site = resolve_site(request.args.get("site"))
    token = r.get(f"active_token:{site}")
    if token:
        r.delete(f"session:{token}")
    r.delete(f"active_token:{site}")
    return redirect(SITES[site]["home"])

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    site = resolve_site(request.args.get("site"))
    token = r.get(f"active_token:{site}")
    if not token:
        return jsonify({"status": "blocked"}), 403

    mode = r.get(f"session:{token}")
    if not mode:
        return jsonify({"status": "blocked"}), 403  # session idled out

    # Refresh the idle TTL, preserving the session mode ("active" or "study").
    r.setex(f"session:{token}", SESSION_IDLE_TTL, mode)

    # Study mode is free and always available: keep the session alive, never charge/cool.
    # We still LOG the foreground seconds (separately from budgeted usage) so "am I
    # actually studying?" is measurable — same visibility-gated, gap-capped accounting
    # as usage, but it never touches spent/cooldown.
    if mode == "study":
        now = time.time()
        last = r.get("last_study_beat")
        if last:
            gap = now - float(last)
            if gap <= HEARTBEAT_MAX_GAP:
                day = time.strftime("%Y-%m-%d")
                r.incrbyfloat(f"study_usage:{day}", gap)
                r.expire(f"study_usage:{day}", 100 * 86400)
                r.set("last_study_charge", now)
        r.set("last_study_beat", now)
        return jsonify({"status": "study"})

    p = pool(site)
    last = r.get(f"last_heartbeat:{p}")
    now = time.time()
    if last:
        gap = now - float(last)
        if gap <= HEARTBEAT_MAX_GAP:
            ph = phase()
            # Usage history: per-day, per-site seconds actually charged. Never cleared
            # by resets/cooldowns (it's history, not budget state); self-prunes after
            # ~100 days. last_charge doubles as a liveness marker for /stats — if it
            # goes stale for days, the heartbeat pipeline probably broke (fails open).
            day = time.strftime("%Y-%m-%d")
            r.incrbyfloat(f"usage:{day}:{site}", gap)
            r.expire(f"usage:{day}:{site}", 100 * 86400)
            r.set("last_charge", now)
            if ph == "night":
                # Charge the independent night buffer, not the day bucket. No cooldown
                # at night; just end the session when the small buffer is spent.
                if r.incrbyfloat(f"night_spent:{p}", gap) >= NIGHT_BUDGET_SECONDS:
                    r.delete(f"active_token:{site}")
                    r.delete(f"session:{token}")
                    return jsonify({"status": "blocked", "remaining": 0}), 403
            else:
                spent = r.incrbyfloat(f"spent:{p}", gap)
                if ph == "day":
                    # Whole bucket drained -> hard 1-hour cooldown for the pool.
                    if spent >= pool_max_budget(p):
                        r.delete(f"active_token:{site}")
                        r.delete(f"session:{token}")
                        start_cooldown(p, site, now)
                        return jsonify({"status": "blocked", "remaining": 0}), 403
                    # This site's slice is used up but the bucket isn't -> end just this
                    # site's session, no cooldown; a bigger-cap site can still be used.
                    if spent >= SITES[site]["budget_seconds"]:
                        r.delete(f"active_token:{site}")
                        r.delete(f"session:{token}")
                        return jsonify({"status": "blocked", "remaining": 0}), 403
                # Wind-down: shrinking cap on the day bucket, no cooldown.
                elif spent >= effective_cap(site):
                    r.delete(f"active_token:{site}")
                    r.delete(f"session:{token}")
                    return jsonify({"status": "blocked", "remaining": 0}), 403

    r.set(f"last_heartbeat:{p}", now)
    remaining = get_remaining_budget(site)
    return jsonify({"status": "ok", "remaining": int(remaining)})

@app.route('/remaining')
def remaining():
    site = request.args.get("site")
    if site:
        site = resolve_site(site)
        return jsonify({
            "site": site,
            "remaining": int(get_remaining_budget(site)),
            "cooldown": int(get_cooldown_remaining(site))
        })
    return jsonify({
        s: {
            "remaining": int(get_remaining_budget(s)),
            "cooldown": int(get_cooldown_remaining(s))
        }
        for s in SITES
    })

@app.route('/stats')
def stats():
    now = time.time()
    order = ["reddit", "youtube", "spotify", "puzzmo"]   # fixed series order (matches template)

    # Last 14 local days, oldest first.
    days = []
    totals = []
    for i in range(13, -1, -1):
        t = time.localtime(now - i * 86400)
        key_day = time.strftime("%Y-%m-%d", t)
        secs = {s: float(r.get(f"usage:{key_day}:{s}") or 0) for s in order}
        total = sum(secs.values())
        totals.append(total)
        days.append({
            "label": time.strftime("%-d", t) if i % 2 == 0 else "",
            "label_full": time.strftime("%a %b %-d", t),
            "secs": secs, "total": total,
        })

    # Scale segments against the biggest day (leave 0-height segments out entirely).
    max_total = max(totals) or 1
    for d in days:
        for s, css in (("reddit", "r"), ("youtube", "y"), ("spotify", "s"), ("puzzmo", "p")):
            pct = d["secs"][s] / max_total * 100
            d[f"{css}_pct"] = round(pct, 1) if pct >= 1 else 0
            d[f"{css}_min"] = int(d["secs"][s] // 60)
        d["total_min"] = int(d["total"] // 60)

    today_min = days[-1]["total_min"]
    this7 = sum(totals[7:]) / 7
    prior7 = sum(totals[:7]) / 7
    week_avg = int(this7 // 60)
    if prior7 <= 0:
        trend, trend_cls = "—", ""
    else:
        pct = (this7 - prior7) / prior7 * 100
        if pct <= -1:   trend, trend_cls = f"▾{abs(int(pct))}%", "down"   # less = good
        elif pct >= 1:  trend, trend_cls = f"▴{int(pct)}%", "up"
        else:           trend, trend_cls = "flat", ""

    # Cooldown clustering: does a fresh cooldown tend to start soon after the last
    # one ended? That "rapid repeat" (within RAPID_REPEAT_WINDOW) is the binge
    # signal the escalating-cooldown idea targets — high daily *totals* don't imply
    # it. Gather the last 7 days of "<epoch> <site>" events, ordered.
    cd_events = []
    for i in range(6, -1, -1):
        key_day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for raw in r.lrange(f"cooldown_events:{key_day}", 0, -1):
            try:
                cd_events.append(float(raw.split()[0]))
            except (ValueError, IndexError):
                continue
    cd_events.sort()
    week_rapid = sum(1 for a, b in zip(cd_events, cd_events[1:])
                     if b - a <= RAPID_REPEAT_WINDOW)

    # Study mode (free, unbudgeted) is logged separately — this is the one metric the
    # whole thing is FOR, so surface it. Today + this-week's foreground study minutes.
    today_key = time.strftime("%Y-%m-%d", time.localtime(now))
    study_today_min = int(float(r.get(f"study_usage:{today_key}") or 0) // 60)
    study_week_min = 0
    for i in range(7):
        dk = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        study_week_min += int(float(r.get(f"study_usage:{dk}") or 0) // 60)

    today_ts = sorted(t for t in cd_events
                      if time.strftime("%Y-%m-%d", time.localtime(t)) == today_key)
    cd_today_times = [time.strftime("%-I:%M%p", time.localtime(t)).lower() for t in today_ts]
    cd_today_rapid = sum(1 for a, b in zip(today_ts, today_ts[1:])
                         if b - a <= RAPID_REPEAT_WINDOW)
    cd = {
        "today_n": len(today_ts), "today_times": cd_today_times,
        "today_rapid": cd_today_rapid,
        "week_n": len(cd_events), "week_rapid": week_rapid,
        "hours": RAPID_REPEAT_WINDOW // 3600,
    }

    # Liveness: if nothing has been charged for days, either it's a clean streak or
    # the heartbeat pipeline silently broke (the system fails open — this is the alarm).
    last = r.get("last_charge")
    stale = False
    if not last:
        live_line = "No usage recorded yet."
    else:
        age = now - float(last)
        if age < 3600:        ago = f"{int(age // 60)}m ago"
        elif age < 86400:     ago = f"{int(age // 3600)}h ago"
        else:                 ago = f"{int(age // 86400)}d ago"
        if age > 3 * 86400:
            stale = True
            live_line = f"Nothing charged in {int(age // 86400)} days — clean streak, or a broken heartbeat?"
        else:
            live_line = f"Heartbeat alive — last charged {ago}."

    return render_template_string(STATS_PAGE,
        days=days, today_min=today_min, week_avg=week_avg,
        trend=trend, trend_cls=trend_cls, live_line=live_line, stale=stale, cd=cd,
        study_today_min=study_today_min, study_week_min=study_week_min)

def daily_reset():
    pools = set()
    for site in SITES:
        token = r.get(f"active_token:{site}")
        if token:
            r.delete(f"session:{token}")
        r.delete(f"active_token:{site}")
        pools.add(pool(site))
    for p in pools:                    # clear shared pools once (covers grouped sites)
        r.delete(f"spent:{p}")
        r.delete(f"night_spent:{p}")
        r.delete(f"cooldown:{p}")
        r.delete(f"cooldown_secs:{p}")
        r.delete(f"last_heartbeat:{p}")
        r.delete(f"refilled_through:{p}")
    print("[RESET] Daily budget reset complete")

scheduler = BackgroundScheduler()
# Reset at the curfew's end (7am), not midnight — a "fresh day" of budget starts when
# you wake, and this avoids handing out fresh budget in the middle of the night window.
scheduler.add_job(daily_reset, 'cron', hour=NIGHT_END_HOUR, minute=0)
scheduler.start()

if __name__ == '__main__':
    # Production WSGI server (waitress) instead of the Werkzeug dev server: more
    # robust for 24/7 operation, no dev-server warning. Localhost-only — it's only
    # ever reached via the mitmproxy addon over loopback.
    from waitress import serve
    serve(app, host='127.0.0.1', port=5000)
