/* The page, really loaded, really handed the server's own frames.
 *
 * Every client-side test this project had called one exported pure function
 * with an object typed out in the test. That proved the function and nothing
 * about the path a frame actually travels: `Session.broadcast` -> the socket ->
 * `handle` -> a renderer -> the DOM. Three bugs lived in that gap at once and
 * 975 tests were blind to all of them, because no test anywhere constructed a
 * frame the server had built.
 *
 * So this loads the page the SERVER rendered (`_page(session)`), runs the real
 * viewer.js inside it, feeds it frames the SERVER built (`_diff`, the state
 * broadcast), and reports what the page holds afterwards. Nothing here may
 * invent a frame; the caller passes what the server produced.
 *
 * `steps` is the other half of the same idea for the things the AUTHOR does --
 * clicking a paragraph, typing into it, blurring, changing tab. A frame is only
 * half of "what happens when a patch lands while he is typing"; the typing is
 * the other half, and a test that cannot express it can only ever check the
 * page at rest.
 *
 * Usage:  node drive.js <page.html> <frames.json>   -> a JSON snapshot on stdout
 */
'use strict';

const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

/* The page's own console goes to stderr, never to stdout. The snapshot IS
 * stdout, and viewer.js writes a `console.info` whenever it collapses a void
 * block -- which put a line of prose in front of the JSON and made the harness
 * fail on documents that happened to contain a bare `\label`. */
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (e) => { console.error(String(e && e.stack || e)); });
['log', 'info', 'warn', 'error', 'debug'].forEach((level) => {
  virtualConsole.on(level, (...args) => { console.error('[page]', ...args); });
});

const pagePath = process.argv[2];
const framesPath = process.argv[3];
const html = fs.readFileSync(pagePath, 'utf8');
const plan = JSON.parse(fs.readFileSync(framesPath, 'utf8'));
const frames = plan.frames || [];

/* The socket is the one thing a test must not have: the page opens it on boot
 * and would sit retrying against a server that is not there, and the frames
 * arrive by hand anyway. Stubbed before any script runs, so `connect()` finds
 * it. Everything else the page touches is real. */
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8000/',
  virtualConsole,
  beforeParse(window) {
    window.WebSocket = function (url) {
      this.url = url;
      this.readyState = 1;
      this.sent = [];
      this.send = (data) => { this.sent.push(data); window.__sent.push(data); };
      this.close = () => {};
      window.__socket = this;
    };
    window.WebSocket.OPEN = 1;
    window.__sent = [];
    window.__fetched = [];
    window.__errors = [];

    /* The page asks the server for its stored drafts on boot. There is no
     * server, and inventing one would put a second implementation of the
     * routes in the test suite. An empty answer is what a manuscript with no
     * unsaved text returns, which is every fixture here. */
    window.fetch = function (url, opts) {
      window.__fetched.push({ url: String(url), method: (opts && opts.method) || 'GET' });
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
        text: () => Promise.resolve(''),
      });
    };
    window.addEventListener('error', (e) => {
      window.__errors.push(String((e.error && e.error.stack) || e.message));
    });
  },
});

const window = dom.window;
const document = window.document;

/* `boot()` waits for DOMContentLoaded, which has not fired when the JSDOM
 * constructor returns. Driving before it lands means driving a page whose
 * element handles are all undefined -- which is not the live path, it is a
 * page that never opened. */
function ready() {
  if (document.readyState === 'complete') return Promise.resolve();
  return new Promise((res) => window.addEventListener('load', res, { once: true }));
}

/* The source editor as the page is holding it right now. Deliberately asked for
 * by the selector the panel has always used rather than by anything the repair
 * introduced, so the same probe reads a page with the fix and a page without
 * one -- which is what lets a guard here fail on the bug. */
function editorNow() {
  return document.querySelector('#ibody textarea.src[data-role="src"]');
}

function editorTrail(step, el, first) {
  return {
    step,
    present: !!el,
    same: !!el && el === first,
    block: el ? el.getAttribute('data-block') : null,
    value: el ? el.value : null,
    showing: !!el && !isHidden(el),
    focused: !!el && document.activeElement === el,
    /* How many source editors are in the document at once. One is the whole
     * of what keeps per-block undo per-block: Blink's undo stack is per frame,
     * so a second editor left lying about is a second block's history within
     * reach of the near box. */
    count: document.querySelectorAll('#ibody textarea.src[data-role="src"]').length,
  };
}

/* jsdom does no layout, so "is it on screen" has to be asked of the tree. */
function isHidden(el) {
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (n.hidden) return true;
  }
  return false;
}

function fire(el, type) {
  el.dispatchEvent(new window.Event(type, { bubbles: true }));
}

/* One thing the author does. Kept to a vocabulary rather than to arbitrary
 * script, so a test states an action and cannot quietly reach past the page. */
function step(V, name) {
  if (name === 'frames') { frames.forEach((f) => { V.handle(f); }); return; }

  if (name.startsWith('select:')) {
    const id = name.slice('select:'.length);
    const el = document.querySelector('#doc-inner [data-mx="' + id + '"]');
    if (!el) throw new Error('no block ' + id + ' on the page');
    el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    return;
  }
  if (name.startsWith('tab:')) {
    const tabs = document.querySelectorAll('#tabs button');
    const n = Number(name.slice('tab:'.length));
    if (!tabs[n]) throw new Error('no tab ' + n + '; there are ' + tabs.length);
    tabs[n].click();
    return;
  }
  if (name.startsWith('act:')) {
    const btn = document.querySelector('[data-act="' + name.slice('act:'.length) + '"]');
    if (!btn) throw new Error('no control for ' + name);
    btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    return;
  }
  if (name === 'focus') { const el = editorNow(); if (el) el.focus(); return; }
  if (name === 'blur') { const el = editorNow(); if (el) el.blur(); return; }
  if (name.startsWith('type:')) {
    const el = editorNow();
    if (!el) throw new Error('nothing to type into');
    el.focus();
    el.value = name.slice('type:'.length);
    fire(el, 'input');
    return;
  }
  /* An undo pressed in the chat composer, which Blink can deliver to the
   * source editor behind it as a `historyUndo` input event. Written as the
   * event because that is what the page can see of it. */
  if (name.startsWith('compose:')) {
    const box = document.querySelector('#ibody .composer textarea[data-role]');
    if (!box) throw new Error('no composer on this tab');
    box.focus();
    box.value = name.slice('compose:'.length);
    fire(box, 'input');
    return;
  }
  if (name.startsWith('bleed:')) {
    const el = editorNow();
    if (!el) throw new Error('no editor to bleed into');
    el.value = name.slice('bleed:'.length);
    fire(el, 'input');
    return;
  }
  throw new Error('unknown step ' + name);
}

async function main() {
  await ready();

  const V = window.MSViewer;
  if (!V) { console.error('viewer.js did not install MSViewer'); process.exit(2); }

  const steps = plan.steps;
  if (!steps) {
    frames.forEach((f) => { V.handle(f); });
    report(V, null);
    return;
  }

  let first = null;
  const trail = [];
  steps.forEach((s) => {
    step(V, s);
    const el = editorNow();
    if (el && !first) first = el;
    trail.push(editorTrail(s, el, first));
  });
  report(V, trail);
}

function report(V, trail) {
  const text = (sel) => {
    const el = document.querySelector(sel);
    return el ? el.textContent : null;
  };
  const attr = (el, name) => (el ? el.getAttribute(name) : null);

  const state = V.agentState();
  const ms = V.ext.ms();

  /* The ticker as the AUTHOR reads it: the rendered line, not the entry object.
   * A field that reaches S.ticker and never reaches the row is a field that did
   * not fix anything. */
  const tickerLines = Array.from(document.querySelectorAll('#ticker .tk')).map((row) => {
    const label = row.querySelector('.tkgo, .tklabel');
    return {
      text: label ? label.textContent : row.textContent,
      title: attr(label, 'title'),
      goto: attr(label, 'data-goto'),
    };
  });

  const cites = Array.from(document.querySelectorAll('#doc-inner .citation[data-cites]')).map((c) => ({
    keys: c.getAttribute('data-cites'),
    cls: c.className,
    text: c.textContent,
  }));

  const blocks = {};
  Array.from(document.querySelectorAll('#doc-inner [data-mx]')).forEach((el) => {
    blocks[el.getAttribute('data-mx')] = el.outerHTML;
  });

  const refs = document.querySelector('#doc-inner #refs');

  /* The LaTeX the page is holding for a block, which is what its source editor
   * shows and what the next save is diffed against. Not the same question as
   * what the block LOOKS like: the two come apart on any edit the render does
   * not show. */
  const source = {};
  (plan.source || []).forEach((id) => {
    const b = V.ext.block(id);
    source[id] = b ? String(b.source || '') : null;
  });

  process.stdout.write(JSON.stringify({
    ticker: state.ticker,
    queue: state.queue,
    tickerLines,
    meta: text('#toolbar-meta'),
    agentTitle: attr(document.querySelector('#agent-status'), 'title'),
    docHtml: document.querySelector('#doc-inner').innerHTML,
    refsHtml: refs ? refs.outerHTML : null,
    blocks,
    source,
    order: Object.keys(blocks),
    cites,
    stats: ms.stats || null,
    citeRecords: ms.cites || null,
    sent: window.__sent,
    errors: window.__errors,
    trail: trail,
  }));

  /* `boot` arms a 15-second interval so the ticker keeps ageing, which is
     right in a browser and is a process that never exits here. */
  dom.window.close();
  process.exit(0);
}

main();
