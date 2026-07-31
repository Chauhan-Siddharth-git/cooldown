// ==UserScript==
// @name         Facebook — allow-list block
// @namespace    cooldown
// @version      4.0
// @description  Blocks all of Facebook except an allow-list (Marketplace, Groups, Messages, your own profile) — no scroll, no reveal — while keeping the nav usable. Handles desktop (role=main column) and mobile (covers all but the tab bar + freezes scroll). Login / logged-out pages are left alone.
// @match        https://www.facebook.com/*
// @match        https://web.facebook.com/*
// @match        https://m.facebook.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==
(function () {
  "use strict";

  // Allow-list model: block EVERY Facebook page except the few the roommate search needs,
  // so profiles, Watch, search and any Messenger→profile hop are covered without having to
  // recognise each doomscroll surface. Login/checkpoint and logged-out pages are never touched.
  var ALLOW = ["/marketplace", "/groups", "/messages", "/notifications",
               "/settings", "/friends", "/bookmarks", "/me"];

  function selfId() { var m = document.cookie.match(/(?:^|;)\s*c_user=(\d+)/); return m ? m[1] : null; }
  function loggedIn() { return !!selfId() || !!document.querySelector('[role="tablist"],[role="banner"]'); }
  function authSurface() {
    if (document.querySelector('input[type="password"]')) return true;   // login / re-auth / checkpoint
    return /^\/(login|checkpoint|recover|reg|two_step|two_factor|security|help|privacy|policies|terms|legal|confirmemail|device)/.test(location.pathname);
  }
  function allowed() {
    var p = location.pathname;
    for (var i = 0; i < ALLOW.length; i++) { if (p === ALLOW[i] || p.indexOf(ALLOW[i] + "/") === 0) return true; }
    if (p === "/profile.php") {                    // your own profile only, never anyone else's
      var id = new URLSearchParams(location.search).get("id"), s = selfId();
      return !id || (s && id === s);
    }
    return false;
  }
  function shouldBlock() {
    if (authSurface() || !loggedIn()) return false;   // never wall login / logged-out
    return !allowed();                                 // home + everything off the allow-list
  }

  function ensureStyle() {
    if (document.getElementById("cd-fb-style")) return;
    var s = document.createElement("style");
    s.id = "cd-fb-style";
    s.textContent = [
      // Desktop: clip the content column and block interaction under the cover.
      ".cd-fb-lock{position:relative!important;max-height:calc(100vh - 60px)!important;overflow:hidden!important;}",
      ".cd-fb-lock > *:not(#cd-fb-cover){pointer-events:none!important;}",
      // Mobile: freeze the page so nothing can scroll.
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
      var t = document.createElement("div"); t.className = "cd-t"; t.textContent = "This page is off";
      var p = document.createElement("div"); p.className = "cd-p";
      p.textContent = "Facebook here is Marketplace, Groups, Messages and your own profile — the feed and the rest stay closed.";
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
    var mobile = window.innerWidth < 900;

    if (mobile && tablist) {
      // MOBILE: leave the tab bar exposed (Marketplace/Groups/Messages), cover the rest.
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
    } else if (!mobile && main) {
      // DESKTOP: cover just the content column; the top bar + rail stay live to navigate.
      document.documentElement.classList.remove("cd-fb-noscroll");
      main.classList.add("cd-fb-lock");
      coverIn(main, "desk");
    } else {
      // No nav to preserve (mobile without a tab bar, or a bare page) → cover it all.
      if (main) main.classList.remove("cd-fb-lock");
      document.documentElement.classList.add("cd-fb-noscroll");
      var f = coverIn(document.documentElement, "mob");
      f.style.top = "0"; f.style.bottom = "0";
    }
  }

  function unlock() {
    var c = document.getElementById("cd-fb-cover");
    if (c) c.remove();
    document.documentElement.classList.remove("cd-fb-noscroll");
    var m = document.querySelector('[role="main"]');
    if (m) m.classList.remove("cd-fb-lock");
  }

  function tick() { if (shouldBlock()) lock(); else unlock(); }

  ["pushState", "replaceState"].forEach(function (fn) {
    var o = history[fn];
    history[fn] = function () { var r = o.apply(this, arguments); setTimeout(tick, 60); return r; };
  });
  window.addEventListener("popstate", tick);
  window.addEventListener("resize", function () { if (shouldBlock()) lock(); });
  document.addEventListener("DOMContentLoaded", tick);
  setInterval(tick, 700);
  tick();
})();
