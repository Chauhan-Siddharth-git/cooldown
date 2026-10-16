"""The Instagram feed ends where the people you follow end -- checked in a real browser.

The declutter anchors on visible text, not Instagram's generated classes, so the only
honest test runs it against a DOM. This serves a feed-shaped page to headless Chrome and
reads the DOM back after the script has run: friends' posts stay; suggested posts, the
suggestion and Reels shelves, and everything after "You're all caught up" go -- including
a post that arrives after load, the way Instagram's endless tail does.
"""
import functools
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import addon  # noqa: E402

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
SCRIPT = re.search(r"<script>.*</script>", addon.INSTAGRAM_DECLUTTER, re.S).group(0)

FEED = """<!doctype html><html><head><meta charset="utf-8"></head><body><main>
<div id="stories"><span>Your story</span></div>
<div class="feed">
  <article id="p1"><header><span>friend_one</span></header><p>post</p></article>
  <article id="s1"><header><span>stranger</span><span>Suggested for you</span></header></article>
  <div id="shelf"><div><span>Suggested for you</span><a href="#">See all</a></div><div>cards</div></div>
  <article id="p2"><header><span>friend_two</span></header></article>
  <div id="reels"><h2>Reels</h2><div>reel cards</div></div>
  <article id="p3"><header><span>friend_three</span></header></article>
  <div id="caught"><div><span>You’re all caught up</span><span>You've seen all new posts.</span></div></div>
  <div id="sugghead"><span>Suggested posts</span></div>
  <article id="x1"><span>stranger post</span></article>
  <article id="x2"><span>another stranger</span></article>
</div></main>
<script>setTimeout(function () {
  var a = document.createElement("article"); a.id = "x3"; a.innerHTML = "<span>late stranger</span>";
  document.querySelector(".feed").appendChild(a); }, 1000);</script>
%s</body></html>"""


def dump(path, pages):
    """Serve `pages` ({url path: html}) on loopback and return Chrome's DOM for `path`."""
    with tempfile.TemporaryDirectory() as site, tempfile.TemporaryDirectory() as profile:
        for url, html in pages.items():
            d = os.path.join(site, url.strip("/"))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "index.html"), "w") as fh:
                fh.write(html)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=site)
        handler.log_message = lambda *a: None
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out = subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--no-default-browser-check", f"--user-data-dir={profile}",
                 "--virtual-time-budget=5000", "--dump-dom",
                 f"http://127.0.0.1:{srv.server_address[1]}{path}"],
                capture_output=True, text=True, timeout=60)
        finally:
            srv.shutdown()
        return out.stdout


def hidden(dom, el_id):
    m = re.search(r'<[a-z0-9]+ id="%s"[^>]*>' % el_id, dom)
    assert m, f"#{el_id} missing from the DOM"
    return 'data-cd-hidden="1"' in m.group(0)


@pytest.fixture(scope="module")
def feed_dom():
    if CHROME is None:
        pytest.skip("needs google-chrome or chromium")
    return dump("/", {"/": FEED % SCRIPT})


def test_friends_posts_stay(feed_dom):
    for pid in ("p1", "p2", "p3", "caught"):
        assert not hidden(feed_dom, pid), pid


def test_suggestions_and_reels_shelves_go(feed_dom):
    for pid in ("s1", "shelf", "reels", "sugghead"):
        assert hidden(feed_dom, pid), pid


def test_everything_after_caught_up_goes_including_late_arrivals(feed_dom):
    for pid in ("x1", "x2", "x3"):
        assert hidden(feed_dom, pid), pid


def test_the_feed_ends_with_a_line_saying_so(feed_dom):
    # The element, not the words: the script's own source contains the words too.
    assert re.search(r'id="caught".*?</div></div>\s*<div id="cd-ig-end"[^>]*>That\'s everything '
                     r'from the people you follow\.</div>', feed_dom, re.S)


def test_only_the_home_feed_is_touched():
    """DMs and profiles are left exactly as Instagram drew them."""
    if CHROME is None:
        pytest.skip("needs google-chrome or chromium")
    dom = dump("/direct/", {"/direct/": FEED % SCRIPT})
    # Elements, not substrings: the script's source names both of these.
    assert 'data-cd-hidden="1"' not in dom and '<div id="cd-ig-end"' not in dom


def test_it_is_injected_on_instagram_only():
    assert addon.INSTAGRAM_DECLUTTER.strip().startswith("<script>")
    src = open(os.path.join(ROOT, "addon.py")).read()
    assert 'if site == "instagram":\n                injection += INSTAGRAM_DECLUTTER' in src
