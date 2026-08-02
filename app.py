from flask import Flask, jsonify, redirect, render_template_string, request
from urllib.parse import urlparse
from collections import deque
from datetime import datetime, timezone
import os
import subprocess
import json
import re
import random
import redis
import secrets
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

# Soft-pause cluster brake: if ONE site trips its own cap CLUSTER_THRESHOLD times inside a
# rolling CLUSTER_WINDOW, a short site-specific cooldown breaks the loop. A lighter, targeted
# cousin of the hard pool cooldown — only bites the Nth re-max in the window, not each one.
CLUSTER_WINDOW = 2 * 60 * 60        # rolling look-back window
CLUSTER_THRESHOLD = 3               # the Nth cap-hit inside the window trips it
CLUSTER_COOLDOWN_SECONDS = 25 * 60  # a short breather, not the 1h hard wall
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
# Study mode: a free, always-open escape hatch locked to an allow-listed YouTube playlist.
# OFF by default — put one or more playlist IDs here AND in addon.py (both lists must match)
# to switch it on. Ships off because a placeholder ID renders a study button that goes
# nowhere, and because the feature only earns its keep if you'll genuinely use it.
STUDY_PLAYLISTS = []

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
        /* Ambient "encrypted traffic through the porthole" background (canvas). */
        #bp-bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;display:block}
        .card{
            position:relative;z-index:1;overflow:hidden;
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
        .foots{display:flex;gap:18px;justify-content:center;align-items:center;margin-top:18px}
        .infobtn{width:19px;height:19px;padding:0;border-radius:50%;border:1px solid var(--line);
            background:transparent;color:#5f6773;font:600 11px/1 -apple-system,Roboto,Arial,sans-serif;
            cursor:pointer;flex:none}
        .infobtn:hover{color:var(--fg);border-color:#3a4150}
        /* Explains the live traffic feed behind the page. Sits inside the card (which clips
           overflow), so it covers the content rather than escaping the rounded corners. */
        .bgpanel{position:absolute;inset:0;z-index:3;display:none;flex-direction:column;
            justify-content:flex-start;gap:9px;padding:20px 22px;text-align:left;
            overflow-y:auto;-webkit-overflow-scrolling:touch;
            background:rgba(14,17,22,.93);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
        .bgpanel.on{display:flex}
        .bgpanel h2{margin:0 0 1px;font-size:14.5px;font-weight:700;letter-spacing:-.2px;text-align:center}
        .bgpanel p{margin:0;font-size:12px;line-height:1.45;color:var(--muted);max-width:none}
        .bgpanel .key{display:flex;gap:9px;align-items:flex-start;font-size:12.5px;line-height:1.45;color:var(--fg)}
        .bgpanel .sw{width:9px;height:9px;border-radius:2px;flex:none;margin-top:4px}
        .bgpanel .sw.g{background:#4ede8c} .bgpanel .sw.r{background:#f06060}
        .bgpanel .pct{margin-left:auto;padding-left:8px;font-variant-numeric:tabular-nums;
            font-weight:700;font-size:12.5px;color:var(--fg)}
        .bgbar{display:flex;height:9px;border-radius:5px;overflow:hidden;background:#0e1116;margin:2px 0 4px}
        .bgbar i{display:block;height:100%;width:0;transition:width .4s}
        #bgBarG{background:#4ede8c} #bgBarR{background:#f06060}
        .bgpanel .fine{font-size:11px;line-height:1.4;color:var(--faint,#5f6773)}
        .bgpanel .dismiss{margin-top:auto;flex:none;background:transparent;border:1px solid var(--line);
            color:var(--muted);border-radius:9px;padding:9px;font-size:13px;width:100%;cursor:pointer}
        .foots .foot{margin-top:0}
        /* Pre-entry reflection: a why-am-I-here pause with concrete alternatives. */
        .r-q{font-size:15px;color:var(--fg);font-weight:600;margin:2px 0 14px;line-height:1.45}
        .chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center}
        .chip{width:auto;padding:9px 13px;font-size:13.5px;font-weight:600;border-radius:999px;
              background:#1c2028;color:var(--fg);border:1px solid var(--line)}
        .chip.sel{background:var(--accent);color:#0a1020;border-color:transparent}
        .r-list{margin-top:16px;text-align:left}
        .r-lead{font-size:13px;color:var(--muted);margin:0 0 10px}
        .r-list ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:9px}
        .r-list li{display:flex;gap:9px;align-items:flex-start;font-size:14px;color:var(--fg);line-height:1.4}
        .r-list li::before{content:"\\25CB";color:var(--accent)}
        .pass{background:var(--go);color:#06120b}
        .cont{background:transparent;color:var(--muted);border:1px solid var(--line)}
        .cont:active{opacity:.7}
    </style>
</head>
<body>
    <canvas id="bp-bg" aria-hidden="true"></canvas>
    <div class="card {{ mood }}">
        <div class="kicker"><span class="dot"></span>{{ overline }}</div>
        {% if countdown %}<div id="cd" class="big" data-secs="{{ countdown }}">·</div>
        {% elif headline %}<div class="big">{{ headline }}</div>{% endif %}
        {% if title %}<h1>{{ title }}</h1>{% endif %}
        <p>{{ message }}</p>
        <div class="actions">
            {% if can_enter and not show_reflect %}
            <form action="/budget/enter?site={{ site }}{% if next_url %}&next={{ next_url|urlencode }}{% endif %}" method="post">
                <button class="enter" type="submit">Enter {{ label }}</button>
            </form>
            {% elif can_enter %}
            <button class="enter" type="button" id="beginBtn">Enter {{ label }}</button>
            <div id="reflect" hidden>
                <p class="r-q">{{ reflect_q }}</p>
                <div class="chips" id="chips"></div>
                <div class="r-list" id="rlist" hidden>
                    <p class="r-lead" id="rlead"></p>
                    <ul id="ritems"></ul>
                </div>
                <div class="actions" id="ractions" hidden>
                    <button class="pass" type="button" id="passBtn">I'll pass — I'm good</button>
                    <form action="/budget/enter?site={{ site }}{% if next_url %}&next={{ next_url|urlencode }}{% endif %}" method="post">
                        <input type="hidden" name="trigger" id="trigField" value="">
                        <button class="cont" type="submit">Continue to {{ label }} anyway</button>
                    </form>
                </div>
            </div>
            {% elif button_text %}
            <button class="blocked" disabled>{{ button_text }}</button>
            {% endif %}
            {% if show_study %}
            <form action="/budget/study?site={{ site }}" method="post">
                <button class="study{% if study_primary %} study-cta{% endif %}" type="submit">{% if study_primary %}Study while you wait{% else %}Study mode{% endif %}</button>
            </form>
            <div class="hint">{% if study_primary %}Turn the break into real progress — locked to the course, no scrolling.{% else %}Locked to the course playlist — no scrolling.{% endif %}</div>
            {% endif %}
        </div>
        <div class="foots"><a class="foot" href="/budget/stats">Usage stats</a><a class="foot" href="/budget/health">Pi health</a><button class="infobtn" id="bgInfoBtn" type="button" aria-label="What is the moving background?">i</button></div>
        <div class="bgpanel" id="bgPanel">
            <h2>The moving background</h2>
            <p>A live picture of the web traffic passing through this box right now.</p>
            <div class="bgbar"><i id="bgBarG"></i><i id="bgBarR"></i></div>
            <div class="key"><span class="sw g"></span><span><b>Encrypted</b> (HTTPS) — scrambled, unreadable. Exactly what you want.</span><span class="pct" id="bgPctG">—</span></div>
            <div class="key"><span class="sw r"></span><span><b>In the clear</b> (DNS + plain HTTP) — readable by anyone in between.</span><span class="pct" id="bgPctR">—</span></div>
            <p class="fine" id="bgRate">measuring…</p>
            <p class="fine"><b>What's counted:</b> web traffic through this box — HTTPS (:443), HTTP (:80)
               and DNS (:53). Not other protocols, and not devices that aren't routed through the box.
               So it's your web browsing, not literally everything.</p>
            <p class="fine">The scrolling lines are a stand-in, not your actual packets — the
               <b>proportions and the volume are real</b>, the hex digits are not. Nothing is recorded
               or sent anywhere.</p>
            <button class="dismiss" id="bgInfoClose" type="button">Got it</button>
        </div>
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
    {% if can_enter and show_reflect %}{% raw %}
    <script>
    (function(){
        var TRIGGERS = [
          { key:"tired", label:"\\uD83D\\uDE34 Tired", lead:"Rest \\u2014 scrolling won't recharge you.",
            items:["Put the phone down, eyes closed for 10 min","Drink a glass of water","If it's late, just go to bed","Step outside for 2 min of air"] },
          { key:"bored", label:"\\uD83D\\uDE10 Bored", lead:"Boredom is a nudge, not an emergency.",
            items:["Text someone you've meant to","5 minutes on one to-do","Open that book or a saved article","Sit with it for 60s \\u2014 it passes"] },
          { key:"stressed", label:"\\uD83D\\uDE30 Stressed", lead:"A feed won't settle this.",
            items:["5 slow breaths \\u2014 in 4, out 6","Write down what's on your mind","Short walk, even around the room","Do one small thing you can control"] },
          { key:"avoiding", label:"\\uD83D\\uDE2C Avoiding something", lead:"What are you putting off?",
            items:["Name the thing you're dodging","Do just its first 2 minutes","Shrink it to one tiny step","Set a 10-min timer and start"] },
          { key:"habit", label:"\\uD83D\\uDD01 Just habit", lead:"You reached without deciding.",
            items:["Did I actually mean to open this?","Put the phone in another room","One slow breath, then choose","Do what you picked it up to avoid"] },
          { key:"need", label:"\\u2705 I actually need it", lead:"Fair enough \\u2014 be deliberate.",
            items:["Get in, get what you need, get out","Hold a rough time limit in mind","Then back to the real thing"] }
        ];
        for(var i=TRIGGERS.length-1;i>0;i--){ var j=Math.floor(Math.random()*(i+1));
          var t=TRIGGERS[i]; TRIGGERS[i]=TRIGGERS[j]; TRIGGERS[j]=t; }   // no fixed positions to memorise
        var reflect=document.getElementById("reflect"); if(!reflect) return;
        var picked="";
        var begin=document.getElementById("beginBtn"), chips=document.getElementById("chips"),
            list=document.getElementById("rlist"), lead=document.getElementById("rlead"),
            items=document.getElementById("ritems"), actions=document.getElementById("ractions"),
            pass=document.getElementById("passBtn");
        begin.addEventListener("click",function(){ begin.style.display="none"; reflect.hidden=false; });
        TRIGGERS.forEach(function(t){
            var b=document.createElement("button"); b.type="button"; b.className="chip"; b.textContent=t.label;
            b.addEventListener("click",function(){
                [].forEach.call(chips.children,function(c){ c.classList.remove("sel"); });
                b.classList.add("sel"); picked=t.key;
                var hid=document.getElementById("trigField"); if(hid) hid.value=t.key;
                lead.textContent=t.lead; items.innerHTML="";
                t.items.forEach(function(it){ var li=document.createElement("li"); li.textContent=it; items.appendChild(li); });
                list.hidden=false; actions.hidden=false;
            });
            chips.appendChild(b);
        });
        pass.addEventListener("click",function(){
            if(picked){ var fd=new FormData(); fd.append("trigger",picked);
              fetch("/budget/reflect",{method:"POST",body:fd,keepalive:true}).catch(function(){}); }
            document.querySelector(".card").innerHTML =
              '<div class="kicker"><span class="dot"></span>Good call</div>'+
              '<h1>Put it down.</h1>'+
              '<p>Part of you already knew. Close this tab and go do the thing \\u2014 future-you says thanks.</p>';
        });
    })();
    </script>
    {% endraw %}{% endif %}
    {% raw %}
    <script>
    (function(){
      var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
      var b=document.getElementById("bgInfoBtn"), p=document.getElementById("bgPanel"),
          x=document.getElementById("bgInfoClose");
      if(b&&p){
        var timer=null;
        function rate(n){ if(n<1024) return n+" B/s"; if(n<1048576) return (n/1024).toFixed(n<10240?1:0)+" KB/s"; return (n/1048576).toFixed(1)+" MB/s"; }
        function refresh(){
          fetch("/budget/feed?t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){return r.json();})
            .then(function(d){
              var e=d.enc||0, u=d.unenc||0, tot=e+u;
              var g=document.getElementById("bgPctG"), rr=document.getElementById("bgPctR"),
                  bg=document.getElementById("bgBarG"), br=document.getElementById("bgBarR"),
                  rt=document.getElementById("bgRate");
              if(!tot){ g.textContent="—"; rr.textContent="—"; bg.style.width="0"; br.style.width="0";
                        rt.textContent="Quiet right now — nothing measurable flowing through."; return; }
              var pg=Math.round(e/tot*100);
              g.textContent=pg+"%"; rr.textContent=(100-pg)+"%";
              bg.style.width=pg+"%"; br.style.width=(100-pg)+"%";
              rt.textContent="Right now: "+rate(e)+" encrypted, "+rate(u)+" in the clear.";
            }).catch(function(){});
        }
        function open_(){ p.classList.add("on"); refresh(); clearInterval(timer); timer=setInterval(refresh,2000); }
        function close_(){ p.classList.remove("on"); clearInterval(timer); timer=null; }
        b.addEventListener("click", function(){ p.classList.contains("on") ? close_() : open_(); });
        if(x) x.addEventListener("click", close_);
        document.addEventListener("keydown", function(e){ if(e.key==="Escape") close_(); });
      }
    })();
    </script>
    <script>
    (function(){
      var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
      var c=document.getElementById("bp-bg"); if(!c||!c.getContext) return;
      var ctx=c.getContext("2d"), W=0, H=0, DPR=Math.min(2, window.devicePixelRatio||1);
      var reduce=window.matchMedia&&window.matchMedia("(prefers-reduced-motion:reduce)").matches;
      var HEX="0123456789abcdef";
      // Scroll speed is CONSTANT and never varies — varying it made a lull look like lag.
      // Traffic level drives DENSITY, packet length and brightness instead: a quiet link is
      // sparse and dim, a busy one fills the screen. Same rhythm, obvious difference.
      var lineH=18, buf=[], sub=0, speed=0.6, redP=0.05, tred=0.05, topFade, botFade;
      var intens=0.25, tintens=0.25;              // eased traffic level, 0..1
      var cols=1, colW=320, maxBytes=12;          // columns fill the full width (was one skinny column)
      function hx(n){ var s=""; for(var i=0;i<n;i++){ s+=HEX[(Math.random()*16)|0]; if(i&1) s+=" "; } return s.trim(); }
      // both are hex; colour is the only tell — green = encrypted (TLS), red = unencrypted (DNS/HTTP)
      function cell(){
        if(Math.random() > 0.20 + intens*0.80) return null;      // gaps when it's quiet
        var red = Math.random() < redP;
        var n = 3 + Math.round(Math.random() * (2 + intens * (maxBytes - 3)));
        return { r: red?1:0, tag: red ? (Math.random()<0.7?"DNS":"HTTP") : "TLS",
                 text: hx(n > maxBytes ? maxBytes : n) };
      }
      function newRow(){ var a=[]; for(var i=0;i<cols;i++) a.push(cell()); return a; }
      function resize(){
        W=c.clientWidth; H=c.clientHeight; c.width=W*DPR; c.height=H*DPR; ctx.setTransform(DPR,0,0,DPR,0,0);
        cols = Math.max(1, Math.round(W / 320));
        colW = W / cols;
        maxBytes = Math.max(4, Math.floor((colW - 64) / 10.8));   // 12px monospace, "aa " per byte
        var N=Math.ceil(H/lineH)+2;
        buf = [];                                    // row shape depends on cols, so rebuild
        while(buf.length<N) buf.push(newRow());
        topFade=ctx.createLinearGradient(0,0,0,64); topFade.addColorStop(0,"#070b0e"); topFade.addColorStop(1,"rgba(7,11,14,0)");
        botFade=ctx.createLinearGradient(0,H-64,0,H); botFade.addColorStop(0,"rgba(7,11,14,0)"); botFade.addColorStop(1,"#070b0e");
      }
      function draw(){
        if(!reduce){ redP+=(tred-redP)*0.05; intens+=(tintens-intens)*0.04; sub+=speed;
          while(sub>=lineH){ sub-=lineH; buf.shift(); buf.push(newRow()); } }
        ctx.fillStyle="#070b0e"; ctx.fillRect(0,0,W,H);
        ctx.font="600 12px ui-monospace,Menlo,monospace"; ctx.textBaseline="alphabetic";
        var br = 0.55 + intens*0.85;                                  // busier = brighter
        var tagA=(0.30*br).toFixed(3), redA=(0.52*br).toFixed(3), grnA=(0.42*br).toFixed(3);
        for(var i=0;i<buf.length;i++){ var row=buf[i], y=i*lineH - sub + lineH;
          for(var k=0;k<row.length;k++){ var cl=row[k]; if(!cl) continue;
            var x=k*colW;
            ctx.fillStyle="rgba(120,132,142,"+tagA+")"; ctx.fillText(cl.tag, x+12, y);
            ctx.fillStyle=cl.r? "rgba(240,96,96,"+redA+")":"rgba(78,222,140,"+grnA+")";
            ctx.fillText(cl.text, x+52, y);
          }
        }
        ctx.fillStyle=topFade; ctx.fillRect(0,0,W,64);
        ctx.fillStyle=botFade; ctx.fillRect(0,H-64,W,64);
        if(!reduce) requestAnimationFrame(draw);
      }
      function poll(){
        fetch("/budget/feed?t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){return r.json();})
          .then(function(d){ if(d){ var tot=(d.enc||0)+(d.unenc||0);
            tred=tot>0? Math.max(0.015, Math.min(0.7, d.unenc/tot)) : 0.02;  // red share = the REAL exposed ratio
            tintens=Math.max(0.12, Math.min(1, Math.log(1+tot/400)/Math.log(3000)));  // fills the screen when busy
          } }).catch(function(){});
      }
      resize(); draw();
      var rzT; window.addEventListener("resize", function(){ clearTimeout(rzT); rzT=setTimeout(resize, 220); });
      if(!reduce){ poll(); setInterval(poll, 2000); }
    })();
    </script>
    {% endraw %}
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
        .why-row{display:flex;align-items:center;gap:10px;margin-top:9px;font-size:13px}
        .why-lab{width:9.5em;flex:none;color:var(--fg)}
        .why-bar{flex:1;height:9px;background:#0e1116;border-radius:5px;overflow:hidden}
        .why-bar i{display:block;height:100%;background:var(--s1);border-radius:5px}
        .why-n{width:5.5em;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums;font-size:12.5px}
        .why-foot{margin-top:14px;padding-top:12px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
        .why-foot b{color:var(--good);font-size:15px}
        .why-empty{color:var(--faint);font-size:12.5px;margin-top:8px;line-height:1.5}
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

    <div class="card" style="margin-bottom:12px">
        <h2>Why you reach for it &mdash; last {{ why.days }} days</h2>
        {% if why.total %}
        {% for row in why.rows %}
        <div class="why-row">
            <span class="why-lab">{{ row.label }}</span>
            <span class="why-bar"><i style="width:{{ row.bar }}%"></i></span>
            <span class="why-n">{{ row.n }}&times; &middot; {{ row.pct }}%</span>
        </div>
        {% endfor %}
        <div class="why-foot">Naming it was enough to stop you <b>{{ why.passes }}</b> of {{ why.total }} times
            ({{ why.rate }}%).</div>
        {% else %}
        <div class="why-empty">Nothing yet. When the gate asks why you're reaching for a site, the
            answer you pick is recorded here &mdash; along with whether naming it was enough to stop you.
            It never leaves this box.</div>
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
    <a class="back" href="/budget/health">Raspberry Pi health &rarr;</a>
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
    # cooldown (the hard wall must not leak away), and at night (the night buffer is a
    # separate, deliberately non-regenerating counter). Wind-down DOES refill: effective_cap()
    # is ramping down, so get_remaining_budget bounds you by that shrinking ceiling — you
    # regen back up toward the time-proportional cap (e.g. ~7.5 min at 10:30), never the full
    # day cap. The refill rate stays the normal one; the ramp alone does the winding-down.
    if pool_has_active_session(p) or r.get(f"cooldown:{p}") or phase() == "night":
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

# The reflection prompt asks *why* you're reaching for the feed. That answer — and
# whether naming it actually stopped you — is the most interesting thing this system can
# know, and it used to live only in the browser and vanish. Recorded per-day, same shape
# as the other logs: "<epoch> <trigger> <action>", self-pruning after ~100 days.
REFLECT_TRIGGERS = {
    "tired":    "Tired",
    "bored":    "Bored",
    "stressed": "Stressed",
    "avoiding": "Avoiding something",
    "habit":    "Just habit",
    "need":     "Actually needed it",
}

# Anything shown identically every time becomes wallpaper — that's how Screen Time's
# "Ignore for today" ends up being tapped without reading. Two defences:
#   · it isn't shown every time (never on your first session of the day, and only on a
#     stable-but-unpredictable ~70% of the rest), so it can't become part of the routine;
#   · the wording rotates and the chips are shuffled, so there's no fixed motor pattern.
# Deliberately seeded rather than live-random, so reloading the page can't reroll it away.
REFLECT_QUESTIONS = [
    "Part of you doesn't want to scroll. What's pulling you in right now?",
    "Before you go in — what are you actually reaching for?",
    "Quick check: what's driving this one?",
    "What's underneath the urge right now?",
    "Honestly — why this, why now?",
    "Something sent you here. What was it?",
]

def reflect_decision(now=None):
    """(show_it, which_question). Skips your first entry of the day, then appears
    unpredictably — the point is that it can't be anticipated and tapped through."""
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    entries = int(r.get(f"entries:{day}") or 0)
    seed = random.Random(f"{day}:{entries}")
    question = REFLECT_QUESTIONS[seed.randrange(len(REFLECT_QUESTIONS))]
    return (entries >= 1 and seed.random() < 0.7), question

def log_reflection(trigger, action, now=None):
    if trigger not in REFLECT_TRIGGERS or action not in ("pass", "enter"):
        return
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    r.rpush(f"reflect:{day}", f"{now:.0f} {trigger} {action}")
    r.expire(f"reflect:{day}", 100 * 86400)

def reflection_summary(days=30, now=None):
    """Per-trigger counts and how often naming it was enough to stop you."""
    now = now if now is not None else time.time()
    rows, passes, total = {}, 0, 0
    for i in range(days):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for raw in r.lrange(f"reflect:{day}", 0, -1):
            parts = raw.split()
            if len(parts) < 3 or parts[1] not in REFLECT_TRIGGERS:
                continue
            t, a = parts[1], parts[2]
            d = rows.setdefault(t, {"key": t, "label": REFLECT_TRIGGERS[t], "n": 0, "passed": 0})
            d["n"] += 1
            total += 1
            if a == "pass":
                d["passed"] += 1
                passes += 1
    out = sorted(rows.values(), key=lambda d: -d["n"])
    top = out[0]["n"] if out else 0
    for d in out:
        d["pct"] = round(100 * d["n"] / total) if total else 0
        d["bar"] = round(100 * d["n"] / top) if top else 0
    return {"rows": out, "total": total, "passes": passes,
            "rate": round(100 * passes / total) if total else 0, "days": days}

def log_soft_pause(site, now=None):
    """Log a per-site SOFT pause: a site hit its own cap while the shared bucket still had
    room, so the session ended with NO hard cooldown. Feeds the cluster brake below
    (recent_soft_pause_count reads this log). Each entry "<epoch> <site>"; per-day key,
    self-prunes after ~100 days."""
    now = now if now is not None else time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    r.rpush(f"soft_pauses:{day}", f"{now:.0f} {site}")
    r.expire(f"soft_pauses:{day}", 100 * 86400)

def recent_soft_pause_count(site, now):
    """How many soft pauses for `site` fell inside the trailing CLUSTER_WINDOW (through now).
    Scans today + yesterday since the window can straddle midnight."""
    cutoff = now - CLUSTER_WINDOW
    count = 0
    for i in (1, 0):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        for raw in r.lrange(f"soft_pauses:{day}", 0, -1):
            parts = raw.split()
            try:
                ts = float(parts[0])
            except (ValueError, IndexError):
                continue
            # No upper bound: entries are stored rounded (f"{now:.0f}"), so the just-logged
            # pause can round to now+1 — an `<= now` check would drop it ~half the time.
            if len(parts) > 1 and parts[1] == site and ts >= cutoff:
                count += 1
    return count

def maybe_cluster_cooldown(site, now):
    """Called right after logging a soft pause. If it's the CLUSTER_THRESHOLD-th cap-hit for
    this site within the rolling window, set a short, site-specific cooldown to break the
    loop (auto-expires via TTL). Returns True if it fired."""
    if recent_soft_pause_count(site, now) >= CLUSTER_THRESHOLD:
        r.setex(f"soft_cd:{site}", CLUSTER_COOLDOWN_SECONDS, f"{now:.0f}")
        return True
    return False

def get_soft_cd_remaining(site):
    """Seconds left on a site's cluster cooldown (0 if none). TTL-backed, so it just melts away."""
    ttl = r.ttl(f"soft_cd:{site}")
    return ttl if ttl and ttl > 0 else 0

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
                countdown=0, show_study=False, study_primary=False, refresh=0, next_url="",
                show_reflect=False, reflect_q=""):
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
        refresh=refresh, next_url=next_url,
        show_reflect=show_reflect, reflect_q=reflect_q)

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
            nshow, nq = reflect_decision()
            return render_gate(site, label, overline=f"{label} · Night mode", mood="sleep",
                headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
                show_reflect=nshow, reflect_q=nq,
                message=f"A small buffer, then closed till {NIGHT_END_HOUR} AM. No refill overnight — spend it wisely.")
        # wind-down
        if remaining <= 0:
            return render_gate(site, label, overline=f"{label} · Winding down", mood="wait",
                countdown=secs_until_hour(NIGHT_START_HOUR), show_study=study_ok,
                title="Paused for now",
                message="Easing toward bedtime — back briefly at night mode, then closed. Time for something calmer.")
        wshow, wq = reflect_decision()
        return render_gate(site, label, overline=f"{label} · Winding down", mood="wait",
            headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
            show_reflect=wshow, reflect_q=wq,
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

    # One site looping (repeated cap-hits in a short window) -> a short site-specific breather.
    soft_cd = get_soft_cd_remaining(site)
    if soft_cd > 0:
        return render_gate(site, label, overline=f"{label} · Short break", mood="wait",
            countdown=soft_cd, show_study=study_ok, study_primary=study_ok,
            title="Take a breather",
            message=f"You've hit your {label} cap a few times in a row — a short pause to break the loop, then it reopens.")

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
    show_reflect, reflect_q = reflect_decision()
    return render_gate(site, label, overline=f"{label} · Time left", mood="go",
        headline=clock(remaining), can_enter=True, show_study=study_ok, next_url=nxt,
        show_reflect=show_reflect, reflect_q=reflect_q,
        message="Foreground time only — the clock ticks while you're looking. Make it count.")

@app.route('/reflect', methods=['POST'])
def reflect():
    # Fired when the reflection prompt talks you out of it. (The other outcome is
    # recorded by /enter, which the "continue anyway" button posts to.)
    log_reflection(request.form.get("trigger", ""), "pass")
    return jsonify({"status": "ok"})

@app.route('/enter', methods=['POST'])
def enter():
    site = resolve_site(request.args.get("site"))
    remaining = get_remaining_budget(site)
    cooldown = get_cooldown_remaining(site)

    if remaining <= 0 or cooldown > 0 or get_soft_cd_remaining(site) > 0:
        return redirect(f'/budget?site={site}')

    log_reflection(request.form.get("trigger", ""), "enter")
    day = time.strftime("%Y-%m-%d")
    r.incr(f"entries:{day}")          # drives "skip the prompt on your first session"
    r.expire(f"entries:{day}", 7 * 86400)

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
                        log_soft_pause(site, now)
                        maybe_cluster_cooldown(site, now)   # short brake if this site is looping
                        return jsonify({"status": "blocked", "remaining": 0}), 403
                # Wind-down: shrinking cap on the day bucket, no cooldown.
                elif spent >= effective_cap(site):
                    r.delete(f"active_token:{site}")
                    r.delete(f"session:{token}")
                    return jsonify({"status": "blocked", "remaining": 0}), 403

    r.set(f"last_heartbeat:{p}", now)
    remaining = get_remaining_budget(site)
    return jsonify({"status": "ok", "remaining": int(remaining), "phase": phase()})

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

    why = reflection_summary()
    return render_template_string(STATS_PAGE, why=why,
        days=days, today_min=today_min, week_avg=week_avg,
        trend=trend, trend_cls=trend_cls, live_line=live_line, stale=stale, cd=cd)

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

# ---------------------------------------------------------------------------
# Pi health monitor: read live system metrics straight off /proc, /sys and a
# couple of quick shell-outs, and draw a line-art board whose ethernet port /
# SoC / power connector reflect real state. Every reader is wrapped in _try so
# a missing file on a non-Pi host degrades to a dash instead of a 500.
# ---------------------------------------------------------------------------

_TEMP_HIST = deque(maxlen=90)   # recent CPU temps for the sparkline (~6 min at 4s polls)
_NET_PREV = {}                  # iface -> (monotonic_t, rx_bytes, tx_bytes) for throughput
_CPU_PREV = {}                  # {"v": /proc/stat snapshot} — CPU% is measured between calls
_HEALTH_CACHE = {}              # {"t": monotonic, "d": payload} — collapses concurrent pollers
_FEED_PREV = {}                 # {"v": (monotonic_t, enc_bytes, unenc_bytes)} for the packet feed

def ui_token():
    """A secret the monitoring pages embed so their own polling can be told apart from a
    script on a gated site. Any page on reddit.com can call /budget/* same-origin, and
    Sec-Fetch headers can't distinguish our gate page from Reddit's own — but a foreign
    script can't read this token, because the pages carrying it can only be fetched by a
    real navigation (enforced in addon.py). Persisted so it survives a restart."""
    tok = r.get("ui_token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        r.set("ui_token", tok)
    return tok

@app.context_processor
def _inject_ui_token():
    return {"ui_tok": _try(ui_token, "")}

def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default

def _first_line(path):
    with open(path) as f:
        return f.readline().strip()

def _cpu_snap():
    cpu = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            p = line.split()
            v = list(map(int, p[1:]))
            cpu[p[0]] = (v[3] + v[4], sum(v))   # idle+iowait, total
    return cpu

def _cpu_stats():
    """Aggregate + per-core busy %, measured BETWEEN calls rather than by sleeping.

    This used to sleep 0.2s to take its own delta, which held one of waitress's few
    worker threads for the whole request — a handful of concurrent /health hits could
    starve the pool and stall the gate (and silently stop heartbeat charging). Now we
    diff against the previous snapshot, so a request never blocks; the poller's ~4s
    cadence also gives a steadier reading than a 0.2s window. First call reads 0.
    """
    a, b = _CPU_PREV.get("v"), _cpu_snap()
    _CPU_PREV["v"] = b
    def pct(name):
        if not a:
            return 0.0
        i1, t1 = a.get(name, (0, 0))
        i2, t2 = b.get(name, (0, 0))
        dt = t2 - t1
        return round(max(0.0, min(100.0, 100.0 * (1 - (i2 - i1) / dt))), 1) if dt > 0 else 0.0
    cores = sorted((k for k in b if k != "cpu" and k.startswith("cpu")),
                   key=lambda k: int(k[3:]))
    return pct("cpu"), [pct(c) for c in cores]

def _loadavg():
    return [float(x) for x in _first_line("/proc/loadavg").split()[:3]]

def _temp_c():
    return round(int(_first_line("/sys/class/thermal/thermal_zone0/temp")) / 1000.0, 1)

def _mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            info[k] = int(rest.strip().split()[0])   # kB
    total = info["MemTotal"]
    used = total - info.get("MemAvailable", info.get("MemFree", 0))
    return {"used_mb": round(used / 1024), "total_mb": round(total / 1024),
            "pct": round(100 * used / total, 1)}

def _disk(path="/"):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = total - st.f_bavail * st.f_frsize
    return {"used_gb": round(used / 1e9, 1), "total_gb": round(total / 1e9, 1),
            "pct": round(100 * used / total)}

def _uptime():
    secs = float(_first_line("/proc/uptime").split()[0])
    d, h, m = int(secs // 86400), int(secs % 86400 // 3600), int(secs % 3600 // 60)
    return f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")

def _iface(name):
    base = f"/sys/class/net/{name}"
    if not os.path.exists(base):
        return None
    state = _try(lambda: _first_line(f"{base}/operstate"), "unknown")
    speed = _try(lambda: int(_first_line(f"{base}/speed")), None)
    if speed is not None and speed < 0:
        speed = None
    # Real NICs report up/down honestly; tunnels (tailscale/wg) sit at "unknown"
    # while perfectly up, so treat anything but an explicit "down" as connected.
    up = state != "down" if name.startswith(("tailscale", "wg", "tun")) else state == "up"
    # Throughput: bytes/sec since the previous read (kept in _NET_PREV per interface).
    rx = _try(lambda: int(_first_line(f"{base}/statistics/rx_bytes")))
    tx = _try(lambda: int(_first_line(f"{base}/statistics/tx_bytes")))
    rx_bps = tx_bps = None
    if rx is not None and tx is not None:
        now = time.monotonic()
        prev = _NET_PREV.get(name)
        if prev and now - prev[0] >= 0.5:   # skip tiny gaps so the rate isn't a spike
            dt = now - prev[0]
            rx_bps = max(0, round((rx - prev[1]) / dt))
            tx_bps = max(0, round((tx - prev[2]) / dt))
        _NET_PREV[name] = (now, rx, tx)
    return {"up": up, "state": state, "speed": speed, "rx_bps": rx_bps, "tx_bps": tx_bps}

def _services(names):
    def one(s):
        return subprocess.run(["systemctl", "is-active", s],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    return {s: _try(lambda s=s: one(s), "unknown") for s in names}

def _power():
    raw = _try(lambda: subprocess.run(["vcgencmd", "get_throttled"],
              capture_output=True, text=True, timeout=2).stdout.strip(), "")
    val = _try(lambda: int(raw.split("=")[1], 16), 0) if "=" in raw else 0
    return {"ok": val == 0,
            "under_voltage_now": bool(val & 0x1),
            "throttled_now": bool(val & 0x4),
            "under_voltage_ever": bool(val & 0x10000)}

def _temp_class(t):
    # Bands set against what a Pi 4 actually does, not what feels hot to a person:
    # it soft-throttles at 80C and hard-throttles at 85C, and 40-60C is ordinary idle.
    # The old 55/70 split painted a perfectly healthy board amber, which is a false alarm.
    if t is None:
        return "off"
    return "cool" if t <= 65 else ("warm" if t <= 78 else "hot")

def _pct_class(p):
    # Shared green/amber/red for utilisation gauges (memory, and the RAM chip glow).
    return "cool" if p < 70 else ("warm" if p < 85 else "hot")

def _spark_points(hist, w=100, h=32, lo=30, hi=85):
    # Map a temperature history to an SVG polyline "x,y x,y ..." over a w×h box.
    pts = list(hist)[-40:]
    if not pts:
        return ""
    if len(pts) == 1:
        pts = pts * 2
    n = len(pts)
    out = []
    for i, t in enumerate(pts):
        x = w * i / (n - 1)
        y = h - max(0.0, min(1.0, (t - lo) / (hi - lo))) * h
        out.append(f"{x:.1f},{y:.1f}")
    return " ".join(out)

def boot_watch():
    """Notice that the box has rebooted, and remember it until you say it was you.

    You cannot detect the SD card being pulled: the card IS the root filesystem, so the
    moment it leaves, the code that would raise the alarm is unreadable. But nobody yanks
    a card from a running Pi — they power it down, copy it, and put it back. That leaves a
    trace: a boot this box can't account for.

    /proc/sys/kernel/random/boot_id is regenerated on every boot, so a change means the
    machine restarted — not merely that a service was restarted.

    Honest limit: anyone who takes the card can also edit this, or clear the flag. It's
    tamper-EVIDENCE for the careless, not tamper-proofing. A sticker across the SD slot is
    a better detector, and costs nothing.
    """
    bid = _try(lambda: _first_line("/proc/sys/kernel/random/boot_id"))
    if not bid:
        return None
    if r.get("last_boot_id") != bid:
        first_ever = r.get("last_boot_id") is None
        r.set("last_boot_id", bid)
        if not first_ever:                       # don't cry wolf on the very first run
            now = time.time()
            r.rpush("boot_events", f"{now:.0f}")
            r.ltrim("boot_events", -50, -1)
            r.set("unacked_boot", f"{now:.0f}")
    return r.get("unacked_boot")

@app.route('/boot-ack', methods=['POST'])
def boot_ack():
    r.delete("unacked_boot")
    return redirect('/health')

def collect_health(max_age=2.0):
    """Snapshot of the box. Cached briefly: the page polls every 4s and several tabs (or a
    hostile same-origin script) would otherwise each spawn ~5 `systemctl` subprocesses per
    hit. Serving a <=2s-old payload keeps the worker threads free for the gate itself."""
    hit = _HEALTH_CACHE.get("d")
    if hit is not None and time.monotonic() - _HEALTH_CACHE.get("t", 0) < max_age:
        return hit
    svc = _services(["cooldown-app", "cooldown-proxy", "cooldown-redirect", "redis-server", "tailscaled"])
    agg, per_core = _try(_cpu_stats, (0.0, []))
    temp = _try(_temp_c)
    if temp is not None:
        _TEMP_HIST.append(temp)
    out = {
        "model": _try(lambda: _first_line("/proc/device-tree/model").replace("\x00", ""), "Raspberry Pi"),
        "cpu": {"pct": agg, "per_core": per_core, "load": _try(_loadavg, [0, 0, 0]), "cores": os.cpu_count() or 1},
        "temp_c": temp,
        "temp_hist": list(_TEMP_HIST),
        "mem": _try(_mem, {"used_mb": 0, "total_mb": 0, "pct": 0}),
        "disk": _try(_disk, {"used_gb": 0, "total_gb": 0, "pct": 0}),
        "uptime": _try(_uptime, "?"),
        "net": {n: _iface(n) for n in ("eth0", "tailscale0", "wlan0")},
        "power": _try(_power, {"ok": True}),
        "services": svc,
        "services_ok": all(v == "active" for v in svc.values()),
    }
    _HEALTH_CACHE.update(t=time.monotonic(), d=out)
    return out

HEALTH_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Pi Health · Countdown</title>
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --faint:#5f6773; --go:#3ecf7c; --wait:#f0a63a; --bad:#e5484d;
        }
        *{box-sizing:border-box}
        body{
            margin:0;background:radial-gradient(1100px 560px at 50% -10%,#161a22,var(--bg));
            color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;padding:26px 16px max(26px,env(safe-area-inset-bottom));
            display:flex;justify-content:center;
        }
        .wrap{width:100%;max-width:560px}
        .kicker{display:flex;align-items:center;gap:8px;justify-content:center;font-size:11.5px;
            font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:18px}
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--go)}
        .board{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 10px 6px;margin-bottom:12px}
        .board svg{display:block;width:100%;height:auto}
        .caption{text-align:center;font-size:11.5px;color:var(--faint);margin:2px 0 6px}
        /* board line-art */
        .pcb{fill:#0c1a12;stroke:#2f5d43;stroke-width:1.5}
        .hole{fill:var(--bg);stroke:#2f5d43;stroke-width:1.2}
        .chip{fill:#161b22;stroke:#3a4150;stroke-width:1.2}
        .port{fill:#0f1319;stroke:#3a4150;stroke-width:1.2}
        .port.dark{fill:#090b0e}
        .pin{stroke:#3a4150;stroke-width:2;stroke-dasharray:2 4}
        .lbl{fill:var(--muted);font:600 9px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .lbl.r{text-anchor:end}
        .ctext{fill:#6b7686;font:700 9px ui-monospace,Menlo,monospace;text-anchor:middle}
        .led{fill:#2a2f3a}
        /* ethernet — the star: green when the link is up */
        #eth .jack{fill:#0f1319;stroke:#3a4150;stroke-width:1.5;transition:.4s}
        #eth.on .jack{fill:#123522;stroke:var(--go)}
        #eth.on .lbl{fill:var(--go)}
        #eth.on .led.link{fill:var(--go)}
        #eth.on .led.act{fill:var(--go);animation:blink 1.5s steps(1) infinite}
        @keyframes blink{50%{opacity:.2}}
        /* ethernet unplugged: red + an occasional shake */
        #eth.down .jack{fill:#2e1414;stroke:var(--bad)}
        #eth.down .lbl{fill:var(--bad)} #eth.down .led{fill:var(--bad)}
        #eth.down{animation:shake 1.8s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
        @keyframes shake{0%,86%,100%{transform:translateX(0)}89%{transform:translateX(-2.5px)}92%{transform:translateX(2.5px)}95%{transform:translateX(-1.5px)}}
        @media (prefers-reduced-motion:reduce){ #eth.on .led.act,#eth.down{animation:none} }
        /* SoC tinted by temperature, RAM by memory use */
        #soc,#ram{transition:.5s}
        #soc.cool,#ram.cool{fill:#123522;stroke:var(--go)}
        #soc.warm,#ram.warm{fill:#2a2412;stroke:var(--wait)}
        #soc.hot,#ram.hot{fill:#2e1414;stroke:var(--bad)}
        .divln{stroke:#2a2f3a;stroke-width:1}
        /* power connector */
        #pwr{fill:#3a4150;transition:.4s} #pwr.on{fill:var(--go)} #pwr.bad{fill:var(--wait)}
        /* metric cards */
        .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
        @media (max-width:460px){.grid{grid-template-columns:repeat(2,1fr)}}
        .metric{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 13px 12px}
        .mtop{display:flex;align-items:baseline;justify-content:space-between}
        .mk{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
        .mv{font-size:20px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
        .metric.cool .mv{color:var(--go)} .metric.warm .mv{color:var(--wait)} .metric.hot .mv{color:var(--bad)}
        .mbig{font-size:19px;font-weight:700;margin-top:6px;letter-spacing:-.4px}
        .bar{background:#0e1116;border-radius:6px;height:7px;overflow:hidden;margin-top:9px}
        .bar i{display:block;height:100%;width:0;background:var(--go);border-radius:6px;transition:width .5s,background .5s}
        .metric.warm .bar i{background:var(--wait)} .metric.hot .bar i{background:var(--bad)}
        .msub{font-size:11.5px;color:var(--faint);margin-top:8px}
        .cores{display:flex;gap:3px;align-items:flex-end;height:26px;margin-top:9px}
        .core{flex:1;height:100%;background:#0e1116;border-radius:2px;display:flex;align-items:flex-end;overflow:hidden}
        .core i{width:100%;background:var(--go);border-radius:2px;transition:height .4s}
        .spark{display:block;width:100%;height:32px;margin-top:9px}
        .spark polyline{stroke:var(--go);stroke-width:2;fill:none;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
        .metric.warm .spark polyline{stroke:var(--wait)} .metric.hot .spark polyline{stroke:var(--bad)}
        .thru{margin-top:11px;font-size:11.5px;color:var(--faint);font-variant-numeric:tabular-nums;display:flex;gap:16px}
        .thru b{color:var(--fg);font-weight:600}
        .net{margin-top:9px;display:flex;flex-direction:column;gap:7px}
        .nrow{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--fg)}
        .nstate,.net .nstate{margin-left:auto;color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
        .ndot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
        .ndot.on{background:var(--go);box-shadow:0 0 0 3px rgba(62,207,124,.15)}
        .ndot.off{background:#3a3f4a}
        .svc{display:flex;flex-wrap:wrap;gap:7px;justify-content:center;margin-top:12px}
        .pill{font-size:11px;font-weight:600;padding:6px 10px;border-radius:999px;border:1px solid var(--line);
            display:flex;align-items:center;gap:6px;color:var(--muted)}
        .pill::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--faint)}
        .pill.ok{color:#cfe9d8}.pill.ok::before{background:var(--go)}
        .pill.bad{color:#f0c9c9;border-color:#3a2222}.pill.bad::before{background:var(--bad)}
        .bootwarn{background:rgba(46,20,20,.86);border:1px solid var(--bad);border-radius:14px;
            padding:15px 16px;margin-bottom:12px}
        .bw-t{font-size:14.5px;font-weight:700;color:#ffd9d9}
        .bw-p{font-size:12.5px;line-height:1.5;color:#e0b9b9;margin-top:6px}
        .bw-b{margin-top:11px;width:100%;padding:10px;border-radius:9px;border:1px solid #5c2c2c;
            background:transparent;color:#e0b9b9;font-size:13px;cursor:pointer}
        .foot{display:flex;gap:16px;justify-content:center;margin-top:20px}
        .foot a{font-size:12.5px;color:var(--faint);text-decoration:none}
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>{{ d.model }}</div>
    {% if boot_alert %}
    <div class="bootwarn">
        <div class="bw-t">⚠️ This box restarted on {{ boot_alert }}</div>
        <div class="bw-p">If that wasn't you — no power cut, no reboot you asked for — then
            someone had physical access, and the SD card holds the certificate your devices
            trust. See <b>RECOVERY.md</b>.</div>
        <form action="/budget/boot-ack" method="post">
            <button class="bw-b" type="submit">That was me — dismiss</button>
        </form>
    </div>
    {% endif %}

    <div class="board">
      <svg viewBox="0 0 380 250" role="img" aria-label="Raspberry Pi board">
        <rect class="pcb" x="18" y="34" width="344" height="182" rx="12"/>
        <circle class="hole" cx="32" cy="48" r="5"/>
        <circle class="hole" cx="32" cy="202" r="5"/>
        <circle class="hole" cx="348" cy="202" r="5"/>
        <!-- 40-pin GPIO header -->
        <rect class="port dark" x="46" y="40" width="250" height="15" rx="2"/>
        <line class="pin" x1="52" y1="44.5" x2="290" y2="44.5"/>
        <line class="pin" x1="52" y1="50.5" x2="290" y2="50.5"/>
        <!-- RAM + SoC -->
        <rect id="ram" class="chip {{ ram_class }}" x="60" y="120" width="52" height="44" rx="3"/>
        <text class="ctext" x="86" y="145">RAM</text>
        <rect id="soc" class="chip {{ tclass }}" x="150" y="108" width="72" height="66" rx="6"/>
        <text class="ctext" x="186" y="138">BCM</text>
        <text class="ctext" x="186" y="150">2711</text>
        <!-- Ethernet (the highlight) -->
        <g id="eth" class="{{ eth_state }}">
          <rect class="jack" x="300" y="52" width="68" height="46" rx="4"/>
          <rect class="port dark" x="307" y="62" width="54" height="28" rx="2"/>
          <circle class="led link" cx="307" cy="57" r="3"/>
          <circle class="led act" cx="361" cy="57" r="3"/>
          <text class="lbl r" x="294" y="70">ETH</text>
          <text class="lbl r" x="294" y="83">1 GbE</text>
        </g>
        <!-- USB (two stacked ports each) -->
        <rect class="port dark" x="300" y="110" width="62" height="28" rx="3"/>
        <line class="divln" x1="301" y1="124" x2="361" y2="124"/>
        <text class="lbl r" x="294" y="128">USB 3.0</text>
        <rect class="port dark" x="300" y="144" width="62" height="28" rx="3"/>
        <line class="divln" x1="301" y1="158" x2="361" y2="158"/>
        <text class="lbl r" x="294" y="162">USB 2.0</text>
        <!-- bottom edge: power, HDMI, AV -->
        <text class="lbl" x="83" y="206">PWR</text>
        <rect id="pwr" class="{{ pwr_class }}" x="70" y="210" width="26" height="11" rx="2"/>
        <text class="lbl" x="146" y="206">HDMI</text>
        <rect class="port dark" x="120" y="210" width="22" height="11" rx="2"/>
        <rect class="port dark" x="150" y="210" width="22" height="11" rx="2"/>
        <rect class="port dark" x="196" y="210" width="16" height="11" rx="2"/>
        <text class="lbl" x="204" y="206">AV</text>
        <!-- microSD (left edge) -->
        <rect class="port dark" x="8" y="150" width="12" height="34" rx="2"/>
        <text class="lbl" x="14" y="145">SD</text>
      </svg>
      <div class="caption">Ethernet glows green when the link is up · SoC tints with temperature</div>
    </div>

    <div class="grid">
      <div class="metric" id="cpuCard">
        <div class="mtop"><span class="mk">CPU</span><span class="mv" id="cpuPct">{{ d.cpu.pct }}%</span></div>
        <div class="cores" id="cpuCores">
          {% for c in d.cpu.per_core %}<div class="core"><i style="height:{{ c }}%"></i></div>{% endfor %}
        </div>
        <div class="msub">load <span id="cpuLoad">{{ '%.2f'|format(d.cpu.load[0]) }}</span> · {{ d.cpu.cores }} cores</div>
      </div>
      <div class="metric {{ tclass }}" id="tempCard">
        <div class="mtop"><span class="mk">Temp</span><span class="mv" id="temp">{% if d.temp_c is not none %}{{ d.temp_c }}&deg;C{% else %}&mdash;{% endif %}</span></div>
        <svg class="spark" viewBox="0 0 100 32" preserveAspectRatio="none"><polyline id="tempLine" points="{{ spark_points }}"/></svg>
        <div class="msub">throttling <span id="throt">{{ 'none' if d.power.ok else 'ACTIVE' }}</span> &middot; limit 80&deg;C</div>
      </div>
      <div class="metric {{ mcard }}" id="memCard">
        <div class="mtop"><span class="mk">Memory</span><span class="mv" id="memPct">{{ d.mem.pct }}%</span></div>
        <div class="bar"><i id="memBar" style="width:{{ d.mem.pct }}%"></i></div>
        <div class="msub"><span id="memText">{{ d.mem.used_mb }} / {{ d.mem.total_mb }} MB</span></div>
      </div>
      <div class="metric {{ dcard }}" id="diskCard">
        <div class="mtop"><span class="mk">Disk</span><span class="mv" id="diskPct">{{ d.disk.pct }}%</span></div>
        <div class="bar"><i id="diskBar" style="width:{{ d.disk.pct }}%"></i></div>
        <div class="msub"><span id="diskText">{{ d.disk.used_gb }} / {{ d.disk.total_gb }} GB</span></div>
      </div>
      <div class="metric">
        <div class="mtop"><span class="mk">Uptime</span></div>
        <div class="mbig" id="uptime">{{ d.uptime }}</div>
        <div class="msub">since boot</div>
      </div>
      <div class="metric">
        <div class="mtop"><span class="mk">Network</span></div>
        <div class="net">
          <div class="nrow"><span class="ndot {{ 'on' if d.net.eth0 and d.net.eth0.up else 'off' }}" id="ndot_eth0"></span>Ethernet<span class="nstate" id="st_eth0">{% if d.net.eth0 and d.net.eth0.up %}Up{% if d.net.eth0.speed %} · {{ d.net.eth0.speed }}M{% endif %}{% else %}Down{% endif %}</span></div>
          <div class="nrow"><span class="ndot {{ 'on' if d.net.tailscale0 and d.net.tailscale0.up else 'off' }}" id="ndot_tailscale0"></span>Tailscale<span class="nstate" id="st_tailscale0">{% if d.net.tailscale0 and d.net.tailscale0.up %}Up{% else %}Down{% endif %}</span></div>
          <div class="nrow"><span class="ndot {{ 'on' if d.net.wlan0 and d.net.wlan0.up else 'off' }}" id="ndot_wlan0"></span>Wi-Fi<span class="nstate" id="st_wlan0">{% if d.net.wlan0 and d.net.wlan0.up %}Up{% else %}Off{% endif %}</span></div>
        </div>
        <div class="thru">eth0<span id="rx">&darr; &mdash;</span><span id="tx">&uarr; &mdash;</span></div>
      </div>
    </div>

    <div class="svc">
      {% for name, state in d.services.items() %}
      <span class="pill {{ 'ok' if state=='active' else 'bad' }}" id="svc_{{ name }}">{{ name }}</span>
      {% endfor %}
    </div>

    <div class="foot"><a href="/budget/stats">&larr; Usage stats</a><a href="/budget/devices">Devices</a><a href="/budget/health">Refresh</a></div>
</div>
{% raw %}
<script>
(function(){
    var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
    function $(id){ return document.getElementById(id); }
    function set(id,t){ var e=$(id); if(e) e.textContent=t; }
    function width(id,p){ var e=$(id); if(e) e.style.width=Math.max(0,Math.min(100,p))+"%"; }
    function tclass(t){ return t==null?"off":(t<=65?"cool":(t<=78?"warm":"hot")); }   // 80C = throttle
    function net(o){ if(!o) return "n/a"; if(!o.up) return "Down"; return o.speed?("Up · "+o.speed+"M"):"Up"; }
    function dot(id,on){ var e=$(id); if(e) e.className="ndot "+(on?"on":"off"); }

    function pclass(p){ return p<70?"cool":(p<85?"warm":"hot"); }
    function cardcls(p){ return p<70?"":(p<85?"warm":"hot"); }
    function rate(b){ if(b==null) return "—"; if(b<1024) return b+" B/s"; if(b<1048576) return (b/1024).toFixed(b<10240?1:0)+" KB/s"; return (b/1048576).toFixed(1)+" MB/s"; }
    function spark(hist){
        var pts=(hist||[]).slice(-40); if(!pts.length) return "";
        if(pts.length===1){ pts=[pts[0],pts[0]]; }
        var n=pts.length,lo=30,hi=85,w=100,h=32,out=[];
        for(var i=0;i<n;i++){ var x=w*i/(n-1),y=h-Math.max(0,Math.min(1,(pts[i]-lo)/(hi-lo)))*h; out.push(x.toFixed(1)+","+y.toFixed(1)); }
        return out.join(" ");
    }
    function update(d){
        set("cpuPct", d.cpu.pct+"%"); set("cpuLoad", d.cpu.load[0].toFixed(2));
        var cores=$("cpuCores");
        if(cores && d.cpu.per_core){
            if(cores.children.length!==d.cpu.per_core.length)
                cores.innerHTML=d.cpu.per_core.map(function(){ return '<div class="core"><i></i></div>'; }).join("");
            d.cpu.per_core.forEach(function(c,i){ var b=cores.children[i]; if(b) b.firstElementChild.style.height=Math.max(0,Math.min(100,c))+"%"; });
        }
        if(d.temp_c!=null){
            set("temp", d.temp_c+"°C");
            var soc=$("soc"); if(soc) soc.setAttribute("class","chip "+tclass(d.temp_c));
            var tc=$("tempCard"); if(tc) tc.className="metric "+tclass(d.temp_c);
        }
        var line=$("tempLine"); if(line && d.temp_hist) line.setAttribute("points", spark(d.temp_hist));
        set("throt", d.power && d.power.ok ? "none" : "ACTIVE");
        set("memPct", d.mem.pct+"%"); width("memBar", d.mem.pct);
        set("memText", d.mem.used_mb+" / "+d.mem.total_mb+" MB");
        var ram=$("ram"); if(ram) ram.setAttribute("class","chip "+pclass(d.mem.pct));
        var mc=$("memCard"); if(mc) mc.className="metric "+cardcls(d.mem.pct);
        set("diskPct", d.disk.pct+"%"); width("diskBar", d.disk.pct);
        set("diskText", d.disk.used_gb+" / "+d.disk.total_gb+" GB");
        var dc=$("diskCard"); if(dc) dc.className="metric "+cardcls(d.disk.pct);
        set("uptime", d.uptime);
        var e=d.net.eth0, ts=d.net.tailscale0, wl=d.net.wlan0;
        var eth=$("eth"); if(eth) eth.setAttribute("class", e&&e.up?"on":(e?"down":"off"));
        dot("ndot_eth0", e&&e.up); set("st_eth0", net(e));
        dot("ndot_tailscale0", ts&&ts.up); set("st_tailscale0", ts&&ts.up?"Up":"Down");
        dot("ndot_wlan0", wl&&wl.up); set("st_wlan0", wl&&wl.up?"Up":"Off");
        if(e){ var rx=$("rx"), tx=$("tx"); if(rx) rx.innerHTML="↓ <b>"+rate(e.rx_bps)+"</b>"; if(tx) tx.innerHTML="↑ <b>"+rate(e.tx_bps)+"</b>"; }
        var pwr=$("pwr"); if(pwr) pwr.setAttribute("class", d.power&&d.power.ok?"on":"bad");
        Object.keys(d.services||{}).forEach(function(s){ var el=$("svc_"+s); if(el) el.className="pill "+(d.services[s]==="active"?"ok":"bad"); });
    }
    function poll(){ fetch("/budget/health?fmt=json&t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){ return r.json(); }).then(update).catch(function(){}); }
    setInterval(poll, 4000);
    document.addEventListener("visibilitychange", function(){ if(!document.hidden) poll(); });
    poll();
})();
</script>
{% endraw %}
</body>
</html>
"""

@app.route('/health')
def health():
    d = collect_health()
    if request.args.get("fmt") == "json":
        return jsonify(d)
    eth = d["net"].get("eth0")
    eth_state = "on" if (eth and eth["up"]) else ("down" if eth else "off")
    mem_pct, disk_pct = d["mem"].get("pct", 0), d["disk"].get("pct", 0)
    # Cards stay neutral until high, then warn — but the RAM chip always glows (cool too).
    card = lambda p: "" if p < 70 else ("warm" if p < 85 else "hot")
    ts = _try(boot_watch)
    boot_alert = time.strftime("%a %-d %b, %-I:%M %p", time.localtime(float(ts))) if ts else None
    return render_template_string(
        HEALTH_PAGE, d=d, boot_alert=boot_alert,
        tclass=_temp_class(d["temp_c"]),
        eth_state=eth_state,
        ram_class=_pct_class(mem_pct),
        mcard=card(mem_pct), dcard=card(disk_pct),
        pwr_class="on" if d["power"].get("ok") else "bad",
        spark_points=_spark_points(d["temp_hist"]))

def _acct_counters():
    """Encrypted vs unencrypted bytes seen by the Pi, from the observational TRAFFIC_ACCT
    chain (mangle table): :443 = TLS (encrypted), :80 + :53 = HTTP + DNS (plaintext).

    STRICTLY READ-ONLY. The chain is created by the root-run redirect script at boot, not
    here: an earlier version rebuilt it inline, which meant an unauthenticated GET could
    trigger privileged firewall *writes* and spawn ~10 sudo processes per request. The app
    now only ever runs this one read, which is all `sudoers.d/budget-proxy` permits."""
    out = subprocess.run(["sudo", "-n", "iptables", "-t", "mangle", "-nvxL", "TRAFFIC_ACCT"],
                         capture_output=True, text=True, timeout=3).stdout
    enc = unenc = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 8 or not parts[1].isdigit():
            continue
        b = int(parts[1])                       # column 2 = bytes
        if "dpt:443" in line:
            enc += b
        elif "dpt:80" in line or "dpt:53" in line:
            unenc += b
    return enc, unenc

@app.route('/feed')
def feed():
    # Encrypted vs unencrypted bytes/sec, driving the packet-feed background (green = TLS,
    # red = the plaintext DNS/HTTP that actually leaves your network in the clear).
    enc, unenc = _try(_acct_counters, (0, 0))
    now, e, u = time.monotonic(), 0, 0
    prev = _FEED_PREV.get("v")
    if prev and now - prev[0] >= 0.3:
        dt = now - prev[0]
        e = max(0, round((enc - prev[1]) / dt))
        u = max(0, round((unenc - prev[2]) / dt))
    _FEED_PREV["v"] = (now, enc, unenc)
    return jsonify({"enc": e, "unenc": u})

# ---------------------------------------------------------------------------
# Devices page: the phone + laptop as the Pi sees them over Tailscale. Everything
# comes from `tailscale status --json` (per-peer online/OS/traffic/last-seen); no
# personal identifiers are baked in — device names are read live.
# ---------------------------------------------------------------------------

_TS_PREV = {}   # tailscale IP -> (monotonic_t, rx, tx) for per-device throughput

def _ts_status():
    out = subprocess.run(["tailscale", "status", "--json"],
                         capture_output=True, text=True, timeout=4).stdout
    return json.loads(out)

def _dev_name(p):
    h = p.get("HostName") or ""
    if h and h.lower() != "localhost":
        return h
    dns = (p.get("DNSName") or "").split(".")[0]
    return dns or (p.get("TailscaleIPs") or ["device"])[0]

def _dev_kind(os_):
    return "phone" if (os_ or "").lower() in ("ios", "android", "ipados") else "computer"

def _fmt_bytes(n):
    if n is None:
        return "—"
    for unit, div in (("GB", 1073741824), ("MB", 1048576), ("KB", 1024)):
        if n >= div:
            return (f"{n/div:.1f} {unit}" if unit != "KB" else f"{n/div:.0f} {unit}")
    return f"{n} B"

def _since(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        s = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return ""
    if s < 0:
        return "just now"
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return f"{int(s)}s ago"

def _device(p):
    ip = (p.get("TailscaleIPs") or [""])[0]
    rx, tx = p.get("RxBytes", 0), p.get("TxBytes", 0)     # Pi<-peer, Pi->peer
    down_bps = up_bps = None                               # device-centric: down = Pi->device
    now = time.monotonic()
    prev = _TS_PREV.get(ip)
    if prev and now - prev[0] >= 0.5:
        dt = now - prev[0]
        up_bps = max(0, round((rx - prev[1]) / dt))
        down_bps = max(0, round((tx - prev[2]) / dt))
    _TS_PREV[ip] = (now, rx, tx)
    # Online = control-plane heartbeat; Active = currently passing packets. A directly-
    # connected peer (especially same-LAN) can read Online=false while still Active with
    # traffic flowing over the WireGuard path — so count "actively passing data" as
    # connected too, else it shows the nonsensical "offline, but downloading".
    online = bool(p.get("Online") or p.get("Active"))
    return {
        "name": _dev_name(p), "os": p.get("OS", "?"), "kind": _dev_kind(p.get("OS")),
        "ip": ip, "online": online,
        "direct": bool(p.get("CurAddr")), "relay": p.get("Relay", ""),
        "down_bytes": tx, "up_bytes": rx, "down_bps": down_bps, "up_bps": up_bps,
        "down_h": _fmt_bytes(tx), "up_h": _fmt_bytes(rx),
        "last_seen": "online" if online else (_since(p.get("LastSeen", "")) or "offline"),
    }

def collect_devices():
    st = _try(_ts_status)
    if not st:
        return {"ok": False, "self": {}, "devices": []}
    devices = [_device(p) for p in st.get("Peer", {}).values()]
    devices.sort(key=lambda d: (d["kind"] != "phone", not d["online"], d["name"].lower()))
    self_ = st.get("Self", {})
    return {"ok": True,
            "self": {"name": _dev_name(self_), "ip": (self_.get("TailscaleIPs") or [""])[0]},
            "devices": devices}

DEVICES_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Devices · Countdown</title>
    <style>
        :root{
            --bg:#0b0d10; --card:#14171d; --line:#232732; --fg:#f4f6f8; --muted:#8b93a0;
            --faint:#5f6773; --go:#3ecf7c; --wait:#f0a63a; --bad:#e5484d; --sleep:#7aa2ff;
        }
        *{box-sizing:border-box}
        body{margin:0;background:radial-gradient(1100px 560px at 50% -10%,#161a22,var(--bg));
            color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
            -webkit-font-smoothing:antialiased;padding:26px 16px max(26px,env(safe-area-inset-bottom));display:flex;justify-content:center}
        .wrap{width:100%;max-width:560px}
        .kicker{display:flex;align-items:center;gap:8px;justify-content:center;font-size:11.5px;
            font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:18px}
        .kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--go)}
        .board{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 10px 8px;margin-bottom:12px}
        .board svg{display:block;width:100%;height:auto}
        .caption{text-align:center;font-size:11.5px;color:var(--faint);margin:4px 0 4px}
        /* line-art nodes */
        .dev .body{fill:#0f1319;stroke:#3a4150;stroke-width:1.5;transition:.4s}
        .dev.on .body{fill:#101c15;stroke:var(--go)}
        .dev .scr{fill:#0a0c10;stroke:#2a2f3a;stroke-width:1}
        .dev.on .scr{stroke:#1f5137}
        .dev .lbl{fill:var(--muted);font:600 10px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .dev.on .lbl{fill:var(--go)}
        .dev .sub{fill:var(--faint);font:600 8.5px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .hub{fill:#0c1a12;stroke:#2f5d43;stroke-width:1.5}
        .hubp{fill:#123522;stroke:var(--go);stroke-width:1.4}
        .hubl{fill:var(--go);font:700 9px ui-monospace,Menlo,monospace;text-anchor:middle}
        .gwl{fill:var(--faint);font:600 9px -apple-system,Roboto,Arial,sans-serif;text-anchor:middle}
        .lane{stroke:#3a3f4a;stroke-width:1.6;fill:none;transition:stroke .4s,stroke-width .35s}
        .lane.dn.on{stroke:var(--go)}
        .lane.up.on{stroke:var(--sleep)}
        .lane.dn.flow{stroke-dasharray:5 7;animation:flowdn .9s linear infinite}
        .lane.up.flow{stroke-dasharray:5 7;animation:flowup .9s linear infinite}
        @keyframes flowdn{to{stroke-dashoffset:-12}}
        @keyframes flowup{to{stroke-dashoffset:12}}
        @media (prefers-reduced-motion:reduce){.lane.flow{animation:none}}
        /* cards */
        .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
        @media (max-width:460px){.grid{grid-template-columns:1fr}}
        .dcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
        .dhead{display:flex;align-items:center;gap:9px}
        .dico{width:26px;height:26px;flex:none;color:var(--muted)}
        .dcard.on .dico{color:var(--go)}
        .dname{font-size:15px;font-weight:700;letter-spacing:-.2px;word-break:break-word}
        .dos{font-size:11px;color:var(--faint);margin-top:1px;text-transform:uppercase;letter-spacing:.04em}
        .drow{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--fg);margin-top:11px}
        .drow .k{color:var(--faint)}.drow .v{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--muted)}
        .sdot{width:8px;height:8px;border-radius:50%;background:#3a3f4a;flex:none}
        .dcard.on .sdot{background:var(--go);box-shadow:0 0 0 3px rgba(62,207,124,.15)}
        .thru{margin-top:11px;padding-top:11px;border-top:1px solid var(--line);font-size:11.5px;
            color:var(--faint);font-variant-numeric:tabular-nums;display:flex;gap:16px}
        .thru b{color:var(--fg);font-weight:600}
        .empty{color:var(--faint);font-size:13px;text-align:center;padding:20px}
        .foot{display:flex;gap:16px;justify-content:center;margin-top:20px}
        .foot a{font-size:12.5px;color:var(--faint);text-decoration:none}
    </style>
</head>
<body>
<div class="wrap">
    <div class="kicker"><span class="dot"></span>Your devices · via {{ d.self.name or 'Pi' }}</div>

    <div class="board">
      <svg viewBox="0 0 380 250" role="img" aria-label="Devices connected to the Pi">
        <!-- two lanes per link: green flows DOWN to a device (its downloads), blue flows UP to the Pi -->
        <path id="ph-dn" class="lane dn {{ 'on' if phone and phone.online else '' }}" d="M176 72 L82 116"/>
        <path id="ph-up" class="lane up {{ 'on' if phone and phone.online else '' }}" d="M183 74 L89 118"/>
        <path id="lp-dn" class="lane dn {{ 'on' if laptop and laptop.online else '' }}" d="M204 72 L286 120"/>
        <path id="lp-up" class="lane up {{ 'on' if laptop and laptop.online else '' }}" d="M197 74 L279 122"/>
        <!-- Pi hub (top) -->
        <rect class="hub" x="150" y="20" width="80" height="52" rx="9"/>
        <circle class="hubp" cx="180" cy="34" r="2.5"/><circle class="hubp" cx="190" cy="34" r="2.5"/><circle class="hubp" cx="200" cy="34" r="2.5"/>
        <text class="hubl" x="190" y="52">{{ (d.self.name or 'pi')[:9] }}</text>
        <text class="gwl" x="190" y="65">gateway</text>
        <!-- phone (bottom-left) -->
        <g class="dev {{ 'on' if phone and phone.online else '' }}" id="dev-phone">
          <rect class="body" x="56" y="120" width="48" height="86" rx="11"/>
          <rect class="scr" x="62" y="132" width="36" height="58" rx="3"/>
          <rect x="72" y="125" width="16" height="3" rx="1.5" fill="#2a2f3a"/>
          <rect x="72" y="196" width="16" height="3" rx="1.5" fill="#2a2f3a"/>
          <text class="lbl" x="80" y="224">{% if phone %}{{ phone.name[:15] }}{% else %}no phone{% endif %}</text>
          <text class="sub" x="80" y="236" id="sub-phone">{% if phone %}{{ phone.last_seen }}{% else %}not connected{% endif %}</text>
        </g>
        <!-- laptop (bottom-right) -->
        <g class="dev {{ 'on' if laptop and laptop.online else '' }}" id="dev-laptop">
          <rect class="body" x="250" y="124" width="72" height="50" rx="4"/>
          <rect class="scr" x="256" y="130" width="60" height="38" rx="2"/>
          <path class="body" d="M244 174 H328 L334 186 H238 Z"/>
          <text class="lbl" x="286" y="206">{% if laptop %}{{ laptop.name[:16] }}{% else %}no laptop{% endif %}</text>
          <text class="sub" x="286" y="218" id="sub-laptop">{% if laptop %}{{ laptop.last_seen }}{% else %}not connected{% endif %}</text>
        </g>
      </svg>
      <div class="caption"><span style="color:var(--go)">&#9660; green</span> = download to a device &nbsp;·&nbsp; <span style="color:var(--sleep)">&#9650; blue</span> = upload to the Pi &nbsp;·&nbsp; dashes flow when data moves</div>
    </div>

    <div class="grid" id="cards">
      {% for x in d.devices %}
      <div class="dcard {{ 'on' if x.online else '' }}" data-ip="{{ x.ip }}">
        <div class="dhead">
          <svg class="dico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
            {% if x.kind == 'phone' %}<rect x="7" y="2" width="10" height="20" rx="2.5"/><line x1="10" y1="18.5" x2="14" y2="18.5"/>
            {% else %}<rect x="3" y="4" width="18" height="12" rx="1.5"/><line x1="1" y1="20" x2="23" y2="20"/>{% endif %}
          </svg>
          <div><div class="dname">{{ x.name }}</div><div class="dos">{{ x.os }}</div></div>
        </div>
        <div class="drow"><span class="sdot"></span><span class="k">Status</span><span class="v">{{ 'Online' if x.online else x.last_seen }}</span></div>
        <div class="drow"><span class="k">Link</span><span class="v">{{ 'Direct' if x.direct else ('Relay · ' + x.relay if x.relay else 'Relay') }}</span></div>
        <div class="drow"><span class="k">Through Pi</span><span class="v" data-total>&darr; {{ x.down_h }} &nbsp; &uarr; {{ x.up_h }}</span></div>
        <div class="thru"><span data-rate>&darr; <b>&mdash;</b></span><span data-rate2>&uarr; <b>&mdash;</b></span></div>
      </div>
      {% else %}
      <div class="empty">No devices on the tailnet right now.</div>
      {% endfor %}
    </div>

    <div class="foot"><a href="/budget/health">&larr; Pi health</a><a href="/budget/stats">Usage stats</a><a href="/budget/devices">Refresh</a></div>
</div>
{% raw %}
<script>
(function(){
    var TOK=(document.querySelector('meta[name="cd-tok"]')||{}).content||"";
    function fmtBytes(n){ if(n==null) return "—"; if(n<1024) return n+" B"; if(n<1048576) return (n/1024).toFixed(0)+" KB"; if(n<1073741824) return (n/1048576).toFixed(1)+" MB"; return (n/1073741824).toFixed(2)+" GB"; }
    function fmtRate(b){ if(b==null) return "—"; if(b<1024) return b+" B/s"; if(b<1048576) return (b/1024).toFixed(b<10240?1:0)+" KB/s"; return (b/1048576).toFixed(1)+" MB/s"; }
    function node(id, sub, dev){
        var g=document.getElementById(id); if(!g) return;
        g.setAttribute("class", "dev "+(dev&&dev.online?"on":""));
        var s=document.getElementById(sub); if(s&&dev) s.textContent=dev.last_seen;
    }
    function laneW(bps){ if(!bps||bps<=0) return 1.6; return Math.max(1.6, Math.min(5.5, 1.6+1.0*Math.log(bps/400)/Math.LN10)); }
    function lanes(prefix, dev){
        var on=dev&&dev.online;
        var dn=document.getElementById(prefix+"-dn"), up=document.getElementById(prefix+"-up");
        if(dn){ dn.setAttribute("class","lane dn"+(on?" on":"")+(on&&dev.down_bps>0?" flow":"")); dn.style.strokeWidth=laneW(on?dev.down_bps:0)+"px"; }
        if(up){ up.setAttribute("class","lane up"+(on?" on":"")+(on&&dev.up_bps>0?" flow":"")); up.style.strokeWidth=laneW(on?dev.up_bps:0)+"px"; }
    }
    function card(x){
        var el=document.querySelector('.dcard[data-ip="'+x.ip+'"]'); if(!el) return;
        el.className="dcard "+(x.online?"on":"");
        var st=el.querySelectorAll(".drow .v");
        if(st[0]) st[0].textContent = x.online?"Online":x.last_seen;
        if(st[1]) st[1].textContent = x.direct?"Direct":(x.relay?("Relay · "+x.relay):"Relay");
        var tot=el.querySelector("[data-total]"); if(tot) tot.innerHTML="↓ "+fmtBytes(x.down_bytes)+" &nbsp; ↑ "+fmtBytes(x.up_bytes);
        var r1=el.querySelector("[data-rate]"); if(r1) r1.innerHTML="↓ <b>"+(x.online?fmtRate(x.down_bps):"—")+"</b>";
        var r2=el.querySelector("[data-rate2]"); if(r2) r2.innerHTML="↑ <b>"+(x.online?fmtRate(x.up_bps):"—")+"</b>";
    }
    function update(d){
        var devs=d.devices||[];
        var phone=devs.find(function(x){return x.kind==="phone";});
        var laptop=devs.find(function(x){return x.kind==="computer";});
        node("dev-phone","sub-phone",phone); lanes("ph",phone);
        node("dev-laptop","sub-laptop",laptop); lanes("lp",laptop);
        devs.forEach(card);
    }
    function poll(){ fetch("/budget/devices?fmt=json&t="+TOK+"&_="+Date.now(),{cache:"no-store"}).then(function(r){return r.json();}).then(update).catch(function(){}); }
    setInterval(poll, 4000);
    document.addEventListener("visibilitychange", function(){ if(!document.hidden) poll(); });
    poll();
})();
</script>
{% endraw %}
</body>
</html>
"""

# --- Shared ambient background: give EVERY page the gate's "encrypted traffic" porthole,
# and frost the panels so the drifting hex softly shows through instead of vanishing behind
# them. The animation is defined once (in BUDGET_PAGE) and reused, so there's no drift. ---
BG_CANVAS = '<canvas id="bp-bg" aria-hidden="true"></canvas>'
# The {% raw %}<script>…</script>{% endraw %} block from BUDGET_PAGE that owns #bp-bg (the
# other raw/script block on that page is the reflection prompt — pick the bp-bg one).
BG_SCRIPT = next(b for b in re.findall(r"\{% raw %\}\s*<script>.*?</script>\s*\{% endraw %\}",
                                       BUDGET_PAGE, re.S) if "bp-bg" in b)
BG_STYLE = ("html,body{background:#070b0e!important}"
            "#bp-bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;display:block}"
            ".wrap{position:relative;z-index:1}"
            ".card,.tile,.board,.metric,.dcard{background:rgba(18,21,27,0.4)!important;"
            "-webkit-backdrop-filter:blur(5px);backdrop-filter:blur(5px)}")

def _add_bg(page, full=True):
    page = page.replace("</head>",
        '<meta name="theme-color" content="#070b0e">'
        '<meta name="cd-tok" content="{{ ui_tok }}"></head>', 1)  # dark iOS bars + poll token
    page = page.replace("</style>", BG_STYLE + "</style>", 1)   # frost + canvas layer + z-index
    if full:                                                    # pages that don't already have the canvas
        page = page.replace("<body>", "<body>\n" + BG_CANVAS, 1)
        page = page.replace("</body>", BG_SCRIPT + "\n</body>", 1)
    return page

STATS_PAGE = _add_bg(STATS_PAGE)
HEALTH_PAGE = _add_bg(HEALTH_PAGE)
DEVICES_PAGE = _add_bg(DEVICES_PAGE)
BUDGET_PAGE = _add_bg(BUDGET_PAGE, full=False)   # already carries the canvas + script; just frost it

@app.route('/devices')
def devices():
    d = collect_devices()
    if request.args.get("fmt") == "json":
        return jsonify(d)
    phone = next((x for x in d["devices"] if x["kind"] == "phone"), None)
    laptop = next((x for x in d["devices"] if x["kind"] == "computer"), None)
    return render_template_string(DEVICES_PAGE, d=d, phone=phone, laptop=laptop)

if __name__ == '__main__':
    # Production WSGI server (waitress) instead of the Werkzeug dev server: more
    # robust for 24/7 operation, no dev-server warning. Localhost-only — it's only
    # ever reached via the mitmproxy addon over loopback.
    from waitress import serve
    serve(app, host='127.0.0.1', port=5000, threads=12)
