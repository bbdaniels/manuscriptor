/* Find in the manuscript.
 *
 * Cmd+F, and Ctrl+F with it. The same page is the Mac app's WKWebView and an
 * ordinary browser tab: WKWebView ships no find bar at all, so Cmd+F did
 * nothing there, and in a tab the browser's own bar would open over the top of
 * this one unless the key is taken. Both bindings, `preventDefault` on both.
 *
 * WHAT IS SEARCHED: the rendered prose in the manuscript column, and nothing
 * else. Not the LaTeX -- the author is looking at the paper, and a match in
 * source he cannot see is a match that cannot be shown to him. Not the rail and
 * not the inspector either: the outline repeats every heading, so searching it
 * would count each one twice and offer the copy that is not the paper.
 *
 * WHY THE HIGHLIGHT API AND NOT WRAPPER SPANS. This document is patched live.
 * A paragraph the author is looking at can be replaced by a websocket frame
 * mid-search, and the same markup is diffed and spliced back to disk. Wrapping
 * matches in `<span>`s would mean writing this feature's markup into the one
 * tree that must stay exactly what the server rendered -- and every teardown
 * would be a chance to leave a fragment behind, or to split a text node under
 * a `data-mx` anchor, or to have the highlighter's own writes wake the observer
 * that watches for patches. `CSS.highlights` paints Ranges. It writes NOTHING.
 * Teardown is `CSS.highlights.delete`, and a patch cannot leave orphaned markup
 * because there was never any markup.
 *
 * The one browser that lacks it (Safari before 17.2) still gets the count, the
 * stepping and the scroll -- the same code, from the same match list -- and the
 * current match shown through the native selection, which also writes nothing.
 * That is the ONLY thing the fallback changes. Matching, counting, locating and
 * revealing have one implementation between the two paths.
 *
 * SURVIVING A PATCH. A MutationObserver on `#doc-inner` re-derives the whole
 * match list whenever the document changes, so stale ranges cannot outlive
 * their text. The current match is carried across by its OFFSET IN THE TEXT,
 * not by its ordinal and not by its block id: ids here are content-derived, so
 * an edit renames its own block and a block id is the one anchor guaranteed to
 * have moved. Nothing this file does mutates `#doc-inner`, so the observer
 * cannot see its own work and loop.
 */
(function () {
  'use strict';

  var HAVE_DOC = typeof document !== 'undefined' && !!document.createElement;

  // ---------------------------------------------------------- the matcher

  function reEscape(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  /* Every match of `query` in `text`, as [start, end) pairs, case-insensitively
     and without overlap -- a match consumes its own text, so "aa" in "aaaa" is
     two and not three.

     The query is a STRING, never a pattern: an author searching for `p(x)` gets
     those four characters, and an unbalanced bracket must not throw the page
     over. Its own whitespace is the exception, and it has to be: pandoc wraps
     prose inside a paragraph, so the rendered text carries line breaks the
     author never typed and a two-word query would fail on exactly the phrases
     worth searching for. One space in the box matches any run of whitespace. */
  function matches(text, query) {
    var hay = String(text == null ? '' : text);
    var q = String(query == null ? '' : query).trim();
    if (!q) return [];
    var pattern = q.split(/\s+/).map(reEscape).join('\\s+');
    var re;
    try { re = new RegExp(pattern, 'gi'); } catch (err) { return []; }
    var out = [];
    var m;
    while ((m = re.exec(hay)) !== null) {
      if (!m[0].length) { re.lastIndex++; continue; }
      out.push([m.index, m.index + m[0].length]);
    }
    return out;
  }

  /* Next and previous, wrapping. Past the last match is the first one: there is
     no end of the paper to fall off. -1 in means nothing is current yet, so the
     first Enter lands on the first match rather than on the second. */
  function step(i, n, delta) {
    var total = n | 0;
    if (total <= 0) return -1;
    var at = (i | 0) + (delta | 0);
    return ((at % total) + total) % total;
  }

  /* The match nearest a remembered offset. This is how the current match
     survives a patch: the list is rebuilt from the new document and the author
     stays on the occurrence closest to where he was reading. A tie goes to the
     earlier match so the choice is not a coin flip. */
  function nearest(starts, target) {
    var list = starts || [];
    if (!list.length) return -1;
    var best = 0;
    var gap = Math.abs(list[0] - target);
    for (var i = 1; i < list.length; i++) {
      var d = Math.abs(list[i] - target);
      if (d < gap) { gap = d; best = i; }
    }
    return best;
  }

  /* Where a match sits once the flat text is mapped back onto the text nodes it
     was concatenated from. `lens` is those nodes' lengths in order.

     The two ends are found by different rules on purpose. A start offset that
     lands on a seam belongs to the HEAD of the next node; an end offset that
     lands on a seam belongs to the TAIL of the one it finished. Treating them
     alike collapses a match that ends exactly at a node boundary into a
     zero-width range, which the browser paints nowhere. */
  function locate(lens, s, e) {
    var sizes = lens || [];
    var a = 0, ao = 0, b = 0, bo = 0;
    var acc = 0;
    var i;
    for (i = 0; i < sizes.length; i++) {
      if (s >= acc && s < acc + sizes[i]) { a = i; ao = s - acc; break; }
      acc += sizes[i];
    }
    if (i >= sizes.length && sizes.length) {
      a = sizes.length - 1;
      ao = sizes[a];
    }
    acc = 0;
    for (i = 0; i < sizes.length; i++) {
      if (e > acc && e <= acc + sizes[i]) { b = i; bo = e - acc; break; }
      acc += sizes[i];
    }
    if (i >= sizes.length && sizes.length) {
      b = sizes.length - 1;
      bo = sizes[b];
    }
    return { a: a, ao: ao, b: b, bo: bo };
  }

  // ----------------------------------------------------------- the document

  /* Chrome the viewer inserts INTO a block: the ¶/line gutter label, the chat
     pin, the insert-after button. It is addressed here by the same class names
     `hydrateBlock` gives it, because that is where it is decided; a match in
     any of them is a match in furniture the author is not reading. */
  var FURNITURE = '.tag,.pin,.add,script,style,[aria-hidden="true"]';

  var S = {
    open: false,
    query: '',
    hits: [],          // [start, end] into the flat text
    ranges: [],        // one Range per hit, in the same order
    cur: -1,
    anchor: -1,        // the current match's text offset, carried over a patch
    restore: null      // what had focus when the bar opened
  };

  var barEl = null;
  var inputEl = null;
  var countEl = null;
  var observer = null;
  var sizer = null;
  var pending = null;

  function docEl() { return document.getElementById('doc'); }
  function innerEl() { return document.getElementById('doc-inner'); }

  function inFurniture(node, root) {
    var el = node.parentNode;
    while (el && el !== root) {
      if (el.matches && el.matches(FURNITURE)) return true;
      el = el.parentNode;
    }
    return false;
  }

  /* The manuscript column as one string, with the text nodes it came from kept
     beside it so a match can be handed back to the DOM. */
  function collect() {
    var root = innerEl();
    var out = { text: '', nodes: [], lens: [] };
    if (!root || !document.createTreeWalker) return out;
    var walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var parts = [];
    var n;
    while ((n = walk.nextNode())) {
      if (!n.nodeValue || inFurniture(n, root)) continue;
      parts.push(n.nodeValue);
      out.nodes.push(n);
      out.lens.push(n.nodeValue.length);
    }
    out.text = parts.join('');
    return out;
  }

  function haveHighlightApi() {
    return typeof CSS !== 'undefined' && CSS && CSS.highlights &&
      typeof Highlight === 'function';
  }

  function clearPaint() {
    if (haveHighlightApi()) {
      CSS.highlights.delete('ms-find');
      CSS.highlights.delete('ms-find-current');
    }
    if (typeof window !== 'undefined' && window.getSelection) {
      var sel = window.getSelection();
      // Only ours. A selection the author made in the inspector is his.
      if (sel && sel.rangeCount && S.ranges.length) {
        try { sel.removeAllRanges(); } catch (err) { /* nothing to drop */ }
      }
    }
  }

  /* Rebuild the match list from whatever the document now holds. Called on
     every keystroke and on every mutation of the manuscript column, which is
     what makes a patch survivable: there is no incremental state to go stale. */
  function scan(keepAnchor) {
    var doc = collect();
    S.hits = matches(doc.text, S.query);
    S.ranges = [];
    for (var i = 0; i < S.hits.length; i++) {
      var at = locate(doc.lens, S.hits[i][0], S.hits[i][1]);
      var r = document.createRange();
      try {
        r.setStart(doc.nodes[at.a], at.ao);
        r.setEnd(doc.nodes[at.b], at.bo);
      } catch (err) { continue; }
      S.ranges.push(r);
    }
    var starts = S.hits.map(function (h) { return h[0]; });
    if (!S.ranges.length) S.cur = -1;
    else if (keepAnchor && S.anchor >= 0) S.cur = nearest(starts, S.anchor);
    else S.cur = 0;
    rememberAnchor();
  }

  function rememberAnchor() {
    S.anchor = (S.cur >= 0 && S.hits[S.cur]) ? S.hits[S.cur][0] : -1;
  }

  function paint() {
    clearPaint();
    if (!S.open || !S.ranges.length) return;
    if (haveHighlightApi()) {
      var rest = new Highlight();
      var got = 0;
      for (var i = 0; i < S.ranges.length; i++) {
        if (i === S.cur) continue;
        rest.add(S.ranges[i]);
        got++;
      }
      if (got) CSS.highlights.set('ms-find', rest);
      if (S.cur >= 0) CSS.highlights.set('ms-find-current', new Highlight(S.ranges[S.cur]));
      return;
    }
    /* No Highlight API. The current match through the native selection, which
       is the only other way to colour a range without writing markup into the
       manuscript. Everything above this point was the same code. */
    if (S.cur < 0 || typeof window === 'undefined' || !window.getSelection) return;
    var sel = window.getSelection();
    if (!sel) return;
    try {
      sel.removeAllRanges();
      sel.addRange(S.ranges[S.cur]);
    } catch (err) { /* a browser that will not take it simply shows the count */ }
  }

  function count() {
    if (!countEl) return;
    var total = S.ranges.length;
    countEl.textContent = total ? (S.cur + 1) + '/' + total : '0/0';
    countEl.classList.toggle('none', !total && !!S.query.trim());
  }

  /* The block a match sits in, by the same `data-mx` anchor the rest of the
     viewer addresses blocks with. Used to scroll when there is no layout to
     measure, and never to build a second block registry. */
  function blockOf(range) {
    var el = range && range.startContainer;
    if (el && el.nodeType === 3) el = el.parentNode;
    while (el && el.getAttribute && !el.hasAttribute('data-mx')) el = el.parentNode;
    return (el && el.getAttribute) ? el : null;
  }

  function reveal() {
    var range = S.ranges[S.cur];
    var col = docEl();
    if (!range || !col) return;
    var rect = range.getBoundingClientRect ? range.getBoundingClientRect() : null;
    var box = col.getBoundingClientRect ? col.getBoundingClientRect() : null;
    if (rect && box && (rect.height || rect.width)) {
      // The bar sits at the top of the column, so leave it room.
      var pad = 72;
      if (rect.top < box.top + pad || rect.bottom > box.bottom - 24) {
        col.scrollTop += (rect.top - box.top) - Math.max(pad, box.height / 3);
      }
      return;
    }
    // Nothing to measure. The block that holds the match is the address the
    // viewer already uses for exactly this.
    var host = blockOf(range);
    if (!host || typeof host.scrollIntoView !== 'function') return;
    try { host.scrollIntoView({ block: 'center' }); } catch (err) { /* no layout */ }
  }

  function go(delta) {
    if (!S.ranges.length) { count(); return; }
    S.cur = step(S.cur, S.ranges.length, delta);
    rememberAnchor();
    paint();
    count();
    reveal();
  }

  function retype(value) {
    S.query = String(value == null ? '' : value);
    S.anchor = -1;
    scan(false);
    paint();
    count();
    if (S.cur >= 0) reveal();
  }

  /* The document moved under the search. Re-derive everything and carry the
     author to whichever match is now nearest where he was. Deferred by a tick
     because one patch arrives as several mutations. */
  function onMutation() {
    if (!S.open) return;
    if (pending) return;
    pending = setTimeout(function () {
      pending = null;
      if (!S.open) return;
      scan(true);
      paint();
      count();
    }, 0);
  }

  // ------------------------------------------------------------------ the bar

  function styles() {
    if (document.getElementById('ms-find-css')) return;
    var s = document.createElement('style');
    s.id = 'ms-find-css';
    /* Tokens throughout, never a literal colour: the bar has to follow both
       skins and whatever hue the author turned the wheel to. `::highlight()`
       resolves custom properties against the element the text belongs to, so
       the paint follows the skin for free -- which is also why the bar itself
       is appended inside `.app` and not to the body. Glass redefines the
       neutrals ON `.app`, and a bar outside it would render in Instrument's
       palette over Glass's ground. */
    s.textContent = [
      '.msfind{position:fixed;z-index:58;display:flex;align-items:center;gap:.25rem;',
      'padding:.3rem .35rem;border:1px solid var(--rule);border-radius:7px;',
      'background:var(--surface);color:var(--ink);box-shadow:var(--shadow);',
      'font-family:var(--ui);}',
      '.msfind .msfind-q{font:inherit;font-size:.78rem;width:14rem;padding:.26rem .45rem;',
      'border:1px solid var(--rule);border-radius:5px;background:var(--sunken);color:var(--ink);}',
      '.msfind .msfind-q:focus{outline:2px solid var(--accent);outline-offset:-2px;}',
      '.msfind .msfind-n{font-family:var(--mono);font-size:.68rem;color:var(--muted);',
      'min-width:2.8rem;text-align:center;font-variant-numeric:tabular-nums;}',
      '.msfind .msfind-n.none{color:var(--missing);}',
      '.msfind button{font:inherit;font-size:.76rem;line-height:1;padding:.26rem .42rem;',
      'border:1px solid transparent;border-radius:5px;background:transparent;',
      'color:var(--muted);cursor:pointer;}',
      '.msfind button:hover{border-color:var(--accent);color:var(--accent);',
      'background:var(--accent-soft);}',
      '.app[data-skin="glass"] .msfind{border-radius:999px;',
      'background:hsl(var(--h) calc(38% * var(--sat)) 27% / .72);backdrop-filter:blur(24px) saturate(140%);',
      'border-color:rgba(255,255,255,.14);}',
      '.app[data-skin="glass"] .msfind .msfind-q,',
      '.app[data-skin="glass"] .msfind button{border-radius:999px;}',
      '::highlight(ms-find){background-color:var(--accent-soft);color:var(--ink);}',
      '::highlight(ms-find-current){background-color:var(--accent);color:var(--surface);}'
    ].join('');
    document.head.appendChild(s);
  }

  /* Anchored to the manuscript column's own top-right corner rather than to the
     window, because the author moves the divide between reading and working and
     a bar pinned to the window would drift off the prose it is searching. */
  function place() {
    if (!barEl) return;
    var col = docEl();
    if (!col || !col.getBoundingClientRect) return;
    var box = col.getBoundingClientRect();
    if (!box.width && !box.height) return;
    barEl.style.top = (box.top + 10) + 'px';
    barEl.style.right = Math.max(10, (typeof window !== 'undefined' ? window.innerWidth : 0) - box.right + 14) + 'px';
  }

  function build() {
    styles();
    barEl = document.createElement('div');
    barEl.className = 'msfind';
    barEl.setAttribute('role', 'search');
    barEl.innerHTML =
      '<input class="msfind-q" type="text" spellcheck="false" autocomplete="off"' +
      ' aria-label="Find in the manuscript" placeholder="Find in the manuscript">' +
      '<span class="msfind-n" aria-live="polite" aria-label="matches">0/0</span>' +
      '<button type="button" data-msfind="prev" title="Previous match (Shift-Enter)"' +
      ' aria-label="Previous match">↑</button>' +
      '<button type="button" data-msfind="next" title="Next match (Enter)"' +
      ' aria-label="Next match">↓</button>' +
      '<button type="button" data-msfind="close" title="Close (Escape)"' +
      ' aria-label="Close find">✕</button>';

    inputEl = barEl.querySelector('.msfind-q');
    countEl = barEl.querySelector('.msfind-n');

    inputEl.addEventListener('input', function () { retype(inputEl.value); });
    /* The arrows and the keys are one implementation, reached two ways. Two
       would drift, and the one that drifts is the one nobody uses. */
    barEl.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest && e.target.closest('[data-msfind]');
      if (!btn) return;
      e.preventDefault();
      var what = btn.getAttribute('data-msfind');
      if (what === 'close') { close(); return; }
      go(what === 'prev' ? -1 : 1);
      if (inputEl) inputEl.focus();
    });

    (document.getElementById('app') || document.body).appendChild(barEl);
  }

  function open() {
    if (!HAVE_DOC) return;
    if (barEl) {
      // Cmd+F on an open bar refocuses and selects, the way every find bar
      // behaves. Clearing it would throw away the search in progress.
      inputEl.focus();
      try { inputEl.select(); } catch (err) { /* not selectable */ }
      return;
    }
    S.restore = document.activeElement;
    S.open = true;
    build();
    place();
    var root = innerEl();
    if (root && typeof MutationObserver === 'function') {
      observer = new MutationObserver(onMutation);
      observer.observe(root, { childList: true, subtree: true, characterData: true });
    }
    if (typeof window !== 'undefined' && window.addEventListener) {
      window.addEventListener('resize', place);
    }
    /* The divide between reading and working is draggable, and dragging it
       resizes the column without resizing the window. Watching the column is
       the only thing that catches both. */
    var col = docEl();
    if (col && typeof ResizeObserver === 'function') {
      sizer = new ResizeObserver(place);
      sizer.observe(col);
    }
    inputEl.focus();
    if (S.query) { inputEl.value = S.query; retype(S.query); }
  }

  function close() {
    if (!barEl) return;
    S.open = false;
    clearPaint();
    S.hits = [];
    S.ranges = [];
    S.cur = -1;
    S.anchor = -1;
    if (observer) { observer.disconnect(); observer = null; }
    if (sizer) { sizer.disconnect(); sizer = null; }
    if (pending) { clearTimeout(pending); pending = null; }
    if (typeof window !== 'undefined' && window.removeEventListener) {
      window.removeEventListener('resize', place);
    }
    if (barEl.parentNode) barEl.parentNode.removeChild(barEl);
    barEl = null;
    inputEl = null;
    countEl = null;
    var back = S.restore;
    S.restore = null;
    if (back && back.focus && document.contains(back)) {
      try { back.focus(); } catch (err) { /* it went away with a patch */ }
    }
  }

  /* Capture, so the bar answers before the viewer does. Escape already means
     "drop the selection"; with find open it means "close find", and the
     paragraph the author had open has to survive -- otherwise looking something
     up costs him his place. Propagation is stopped for exactly that key and no
     other. */
  function onKey(e) {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && String(e.key).toLowerCase() === 'f') {
      e.preventDefault();
      open();
      return;
    }
    if (!barEl) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === 'Enter' && e.target === inputEl) {
      e.preventDefault();
      e.stopPropagation();
      go(e.shiftKey ? -1 : 1);
    }
  }

  if (HAVE_DOC && document.addEventListener) {
    document.addEventListener('keydown', onKey, true);
  }

  /* Nothing to register but the name: find owns no panel, no toolbar action and
     no frame. It watches the document itself, which is what lets it survive a
     patch that arrives while it is open without the viewer having to know it
     exists. */
  if (typeof MSViewer !== 'undefined' && MSViewer && MSViewer.extend) {
    MSViewer.extend({ name: 'find' });
  }

  // Exported for tests, which run this file under node with no document.
  if (typeof module === 'object' && module.exports) {
    module.exports = {
      _matches: matches,
      _step: step,
      _nearest: nearest,
      _locate: locate,
      _open: open,
      _close: close,
      _state: function () { return { open: S.open, query: S.query, cur: S.cur, n: S.ranges.length }; }
    };
  }
})();
