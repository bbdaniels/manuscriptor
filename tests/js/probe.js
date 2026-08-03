/* What the stylesheet actually says about an element of the page the server
 * rendered.
 *
 * `drive.js` answers "what does the page HOLD after a frame lands". This
 * answers a different question the suite could not ask at all: "what style
 * governs this element", resolved by the real cascade out of the real
 * stylesheet, against the real DOM the server built.
 *
 * It is NOT a layout measurement, and nothing here should be read as one.
 * jsdom does no layout -- every `getBoundingClientRect()` in it is a zero box
 * -- so the widths and heights a CSS bug moves cannot be asserted here. What
 * can be asserted is the declaration that decides them, on the element it
 * decides them for. Two CSS defects found on 2026-08-03 were each one declared
 * value away from correct (`overflow-wrap: anywhere` inherited into table
 * cells; a `<td>`'s `<p>` keeping its body margin), and both are visible at
 * this level. Their layout consequence was measured in a real browser and
 * written into the test's docstring, because that is the only place it can
 * honestly live.
 *
 * jsdom's own `getComputedStyle` does not inherit -- it reports the declared
 * value for that element and an empty string otherwise -- so inherited
 * properties are resolved here by walking ancestors, which is the inheritance
 * the browser performs.
 *
 * Usage:  node probe.js <page.html> <probes.json>  -> a JSON report on stdout
 *
 * probes.json: [{"sel": "...", "props": ["margin-top"],
 *                "inherited": ["overflow-wrap"], "nth": 0}]
 */
'use strict';

const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (e) => { console.error(String((e && e.stack) || e)); });
['log', 'info', 'warn', 'error', 'debug'].forEach((level) => {
  virtualConsole.on(level, (...args) => { console.error('[page]', ...args); });
});

const html = fs.readFileSync(process.argv[2], 'utf8');
const probes = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

/* No scripts. The question is what the stylesheet says about the page as
 * served, and hydration cannot change a declaration. */
const dom = new JSDOM(html, { virtualConsole });
const { window } = dom;
const { document } = window;

function declared(el, prop) {
  return window.getComputedStyle(el).getPropertyValue(prop) || '';
}

/* The nearest ancestor (self first) that declares `prop`, which for an
 * inherited property is the value in force on `el`. */
function inherited(el, prop) {
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    const v = declared(node, prop);
    if (v) {
      return { value: v, from: node.tagName.toLowerCase() + (node.className ? '.' + String(node.className).split(/\s+/)[0] : '') };
    }
  }
  return { value: '', from: null };
}

const report = probes.map((p) => {
  const all = [...document.querySelectorAll(p.sel)];
  const el = all[p.nth || 0];
  if (!el) return { sel: p.sel, found: false, count: all.length };
  const out = { sel: p.sel, found: true, count: all.length, declared: {}, inherited: {} };
  (p.props || []).forEach((prop) => { out.declared[prop] = declared(el, prop); });
  (p.inherited || []).forEach((prop) => { out.inherited[prop] = inherited(el, prop); });
  out.html = el.outerHTML.slice(0, 200);
  return out;
});

process.stdout.write(JSON.stringify(report));
