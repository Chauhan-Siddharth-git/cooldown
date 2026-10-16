// Drive play/tetris.html's real script with touch events, under node, with a stub page.
// Usage: node tetris_touch_harness.js <path-to-tetris.html>  -> prints JSON results
// Used by tests/test_play.py. The piece sequence is seeded so every run is the same.
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
let js = html.match(/<script>([\s\S]*)<\/script>/)[1];
// Test-only window onto the closure; the shipped file is untouched.
js = js.replace(/\}\)\(\);\s*$/, "window.__t = { get cur() { return cur; }, get state() { return state; }," +
  " get holdType() { return holdType; } };\n})();");

let clock = 1000;
const perf = { now: () => clock };
const docHandlers = {}, winHandlers = {}, els = {};
let rafs = [];
function stubCtx() {
  return new Proxy({}, { get: (o, k) => (k in o ? o[k] : () => ({ addColorStop() {} })),
                         set: (o, k, v) => { o[k] = v; return true; } });
}
function el(id) {
  if (els[id]) return els[id];
  const e = {
    id, style: {}, hidden: false, width: 300, height: 600, _html: "", textContent: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    listeners: {},
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    getContext: () => stubCtx(),
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 300, height: 600 }),
    contains: () => false, focus() {}, blur() {}, appendChild() {}, setAttribute() {},
    querySelector: (s) => (s === "#go" || s === "button" ? el("go") : null),
    set innerHTML(v) { this._html = v; if (v.includes('id="go"')) delete els.go; },
    get innerHTML() { return this._html; },
  };
  return (els[id] = e);
}
const document = {
  getElementById: el, querySelector: () => null, hidden: false, activeElement: null,
  addEventListener(t, f) { (docHandlers[t] = docHandlers[t] || []).push(f); },
  documentElement: el("html"), body: el("body"),
};
const window = {
  innerWidth: 390, innerHeight: 844, devicePixelRatio: 3, ontouchstart: null,
  location: { search: "?min=3" },
  addEventListener(t, f) { (winHandlers[t] = winHandlers[t] || []).push(f); },
  requestAnimationFrame(f) { rafs.push(f); return rafs.length; },
};
let seed = 12345;
const seededMath = Object.create(Math);
seededMath.random = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const sandbox = { window, document, performance: perf, navigator: { maxTouchPoints: 5 },
                  Math: seededMath, Date, JSON, Number, String, Array, Object, isFinite, parseFloat,
                  parseInt, RegExp, Error, decodeURIComponent, console, Proxy };
sandbox.requestAnimationFrame = window.requestAnimationFrame;
sandbox.getComputedStyle = window.getComputedStyle = () => ({ getPropertyValue: () => "#888888" });
sandbox.matchMedia = window.matchMedia = () => ({ matches: false, addEventListener() {} });
sandbox.setTimeout = setTimeout; sandbox.clearTimeout = clearTimeout;
vm.createContext(sandbox);
vm.runInContext(js, sandbox);
const T = window.__t;

function tick(ms) {              // run animation frames forward
  for (let i = 0; i < Math.ceil(ms / 16); i++) {
    clock += 16;
    const fs_ = rafs; rafs = [];
    fs_.forEach(f => f(clock));
  }
}
function touch(type, x, y) {
  let prevented = false;
  const ev = { touches: type === "touchend" ? [] : [{ clientX: x, clientY: y }],
               preventDefault() { prevented = true; } };
  (docHandlers[type] || []).forEach(f => f(ev));
  return prevented;
}
const CELL = 30;                 // 300px board / 10 columns
const out = {};

out.titlePreventsDefault = touch("touchstart", 100, 100);   // card buttons must stay taps
touch("touchend", 100, 100);
out.hasTouchHints = els.card._html.includes("tap to rotate");
(el("go").listeners.click || []).forEach(f => f());           // press Start
tick(50);
out.state = T.state;

function freshNonO() {          // the O piece does not change on rotation; skip it
  for (let i = 0; i < 10 && T.cur && T.cur.type === "O"; i++) {
    touch("touchstart", 150, 100); clock += 50; touch("touchmove", 150, 100 + 6 * CELL);
    touch("touchend", 150, 100 + 6 * CELL); tick(1500);
  }
}
freshNonO();
let r0 = T.cur.rot;
touch("touchstart", 150, 300); clock += 80; touch("touchend", 150, 300);
out.tapRotates = T.cur.rot !== r0;

let x0 = T.cur.x;
touch("touchstart", 100, 300); clock += 100;
touch("touchmove", 100 + 1.2 * CELL, 300); touch("touchmove", 100 + 3.1 * CELL, 300);
clock += 400; touch("touchend", 100 + 3.1 * CELL, 300);
out.dragRightMoves = T.cur.x - x0;

let y0 = T.cur.y, piece = T.cur;
touch("touchstart", 150, 200); clock += 200; touch("touchmove", 150, 200 + 2.2 * CELL);
clock += 400; touch("touchend", 150, 200 + 2.2 * CELL);
out.slowDragDownSoftDrops = T.cur === piece ? T.cur.y - y0 : "piece changed";

piece = T.cur;
touch("touchstart", 150, 100); clock += 60; touch("touchmove", 150, 100 + 5 * CELL);
touch("touchend", 150, 100 + 5 * CELL); tick(1500);
out.flickDownHardDrops = T.cur !== piece;

out.holdBefore = T.holdType;
touch("touchstart", 150, 500); clock += 60; touch("touchmove", 150, 500 - 4 * CELL);
touch("touchend", 150, 500 - 4 * CELL); tick(100);
out.flickUpHolds = T.holdType !== null && T.holdType !== undefined;

console.log(JSON.stringify(out));
