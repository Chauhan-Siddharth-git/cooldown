// ==UserScript==
// @name         Facebook Feed — frosted overlay (tap to reveal)
// @namespace    cooldown
// @version      1.0
// @description  Covers the Facebook home feed with a frosted "tap to reveal" panel instead of deleting it — a friction speed bump, not a wall. The top nav, Marketplace, Groups and posting stay usable. Home page only. (Alternative to News Feed Eradicator; disable that if you use this.)
// @match        https://www.facebook.com/*
// @match        https://web.facebook.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==
(function () {
  "use strict";

  // Reveal lasts until you leave the home route (or reload), so every fresh visit to
  // Home re-covers the feed. That's the friction: you have to *choose* to scroll.
  var revealed = false;

  function isHome() {
    var p = location.pathname;
    return p === "/" || p === "/home.php";
  }

  function injectStyle() {
    if (document.getElementById("cd-fb-style")) return;
    var s = document.createElement("style");
    s.id = "cd-fb-style";
    s.textContent = [
      // Cap the main column to one screen and clip it, so there's nothing to scroll,
      // and blur everything under it (the cover itself is excluded).
      ".cd-fb-locked{position:relative!important;max-height:calc(100vh - 62px)!important;overflow:hidden!important;}",
      ".cd-fb-locked > *:not(#cd-fb-cover){filter:blur(8px)!important;pointer-events:none!important;user-select:none!important;}",
      "#cd-fb-cover{position:absolute;inset:0;z-index:40;display:flex;flex-direction:column;align-items:center;",
      "justify-content:center;gap:14px;padding:24px;text-align:center;background:rgba(255,255,255,.30);",
      "-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);}",
      "#cd-fb-cover .cd-t{margin:0;font:700 22px/1.25 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1c1e21;}",
      "#cd-fb-cover .cd-p{margin:0;font:400 14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#3a3b3c;max-width:300px;}",
      "#cd-fb-reveal{border:none;background:#1877f2;color:#fff;font:600 15px -apple-system,'Segoe UI',sans-serif;padding:10px 22px;border-radius:8px;cursor:pointer;}",
      "#cd-fb-reveal:hover{background:#166fe0;}"
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
    t.textContent = "Feed hidden";
    var p = document.createElement("div");
    p.className = "cd-p";
    p.textContent = "You opened Facebook to do a thing — not to scroll. Reveal only if you really mean to.";
    var b = document.createElement("button");
    b.id = "cd-fb-reveal";
    b.textContent = "Reveal feed";
    b.addEventListener("click", function () { revealed = true; unlock(); });
    cover.appendChild(t);
    cover.appendChild(p);
    cover.appendChild(b);
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
    if (isHome() && !revealed) lock();
    else unlock();
  }

  // Facebook is a single-page app; re-check on every in-page navigation, and reset the
  // reveal so returning to Home re-covers it. A slow interval catches React re-renders.
  ["pushState", "replaceState"].forEach(function (fn) {
    var orig = history[fn];
    history[fn] = function () { var r = orig.apply(this, arguments); revealed = false; setTimeout(tick, 60); return r; };
  });
  window.addEventListener("popstate", function () { revealed = false; tick(); });
  document.addEventListener("DOMContentLoaded", tick);
  setInterval(tick, 700);
  tick();
})();
