// Shared by the story page (/) and the console (/console): DOM helpers, the motion kit, the map layout, theme.

const SVG_NS = 'http://www.w3.org/2000/svg';
const MOTION = !matchMedia('(prefers-reduced-motion: reduce)').matches;
const WARN_Z = 3, ALARM_Z = 4;

// service map: where each service sits, caller above callee
const POS = {
  frontend: [400, 36], recommend: [90, 130], catalog: [230, 130], cart: [360, 130], checkout: [590, 130],
  db: [230, 230], cache: [360, 230], payment: [470, 230], email: [600, 230], shipping: [710, 230], currency: [500, 305],
};
const NODE_W = 100, NODE_H = 34;

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const pct = (x) => (x === null || x === undefined ? '–' : `${Math.round(x * 100)}%`);
const usd = (x) => (x === null || x === undefined ? '–' : `$${x < 0.01 ? x.toFixed(4) : x.toFixed(3)}`);
const levelOf = (z) => (z >= ALARM_Z ? 'bad' : z >= WARN_Z ? 'warn' : 'ok');

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : String(child));  // strings become text, never HTML
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

async function getJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

async function errorText(res) {
  try { const body = await res.json(); return typeof body.detail === 'string' ? body.detail : 'Request failed.'; }
  catch { return `Request failed (${res.status}).`; }
}

/** Where an arrow between two service boxes starts and ends, so it touches their borders. */
function edgeEnds([x1, y1], [x2, y2]) {
  const dx = x2 - x1, dy = y2 - y1;
  const toBorder = Math.min((NODE_W / 2) / Math.abs(dx || 1e-9), (NODE_H / 2) / Math.abs(dy || 1e-9));
  const gap = 4 / Math.hypot(dx, dy);
  return { x1: x1 + dx * toBorder, y1: y1 + dy * toBorder, x2: x2 - dx * (toBorder + gap), y2: y2 - dy * (toBorder + gap) };
}

/** Mean z-score of each service's metric over the last 3 ticks before `t`, against its healthy start. */
function zAt(series, metric, t, baselineTicks, stdFloor) {
  return Object.fromEntries(Object.entries(series).map(([s, metrics]) => {
    const values = metrics[metric];
    const base = values.slice(0, baselineTicks);
    const mean = base.reduce((a, v) => a + v, 0) / base.length;
    const std = Math.max(Math.sqrt(base.reduce((a, v) => a + (v - mean) ** 2, 0) / base.length), stdFloor[metric]);
    const recent = values.slice(Math.max(0, t - 2), t + 1);
    return [s, recent.reduce((a, v) => a + (v - mean) / std, 0) / recent.length];
  }));
}

// ---- motion ---------------------------------------------------------------------------

const SPRING_OK = CSS.supports('transition-timing-function', 'linear(0, 1)');
/** A curve from the motion tokens (--ease-*, --spring). */
function ease(name) {
  const token = name === 'spring' && !SPRING_OK ? '--ease-quint' : name === 'spring' ? '--spring' : `--ease-${name}`;
  return getComputedStyle(document.documentElement).getPropertyValue(token).trim() || 'ease-out';
}

/** Bring nodes in one after another: fade, lift, grow from `scale` and/or come into focus from `blur`. */
function enter(nodes, { duration = 350, stagger = 33, delay = 0, easing = 'expo', y = 6, scale = 1, blur = 0 } = {}) {
  if (!MOTION) return;
  const curve = ease(easing);
  [...nodes].forEach((node, i) => node.animate(
    [{ opacity: 0, transform: `translateY(${y}px) scale(${scale})`, filter: `blur(${blur}px)` }, { opacity: 1, transform: 'none', filter: 'blur(0px)' }],
    { duration, delay: delay + i * stagger, easing: curve, fill: 'backwards' }));
}

const REVEAL = { duration: 1000, stagger: 40, easing: 'soft', y: 16, blur: 10 };  // out of a blur, rising into place

/** Wrap each word of a heading in a mask, keeping inner elements whole; returns the words to slide up. */
function splitWords(heading) {
  const words = [];
  const wrap = (content) => { const inner = el('span', {}, content); words.push(inner); return el('span', { class: 'w' }, inner); };
  heading.replaceChildren(...[...heading.childNodes].flatMap((node) => (node.nodeType === Node.TEXT_NODE
    ? node.textContent.split(/(\s+)/).filter(Boolean).map((t) => (/^\s+$/.test(t) ? ' ' : wrap(t)))
    : [wrap(node)])));
  return words;
}

/** Slide masked words or lines up into view, one after another. */
function maskIn(nodes, { delay = 0, stagger = 45, duration = 1100 } = {}) {
  if (!MOTION) return;
  const curve = ease('lusion');
  [...nodes].forEach((node, i) => node.animate([{ transform: 'translateY(110%)' }, { transform: 'none' }],
    { duration, delay: delay + i * stagger, easing: curve, fill: 'backwards' }));
}

/** The first time each node scrolls into view: reveal it and call `onShow(node)`. */
function revealOnScroll(nodes, onShow = null, options = {}) {
  if (!MOTION || !('IntersectionObserver' in window)) { for (const node of nodes) onShow?.(node); return; }
  const io = new IntersectionObserver((entries) => {
    entries.filter((e) => e.isIntersecting).forEach((e, i) => {
      io.unobserve(e.target);
      e.target.classList.remove('pre');
      if (!options.none) enter([e.target], { ...REVEAL, duration: 800, delay: i * 60, ...options });  // none: onShow animates it
      onShow?.(e.target);
    });
  }, { threshold: 0.12 });
  for (const node of nodes) { node.classList.add('pre'); io.observe(node); }
}

/** Count a number up from zero. */
function countUp(node, value, format, ms = 1400) {
  if (!MOTION || value === null || value === undefined) { node.textContent = format(value); return; }
  const start = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - start) / ms);
    node.textContent = format(value * (1 - (1 - k) ** 4));  // ease out
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ---- theme ----------------------------------------------------------------------------

function loadTheme() {
  try { const theme = localStorage.getItem('sherlog-theme'); if (theme) document.documentElement.dataset.theme = theme; } catch { /* default theme */ }
}

/** Flip light/dark with a cross-fade; `after` repaints anything drawn from the colours (canvases). */
function toggleTheme(after = () => {}) {
  const apply = () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('sherlog-theme', root.dataset.theme); } catch { /* the choice just won't persist */ }
    after();
  };
  if (MOTION && document.startViewTransition) document.startViewTransition(apply);
  else apply();
}

loadTheme();
