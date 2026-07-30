// ==UserScript==
// @name         Facebook Feed — hard block
// @namespace    cooldown
// @version      2.0
// @description  Completely blocks the Facebook home feed with a solid cover — no scroll, no reveal. The top nav, Marketplace, Groups, and posting stay fully usable. Home page only.
// @match        https://www.facebook.com/*
// @match        https://web.facebook.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==
(function () {
  "use strict";

  function isHome() {
    var p = location.pathname;
    return p === "/" || p === "/home.php";
  }

  function injectStyle() {
    if (document.getElementById("cd-fb-style")) return;
    var s = document.createElement("style");
    s.id = "cd-fb-style";
    s.textContent = [
      // Cap the main column to one screen and clip it (nothing to scroll), and block
      // interaction with whatever is under the cover.
      ".cd-fb-locked{position:relative!important;max-height:calc(100vh - 62px)!important;overflow:hidden!important;}",
      ".cd-fb-locked > *:not(#cd-fb-cover){pointer-events:none!important;user-select:none!important;}",
      // Solid, opaque cover (theme-aware) — no reveal button, so the feed is simply gone.
      "#cd-fb-cover{position:absolute;inset:0;z-index:40;display:flex;flex-direction:column;align-items:center;",
      "justify-content:center;gap:8px;padding:24px;text-align:center;background:#f0f2f5;}",
      "#cd-fb-cover .cd-t{margin:0;font:700 22px/1.25 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1c1e21;}",
      "#cd-fb-cover .cd-p{margin:0;font:400 14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#65676b;max-width:300px;}",
      "@media (prefers-color-scheme:dark){#cd-fb-cover{background:#18191a;}#cd-fb-cover .cd-t{color:#e4e6eb;}#cd-fb-cover .cd-p{color:#b0b3b8;}}"
    ].join("");
    (document.head || document.documentElement).appendChild(s);
  }

  function lock() {
    var main = document.querySelector('[role="main"]');
    if (!main || main.querySelector("#cd-fb-cover")) return;
    injectStyle();
    main.classList.add("cd-fb-locked");
    var cover = document.createElement("div");
    cover.id = "cd-fb-cover";
    var t = document.createElement("div");
    t.className = "cd-t";
    t.textContent = "Home feed is off";
    var p = document.createElement("div");
    p.className = "cd-p";
    p.textContent = "You're here for Marketplace, Groups and your posts — not the scroll.";
    cover.appendChild(t);
    cover.appendChild(p);
    main.appendChild(cover);
  }

  function unlock() {
    var main = document.querySelector('[role="main"]');
    if (!main) return;
    main.classList.remove("cd-fb-locked");
    var cover = main.querySelector("#cd-fb-cover");
    if (cover) cover.remove();
  }

  function tick() {
    if (isHome()) lock();
    else unlock();
  }

  // Facebook is a single-page app; re-check on every in-page navigation. A slow interval
  // catches React re-renders (which can replace the main column).
  ["pushState", "replaceState"].forEach(function (fn) {
    var orig = history[fn];
    history[fn] = function () { var r = orig.apply(this, arguments); setTimeout(tick, 60); return r; };
  });
  window.addEventListener("popstate", tick);
  document.addEventListener("DOMContentLoaded", tick);
  setInterval(tick, 700);
  tick();
})();
