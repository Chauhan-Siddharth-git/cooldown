// ==UserScript==
// @name         Facebook Declutter — hide home feed + kill Reels
// @namespace    cooldown
// @version      1.0
// @description  Hide the Facebook home feed and bounce Reels, while leaving Marketplace, Groups, posting and Messenger fully usable. No time limit. Facebook fights MITM interception, so this runs client-side instead of through the Cooldown proxy.
// @match        https://www.facebook.com/*
// @match        https://m.facebook.com/*
// @match        https://web.facebook.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==
(function () {
  "use strict";

  // Hide the home news feed ONLY on the home route (Group/Marketplace feeds use the
  // same element, so we scope with a class toggled on <html>), and hide Reels links.
  var css =
    'html.fbdc-home div[role="feed"]{display:none!important}' +
    'a[href^="/reel/"],a[href^="/reels/"],' +
    '[aria-label="Reels"],[aria-label="Reels and short videos"]{display:none!important}';
  var style = document.createElement("style");
  style.textContent = css;
  (document.head || document.documentElement).appendChild(style);

  function isHome() {
    var p = location.pathname;
    return p === "/" || p === "/home.php";
  }
  function markHome() {
    document.documentElement.classList.toggle("fbdc-home", isHome());
  }
  // Bounce the Reels swipe-feed back home (where the feed is hidden).
  function deReel() {
    if (/^\/reels?\//.test(location.pathname)) location.replace("/");
  }
  function apply() { markHome(); deReel(); }

  // Facebook is a single-page app: most navigation is client-side, so hook the history
  // API (plus a slow interval backstop) to re-apply on every in-page navigation.
  ["pushState", "replaceState"].forEach(function (fn) {
    var orig = history[fn];
    history[fn] = function () { var r = orig.apply(this, arguments); apply(); return r; };
  });
  window.addEventListener("popstate", apply);
  document.addEventListener("DOMContentLoaded", apply);
  setInterval(apply, 1000);
  apply();

  // Gentle nudge where the feed was, so home isn't just blank.
  function nudge() {
    if (!isHome() || document.getElementById("fbdc-nudge")) return;
    var feed = document.querySelector('div[role="feed"]');
    if (!feed || !feed.parentNode) return;
    var d = document.createElement("div");
    d.id = "fbdc-nudge";
    d.textContent = "Home feed hidden — Marketplace, Groups, and your posts are still up top.";
    d.style.cssText = "padding:24px;margin:16px auto;max-width:500px;border-radius:8px;" +
      "background:#fff;color:#65676b;font-family:sans-serif;text-align:center;" +
      "font-size:15px;box-shadow:0 1px 2px rgba(0,0,0,.12)";
    feed.parentNode.insertBefore(d, feed);
  }
  setInterval(nudge, 1000);
})();
