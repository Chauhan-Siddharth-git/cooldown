// ==UserScript==
// @name         Facebook Feed — hard block
// @namespace    cooldown
// @version      3.0
// @description  Completely blocks the Facebook home feed — no scroll, no reveal — while keeping the nav usable. Handles desktop (role=main column) and mobile (covers all but the tab bar + freezes scroll). Home page only.
// @match        https://www.facebook.com/*
// @match        https://web.facebook.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==
(function () {
  "use strict";

  function isHome() { var p = location.pathname; return p === "/" || p === "/home.php"; }

  function ensureStyle() {
    if (document.getElementById("cd-fb-style")) return;
    var s = document.createElement("style");
    s.id = "cd-fb-style";
    s.textContent = [
      // Desktop: clip the feed column and block interaction under the cover.
      ".cd-fb-lock{position:relative!important;max-height:calc(100vh - 60px)!important;overflow:hidden!important;}",
      ".cd-fb-lock > *:not(#cd-fb-cover){pointer-events:none!important;}",
      // Mobile: freeze the page so the feed can't scroll.
      "html.cd-fb-noscroll, html.cd-fb-noscroll > body { overflow:hidden !important; }",
      // The cover (solid, theme-aware). Desktop = absolute inside the column; mobile = fixed.
      "#cd-fb-cover{z-index:2147483000;display:flex;flex-direction:column;align-items:center;",
      "justify-content:center;gap:8px;padding:24px;text-align:center;background:#f0f2f5;}",
      "#cd-fb-cover.desk{position:absolute;inset:0;}",
      "#cd-fb-cover.mob{position:fixed;left:0;right:0;}",
      "#cd-fb-cover .cd-t{margin:0;font:700 22px/1.25 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1c1e21;}",
      "#cd-fb-cover .cd-p{margin:0;font:400 14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#65676b;max-width:320px;}",
      "@media (prefers-color-scheme:dark){#cd-fb-cover{background:#18191a;}#cd-fb-cover .cd-t{color:#e4e6eb;}#cd-fb-cover .cd-p{color:#b0b3b8;}}"
    ].join("");
    (document.head || document.documentElement).appendChild(s);
  }

  function coverIn(parent, cls) {
    var c = document.getElementById("cd-fb-cover");
    if (c && c.parentNode !== parent) { c.remove(); c = null; }
    if (!c) {
      c = document.createElement("div");
      c.id = "cd-fb-cover";
      var t = document.createElement("div"); t.className = "cd-t"; t.textContent = "Home feed is off";
      var p = document.createElement("div"); p.className = "cd-p";
      p.textContent = "You're here for Marketplace, Groups and your posts — not the scroll.";
      c.appendChild(t); c.appendChild(p);
      parent.appendChild(c);
    }
    c.className = cls;
    return c;
  }

  function lock() {
    ensureStyle();
    var tablist = document.querySelector('[role="tablist"]');
    var main = document.querySelector('[role="main"]');

    if (window.innerWidth < 900 && tablist) {
      // MOBILE: leave the tab bar exposed, cover the rest, freeze scroll.
      if (!document.documentElement.classList.contains("cd-fb-noscroll")) window.scrollTo(0, 0);
      var r = tablist.getBoundingClientRect();
      var c = coverIn(document.documentElement, "mob");
      if (r.top < window.innerHeight / 2) {          // tab bar at top → cover below it
        c.style.top = Math.max(0, r.bottom) + "px"; c.style.bottom = "0";
      } else {                                        // tab bar at bottom → cover above it
        c.style.top = "0"; c.style.bottom = (window.innerHeight - r.top) + "px";
      }
      document.documentElement.classList.add("cd-fb-noscroll");
      if (main) main.classList.remove("cd-fb-lock");
    } else if (main) {
      // DESKTOP: cover just the feed column; the rest of the page is untouched.
      document.documentElement.classList.remove("cd-fb-noscroll");
      main.classList.add("cd-fb-lock");
      coverIn(main, "desk");
    } else {
      unlock();   // nothing to anchor to yet — wait for FB to render
    }
  }

  function unlock() {
    var c = document.getElementById("cd-fb-cover");
    if (c) c.remove();
    document.documentElement.classList.remove("cd-fb-noscroll");
    var m = document.querySelector('[role="main"]');
    if (m) m.classList.remove("cd-fb-lock");
  }

  function tick() { if (isHome()) lock(); else unlock(); }

  ["pushState", "replaceState"].forEach(function (fn) {
    var o = history[fn];
    history[fn] = function () { var r = o.apply(this, arguments); setTimeout(tick, 60); return r; };
  });
  window.addEventListener("popstate", tick);
  window.addEventListener("resize", function () { if (isHome()) lock(); });
  document.addEventListener("DOMContentLoaded", tick);
  setInterval(tick, 700);
  tick();
})();
