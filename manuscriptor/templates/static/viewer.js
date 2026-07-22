/* Manuscriptor viewer.
 *
 * Wrapped as a UMD-ish module so the pure logic can be required under node and
 * tested without a browser. The DOM half only runs when there is a document.
 *
 * Four behaviours here are load-bearing and are commented where they live:
 *
 *   1. A DRAFT BELONGS TO ITS BLOCK, not to the panel showing it. The inspector
 *      is rebuilt on every selection change and every patch, so a draft held in
 *      the panel would be destroyed constantly. Drafts live in S.drafts and are
 *      mirrored into localStorage, so neither a rebuild nor a reload can eat
 *      typed work.
 *   2. THERE IS NO SAVE BUTTON. An edit is sent after a pause and on blur, and
 *      only when the LaTeX is plausibly balanced. When it is not, it is HELD
 *      and the panel says why, because a half-typed \citep{ reaching the file
 *      breaks the render and the compiler.
 *   3. A PATCH NEVER TOUCHES THE BLOCK THAT HAS FOCUS. It waits for blur.
 *      Otherwise the author's own save re-renders the paragraph under their
 *      cursor and fights them.
 *   4. THREE COLUMNS, THREE SCROLLS. A patch restores all three, plus the
 *      caret and any half-typed box.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) { module.exports = api; }
  else { root.MSViewer = api; }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // ------------------------------------------------------------------ config

  var MS_DRAFT_PREFIX = 'ms:draft:';
  var MS_PREF_PREFIX = 'ms:pref:';
  var SAVE_DEBOUNCE_MS = 800;

  /* Commands whose meaning depends on an argument that has not been typed yet.
     A trailing name that is a PREFIX of one of these is someone mid-word:
     \c, \ci, \cit, \cite all hold. A trailing \clearpage or \noindent is a
     complete command and saves, which is why this is a list rather than a
     blanket "ends with a backslash-command" rule. */
  var ARG_COMMANDS = [
    'cite', 'citep', 'citet', 'citealp', 'citealt', 'citeauthor', 'citeyear',
    'autocite', 'parencite', 'textcite',
    'ref', 'autoref', 'eqref', 'pageref', 'cref', 'Cref', 'nameref',
    'input', 'include', 'includegraphics',
    'label', 'caption', 'footnote', 'footnotetext',
    'emph', 'textbf', 'textit', 'texttt', 'textsc', 'textsuperscript',
    'underline', 'uline', 'href', 'url', 'texorpdfstring',
    'section', 'subsection', 'subsubsection', 'paragraph', 'title',
    'begin', 'end', 'frac', 'text', 'mathrm', 'num', 'SI'
  ];

  // ------------------------------------------------------------ pure helpers

  /* The marker contract carries a block id WITHOUT its "b-" prefix, so a
     harvest that forgets to put it back would leave every block unaddressable.
     Accept both spellings and treat the prefixed form as canonical. */
  function normId(v) {
    if (v === null || v === undefined) return '';
    v = String(v);
    return v.slice(0, 2) === 'b-' ? v : 'b-' + v;
  }

  /* Keyed by BLOCK, never by panel, tab or position. This is the whole of
     behaviour 1: the same block gives the same key from anywhere. */
  function draftKey(docKey, blockId) {
    return MS_DRAFT_PREFIX + String(docKey) + ':' + String(blockId);
  }

  function spliceAt(text, at, insert) {
    text = String(text === null || text === undefined ? '' : text);
    at = Math.max(0, Math.min(text.length, at | 0));
    return { text: text.slice(0, at) + insert + text.slice(at), caret: at + insert.length };
  }

  function held(reason) { return { ok: false, reason: reason }; }

  function isPrefixOfArgCommand(name) {
    for (var i = 0; i < ARG_COMMANDS.length; i++) {
      if (ARG_COMMANDS[i].indexOf(name) === 0) return true;
    }
    return false;
  }

  /* Is this block plausibly balanced LaTeX?
   *
   * Not a parser and not trying to be. It answers one question: would writing
   * this to the .tex file right now break the render for everybody. Braces and
   * environments are the two things that do, and a command whose argument has
   * not been typed yet is the third. */
  function validateLatex(text) {
    text = String(text === null || text === undefined ? '' : text);

    var depth = 0;
    var bare = '';           // the text with comments removed, for env matching
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (ch === '\\') {           // a backslash escapes exactly one character
        bare += ch + text.charAt(i + 1);
        i++;
        continue;
      }
      if (ch === '%') {            // a comment runs to the end of the line
        while (i < text.length && text.charAt(i) !== '\n') i++;
        bare += '\n';
        continue;
      }
      if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth < 0) return held('A closing brace } arrives before anything opened it.');
      }
      bare += ch;
    }
    if (depth > 0) {
      return held('Unbalanced brace: ' + depth + ' more { than }. Close it and it will save.');
    }

    var envs = [];
    var re = /\\(begin|end)\{([^}]*)\}/g;
    var m;
    while ((m = re.exec(bare)) !== null) {
      if (m[1] === 'begin') envs.push(m[2]);
      else {
        var open = envs.pop();
        if (open === undefined) return held('\\end{' + m[2] + '} with no \\begin{' + m[2] + '}.');
        if (open !== m[2]) return held('\\begin{' + open + '} is closed by \\end{' + m[2] + '}.');
      }
    }
    if (envs.length) {
      var last = envs[envs.length - 1];
      return held('\\begin{' + last + '} has no matching \\end{' + last + '}.');
    }

    var tail = text.replace(/\s+$/, '');
    var t = /(\\+)([a-zA-Z@]*)$/.exec(tail);
    if (t) {
      var odd = t[1].length % 2 === 1;   // an even run is \\, an escaped backslash
      if (!t[2] && odd) {
        return held('It ends in a lone backslash, which is not a command yet.');
      }
      if (t[2] && odd && isPrefixOfArgCommand(t[2])) {
        return held('It ends mid-command (\\' + t[2] + '), whose argument is missing.');
      }
    }

    return { ok: true, reason: null };
  }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* A picked colour carries its saturation as well as its hue, so a muted
     slate gives a muted space instead of snapping back to a saturated one. */
  function hexToHS(hex) {
    var r = parseInt(hex.slice(1, 3), 16) / 255,
        g = parseInt(hex.slice(3, 5), 16) / 255,
        b = parseInt(hex.slice(5, 7), 16) / 255,
        max = Math.max(r, g, b), min = Math.min(r, g, b),
        d = max - min, l = (max + min) / 2, h = 0, sat = 0;
    if (d) {
      sat = d / (1 - Math.abs(2 * l - 1));
      if (max === r) h = 60 * (((g - b) / d) % 6);
      else if (max === g) h = 60 * ((b - r) / d + 2);
      else h = 60 * ((r - g) / d + 4);
      if (h < 0) h += 360;
    }
    return { h: Math.round(h), s: Math.max(0.25, Math.min(1.6, sat * 1.35)) };
  }

  function ago(iso) {
    if (!iso) return '';
    var then = Date.parse(iso);
    if (isNaN(then)) return String(iso);
    var s = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (s < 5) return 'just now';
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    return Math.round(s / 3600) + 'h ago';
  }

  var api = {
    validateLatex: validateLatex,
    normId: normId,
    draftKey: draftKey,
    spliceAt: spliceAt,
    hexToHS: hexToHS,
    MS_DRAFT_PREFIX: MS_DRAFT_PREFIX
  };

  if (typeof document === 'undefined') return api;   // required under node

  // ------------------------------------------------------------------- state

  var S = {
    ms: {},
    docKey: 'doc',
    blocks: {},
    chats: {},
    order: [],              // block ids in document order
    sel: null,              // {kind, key, blockId}
    tabMemory: {},          // selection key -> tab index
    tab: 0,
    focusedBlock: null,     // the block whose editor has focus, if any
    deferredPatches: {},    // behaviour 3: patches waiting for a blur
    drafts: {},             // behaviour 1: block id -> unsaved source
    restored: {},           // block id -> the draft came back from storage
    save: {},               // block id -> {state, reason, at}
    sent: {},               // block id -> the text last sent, to confirm against
    blockState: {},         // block id -> queued|working|done|locked
    caret: null,            // {id, start, end, scrollTop}
    sock: null,
    sockState: 'connecting',
    saveTimer: null,
    retry: 0
  };

  var appEl, railEl, docEl, inspEl, docInner, ibodyEl, tabsEl,
      eyebrowEl, ititleEl, isubEl, jumpBtn, liveEl, liveTextEl, metaEl,
      outlineEl, pathEl, hueWheel;

  var anchorEl = null, io = null, warnedBareId = false;

  // ----------------------------------------------------------------- storage

  function store(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }
  function load(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function drop(key) {
    try { window.localStorage.removeItem(key); } catch (e) { /* ignore */ }
  }

  function setDraft(id, text) {
    S.drafts[id] = text;
    store(draftKey(S.docKey, id), text);
    markDirty(id, true);
  }
  function clearDraft(id) {
    delete S.drafts[id];
    delete S.restored[id];
    drop(draftKey(S.docKey, id));
    markDirty(id, false);
  }
  function draftOf(id) {
    return Object.prototype.hasOwnProperty.call(S.drafts, id) ? S.drafts[id] : null;
  }
  function sourceOf(id) {
    var d = draftOf(id);
    if (d !== null) return d;
    var b = S.blocks[id];
    return b ? String(b.source || '') : '';
  }

  /* A reload must not eat work either, so the drafts come back off disk before
     anything is rendered. A draft that now matches the file is dropped: the
     author's change landed while they were away. */
  function restoreDrafts() {
    var ids = Object.keys(S.blocks);
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      var text = load(draftKey(S.docKey, id));
      if (text === null) continue;
      if (text === String(S.blocks[id].source || '')) { drop(draftKey(S.docKey, id)); continue; }
      S.drafts[id] = text;
      S.restored[id] = true;
    }
  }

  // -------------------------------------------------------------- hydration

  function nodeFromHtml(html) {
    var t = document.createElement('template');
    t.innerHTML = String(html === null || html === undefined ? '' : html).trim();
    return t.content.firstElementChild;
  }

  var EXHIBIT_KINDS = { table: 1, figure: 1, generated: 1 };

  function hydrateBlock(el, ordinal) {
    var raw = el.getAttribute('data-mx');
    var id = normId(raw);
    if (raw !== id && !warnedBareId) {
      warnedBareId = true;
      if (window.console) {
        console.warn('data-mx carries the bare marker id (' + raw + '). ' +
          'render/postprocess.py should write the canonical "b-" form.');
      }
    }
    el.setAttribute('data-mx', id);

    var b = S.blocks[id] || {};
    var kind = b.kind || '';
    var isExhibit = EXHIBIT_KINDS[kind] === 1 ||
      el.tagName === 'TABLE' || el.tagName === 'FIGURE';

    el.classList.add(isExhibit ? 'exhibit' : 'blk');
    if (kind === 'heading' || /^H[1-6]$/.test(el.tagName)) el.classList.add('is-heading');
    if (b.editable === false) el.classList.add('is-locked');
    if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');

    // Chrome we own, removed first so re-hydration after a patch cannot double it.
    var mine = el.querySelectorAll(':scope > .tag, :scope > .pin, :scope > .add');
    for (var i = 0; i < mine.length; i++) mine[i].remove();

    if (!isExhibit && !el.classList.contains('is-heading')) {
      var tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = '¶' + ordinal + (b.line_start ? ' · ' + b.line_start : '');
      el.insertBefore(tag, el.firstChild);
    }

    var st = S.blockState[id];
    var chat = (S.chats[id] || []).length;
    if (st || chat) {
      var pin = document.createElement('button');
      pin.type = 'button';
      pin.className = 'pin' + (st ? ' ' + st : '');
      pin.setAttribute('data-goto-chat', id);
      pin.title = st ? st : chat + ' in the chat on this block';
      pin.textContent = st === 'done' ? '✓' : (st === 'working' ? '●' : (st === 'locked' ? '⊘' : String(chat || '·')));
      el.insertBefore(pin, el.firstChild);
    }

    markDirty(id, draftOf(id) !== null);
    return id;
  }

  function hydrateSpans(scope) {
    // pandoc emits <span class="citation" data-cites="key1 key2">
    var cites = scope.querySelectorAll('.citation[data-cites]');
    for (var i = 0; i < cites.length; i++) {
      var c = cites[i];
      var keys = String(c.getAttribute('data-cites') || '').split(/\s+/).filter(Boolean);
      if (!keys.length) continue;
      c.classList.add('cite');
      c.setAttribute('data-key', keys[0]);
      if (!c.hasAttribute('tabindex')) c.setAttribute('tabindex', '0');
      var rec = (S.ms.cites || {})[keys[0]];
      if (rec && rec.status) {
        c.classList.add(rec.status === 'verbatim' ? 'verbatim'
          : rec.status === 'paraphrase' ? 'para' : 'miss');
      }
    }
    // computed values, marked by the render pass
    var vals = scope.querySelectorAll('[data-mx-value]');
    for (var j = 0; j < vals.length; j++) {
      vals[j].classList.add('val');
      vals[j].setAttribute('data-key', vals[j].getAttribute('data-mx-value'));
      if (!vals[j].hasAttribute('tabindex')) vals[j].setAttribute('tabindex', '0');
    }
    var already = scope.querySelectorAll('.val[data-key]');
    for (var k = 0; k < already.length; k++) {
      if (!already[k].hasAttribute('tabindex')) already[k].setAttribute('tabindex', '0');
    }
  }

  function hydrate() {
    var els = docInner.querySelectorAll('[data-mx]');
    S.order = [];
    var n = 0;
    for (var i = 0; i < els.length; i++) {
      var b = S.blocks[normId(els[i].getAttribute('data-mx'))] || {};
      if (!EXHIBIT_KINDS[b.kind] && b.kind !== 'heading') n++;
      S.order.push(hydrateBlock(els[i], n));
    }
    hydrateSpans(docInner);
  }

  function blockEl(id) {
    return docInner.querySelector('[data-mx="' + String(id).replace(/"/g, '\\"') + '"]');
  }

  function markDirty(id, on) {
    var el = blockEl(id);
    if (el) el.classList.toggle('dirty', !!on);
  }

  // --------------------------------------------------- three scrolls, one caret

  function captureUI() {
    var active = document.activeElement;
    var field = null;
    if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT')) {
      field = {
        role: active.getAttribute('data-role') || '',
        start: active.selectionStart,
        end: active.selectionEnd,
        scroll: active.scrollTop
      };
    }
    return {
      rail: railEl ? railEl.scrollTop : 0,
      doc: docEl ? docEl.scrollTop : 0,
      insp: ibodyEl ? ibodyEl.scrollTop : 0,
      field: field
    };
  }

  // `keepDoc` false means the caller is deliberately navigating, so the
  // document's scroll position is the one thing NOT to put back. Without it a
  // jump-to-section scrolls and is then immediately undone by the re-render
  // that follows it, which looks exactly like a dead link.
  function restoreUI(ui, keepDoc) {
    if (!ui) return;
    if (railEl) railEl.scrollTop = ui.rail;
    if (docEl && keepDoc !== false) docEl.scrollTop = ui.doc;
    if (ibodyEl) ibodyEl.scrollTop = ui.insp;
    if (ui.field && ui.field.role) {
      var f = ibodyEl.querySelector('[data-role="' + ui.field.role + '"]');
      if (f) {
        f.focus();
        try { f.setSelectionRange(ui.field.start, ui.field.end); } catch (e) { /* not a text field */ }
        f.scrollTop = ui.field.scroll;
      }
    }
  }

  // ------------------------------------------------------------- the inspector

  function card(title, right, html, flush) {
    return '<div class="card">' +
      (title || right ? '<header><b>' + esc(title) + '</b>' +
        (right ? '<span>' + esc(right) + '</span>' : '') + '</header>' : '') +
      '<div class="in' + (flush ? ' flush' : '') + '">' + html + '</div></div>';
  }

  function composer(role, placeholder, value, hint) {
    return '<div class="composer">' +
      '<textarea data-role="' + esc(role) + '" placeholder="' + esc(placeholder) + '">' +
      esc(value || '') + '</textarea>' +
      '<div class="row"><button class="btn pri" data-act="send:' + esc(role) + '">Send</button>' +
      '<span class="hintline">' + esc(hint || '⌘↵ to send') + '</span></div></div>';
  }

  function selKey() {
    if (!S.sel) return '';
    return S.sel.kind + ':' + S.sel.key;
  }

  function select(kind, key, blockId, tab, opts) {
    var els = document.querySelectorAll('.sel');
    for (var i = 0; i < els.length; i++) els[i].classList.remove('sel');

    S.sel = { kind: kind, key: key, blockId: blockId || null };
    S.tab = tab === undefined ? (S.tabMemory[selKey()] || 0) : tab;

    var anchor = blockId ? blockEl(blockId) : null;
    if (anchor) anchor.classList.add('sel');
    if (io && anchorEl) io.unobserve(anchorEl);
    anchorEl = anchor;
    if (jumpBtn) jumpBtn.hidden = true;
    if (io && anchorEl) io.observe(anchorEl);

    renderInspector(opts && opts.keepDoc);
  }

  function renderInspector(keepDoc) {
    if (!S.sel) return;
    var ui = captureUI();
    var view = null;
    if (S.sel.kind === 'block') view = viewBlock(S.sel.key);
    else if (S.sel.kind === 'cite') view = viewCite(S.sel.key);
    else if (S.sel.kind === 'value') view = viewValue(S.sel.key);
    else view = viewPanel(S.sel.key);
    if (!view) return;

    S.tab = Math.min(S.tab, view.tabs.length - 1);
    S.tabMemory[selKey()] = S.tab;

    eyebrowEl.innerHTML = esc(view.eyebrow) +
      (view.chip ? ' <span class="chip ' + esc(view.chip[0]) + '">' + esc(view.chip[1]) + '</span>' : '');
    ititleEl.textContent = view.title;
    isubEl.textContent = view.sub;

    tabsEl.innerHTML = '';
    view.tabs.forEach(function (t, i) {
      var b = document.createElement('button');
      b.type = 'button';
      b.innerHTML = esc(t.name) + (t.n ? ' <span class="count">' + esc(t.n) + '</span>' : '');
      if (i === S.tab) b.className = 'on';
      b.addEventListener('click', function () { S.tab = i; renderInspector(); });
      tabsEl.appendChild(b);
    });

    ibodyEl.innerHTML = '<div data-role="banners"></div>' + view.tabs[S.tab].body;
    // Measured against the panel's siblings, so it has to run once more after
    // layout settles or a long block sizes itself against a half-built card.
    requestAnimationFrame(autosizeAll);
    renderBanners();

    wireInspector();
    restoreUI(ui, keepDoc);
  }

  /* The banners live in their own container so they can be refreshed without
     rebuilding the panel, which would take the editor and the caret with it. */
  function renderBanners() {
    var box = ibodyEl.querySelector('[data-role="banners"]');
    if (!box) return;
    var id = S.sel && S.sel.blockId;
    var html = '';
    if (id && S.restored[id]) {
      html += '<div class="restored">Unsaved draft restored. It was kept when you clicked away.</div>';
    }
    if (id && draftOf(id) !== null) {
      html += '<div class="dirtybar"><span>Unsaved</span> kept on this block until it saves or you discard it</div>';
    }
    box.innerHTML = html;
  }

  // ------------------------------------------------------------------- views

  function fileLine(b) {
    if (!b) return '';
    var line = b.line_start === b.line_end ? b.line_start : b.line_start + '–' + b.line_end;
    return String(b.file || '') + ':' + line;
  }

  function viewBlock(id) {
    var b = S.blocks[id];
    if (!b) {
      return {
        eyebrow: 'Block', chip: null, title: 'Not in the block map', sub: String(id),
        tabs: [{ name: 'Source', body: '<p class="empty">This element carries a <b>data-mx</b> the server did not describe. It cannot be edited until the next render.</p>' }]
      };
    }
    var chat = S.chats[id] || [];
    var values = b.values || [];
    var cites = b.cites || [];
    return {
      eyebrow: b.kind === 'paragraph' ? 'Paragraph' : (b.kind || 'Block'),
      chip: b.editable === false ? ['c', 'generated'] : null,
      title: titleFor(b),
      sub: fileLine(b) + (values.length ? ' · ' + values.length + ' computed value' + (values.length === 1 ? '' : 's') + ' inside' : ''),
      tabs: [
        { name: 'Source', body: sourceTab(id, b) },
        { name: 'Chat', n: chat.length || 0, body: chatTab(id, chat) },
        { name: 'References', n: values.length + cites.length || 0, body: refsTab(id, b) }
      ]
    };
  }

  function titleFor(b) {
    var head = b.parent_heading ? b.parent_heading : 'Manuscript';
    var text = String(b.source || '').replace(/\\[a-zA-Z]+\**(\[[^\]]*\])?(\{[^}]*\})?/g, ' ')
      .replace(/[{}]/g, '').replace(/\s+/g, ' ').trim();
    if (!text) return head;
    return head + ' — ' + (text.length > 46 ? text.slice(0, 46) + '…' : text);
  }

  function sourceTab(id, b) {
    var state = S.blockState[id];

    if (state === 'locked') {
      return card('', 'locked while working',
        '<div class="locked"><span>◔</span><div><b>Locked.</b> This block is being edited right now. ' +
        'It unlocks when the change lands, and you will see the diff.</div></div>');
    }

    /* Never hand-edit a generated block. Editing it would hardcode a result
       into the manuscript, which is the one thing this tool exists to stop. */
    if (b.editable === false) {
      var producers = (b.values || []).map(function (v) { return v.producer; }).filter(Boolean);
      var who = producers.length ? producers.join(', ') : 'the script that writes ' + (b.file || 'this file');
      return card('This block is generated', fileLine(b),
        '<div class="locked"><span>⊘</span><div><b>Not editable here.</b> This file is written by <b>' +
        esc(who) + '</b>. Editing it would hardcode a result into the manuscript. ' +
        'Change the script and it will rebuild.</div></div>' +
        '<div class="row" style="margin-top:.6rem"><button class="btn" data-act="ask:rerun">Ask for a re-run</button></div>') +
        card('Read-only source', '', '<textarea class="src" readonly data-role="ro">' + esc(b.source || '') + '</textarea>', true);
    }

    var text = sourceOf(id);
    var includes = b.includes || [];
    var incl = includes.length
      ? '<p class="meta" style="padding:.5rem .7rem .6rem">You are holding the unflattened source, so ' +
        includes.map(function (i) { return '<span class="inputtok">' + esc(i.directive) + '</span>'; }).join(' ') +
        ' survive' + (includes.length === 1 ? 's' : '') + ' your edit. The values shown in the page are read from those files.</p>'
      : '<p class="meta" style="padding:.5rem .7rem .6rem">This is the source as it sits in the file, not the flattened render.</p>';

    return card(fileLine(b), 'editable',
      '<textarea class="src" spellcheck="false" data-role="src" data-block="' + esc(id) + '">' + esc(text) + '</textarea>' +
      '<div class="insbar"><span>At cursor</span>' +
      '<button class="ins" type="button" data-open="insert:cite">Citation</button>' +
      '<button class="ins" type="button" data-open="insert:value">Number</button>' +
      '<button class="ins" type="button" data-act="ins:footnote">Footnote</button>' +
      '</div>' +
      '<div class="insbar"><span>After this ¶</span>' +
      '<button class="ins" type="button" data-open="insert:exhibit">Exhibit</button>' +
      '<button class="ins" type="button" data-open="insert:paragraph">New paragraph</button>' +
      '</div>' + incl, true) +
      card('', '', saveBox(id, b), true);
  }

  // The editor should open showing the whole paragraph rather than a 7rem
  // porthole the author has to drag open. But a long block must not push the
  // insert bar past the bottom of the panel, so growth stops at whatever room
  // the inspector actually has once the rest of the card is accounted for.
  function autosize(el) {
    if (!el) return;
    var panel = el.closest('.insp-body') || el.parentElement;
    var others = 0;
    if (panel) {
      for (var i = 0; i < panel.children.length; i++) {
        var c = panel.children[i];
        if (!c.contains(el)) others += c.offsetHeight;
      }
    }
    var room = panel ? panel.clientHeight - others - 150 : 400;
    el.style.height = 'auto';
    el.style.height = Math.max(96, Math.min(el.scrollHeight + 2, Math.max(160, room))) + 'px';
  }

  function autosizeAll() {
    var el = document.querySelector('.insp-body textarea.src');
    if (el) autosize(el);
  }

  function saveStateHtml(id, b) {
    var s = S.save[id] || {};
    var cls = '', text;
    if (s.state === 'held') {
      cls = ' held';
      text = '<b>Held.</b> ' + esc(s.reason) + ' Nothing is written while it would not parse.';
    } else if (s.state === 'pending') {
      cls = ' pending';
      text = '<b>Saving…</b> writes on a pause, not a button.';
    } else if (s.state === 'offline') {
      cls = ' offline';
      text = '<b>Not connected.</b> Your draft is kept here and on disk until the server is back.';
    } else if (s.state === 'saved') {
      text = '<b>Saved</b> to ' + esc(b.file || 'the file') + ', ' + esc(ago(s.at)) + '. Writes on a pause, not a button.';
    } else if (draftOf(id) !== null) {
      text = '<b>Unsaved.</b> It will write about a second after you stop typing.';
    } else {
      text = '<b>No unsaved changes</b> in this block.';
    }
    return '<div class="savestate' + cls + '"><i></i><span>' + text + '</span></div>' +
      '<div class="row" style="padding:0 .7rem .6rem">' +
      '<button class="btn" data-act="revert" ' + (draftOf(id) === null ? 'disabled' : '') + '>Revert to last good</button>' +
      '<button class="btn" data-act="discard" ' + (draftOf(id) === null ? 'disabled' : '') + '>Discard draft</button></div>';
  }

  function saveBox(id, b) {
    return '<div data-role="savebox">' + saveStateHtml(id, b) + '</div>';
  }

  function renderSaveBox(id) {
    var box = ibodyEl.querySelector('[data-role="savebox"]');
    if (box) box.innerHTML = saveStateHtml(id, S.blocks[id]);
  }

  function chatTab(id, chat) {
    // The composer goes at the TOP: the thing you came here to do is type.
    var key = 'chat:' + id;
    var body = card('Ask for a change here', '⌘↵',
      composer(key, 'Cut the second sentence and fold the persistence claim into the first.',
        load(draftKey(S.docKey, key)) || ''));
    if (chat.length) {
      body += card('Earlier', chat.length + ' message' + (chat.length === 1 ? '' : 's'),
        '<div class="chat">' + chat.map(function (m) {
          return '<div class="msg' + (m.who === 'you' ? ' bb' : '') + '">' +
            '<div class="who">' + esc(m.who) + (m.ts ? ' · ' + esc(ago(m.ts)) : '') +
            (m.state ? ' · ' + esc(m.state) : '') + '</div>' + esc(m.body) + '</div>';
        }).join('') + '</div>');
    } else {
      body += card('', '', '<p class="empty">No chat on this block yet. A note here becomes a comment ' +
        'anchored to these bytes, not to a page number.</p>');
    }
    return body;
  }

  function refsTab(id, b) {
    var values = b.values || [];
    var cites = b.cites || [];
    var out = '';

    out += card('Computed values', String(values.length),
      values.length
        ? values.map(function (v) {
            return '<button class="hit" type="button" data-open="value:' + esc(v.key) + '">' +
              '<b>' + esc(v.key) + '</b>' +
              '<span>' + esc(v.description || 'No description derived yet for this fragment.') + '</span>' +
              '<span class="ok">' + esc(v.producer || 'producer unknown') + '</span></button>';
          }).join('')
        : '<p class="meta">None. Any number in this paragraph was typed by hand rather than read from a file some script wrote.</p>' +
          '<div class="row" style="margin-top:.6rem"><button class="btn" data-open="insert:value">Insert a number</button></div>');

    out += card('Citations', String(cites.length),
      cites.length
        ? cites.map(function (k) {
            var rec = (S.ms.cites || {})[k] || {};
            return '<button class="hit" type="button" data-open="cite:' + esc(k) + '">' +
              '<b>' + esc(k) + '</b>' +
              '<span>' + esc(rec.title || 'Open to see what supports this claim.') + '</span>' +
              (rec.status ? '<span class="' + (rec.status === 'verbatim' ? 'ok' : 'warn') + '">' + esc(rec.status) + '</span>' : '') +
              '</button>';
          }).join('')
        : '<p class="meta">None in this block.</p>' +
          '<div class="row" style="margin-top:.6rem"><button class="btn" data-open="insert:cite">Insert a citation</button></div>');

    if ((b.includes || []).length) {
      out += card('Include directives', String(b.includes.length),
        b.includes.map(function (i) {
          return '<div class="hit"><b>' + esc(i.directive) + '</b><span>' + esc(i.target) + '</span></div>';
        }).join('') +
        '<p class="meta" style="margin-top:.5rem">These are read-only inside this block: they are outputs, and the ' +
        'directive is what the editor holds.</p>');
    }
    return out;
  }

  function blocksUsing(pred) {
    return S.order.filter(function (id) { return S.blocks[id] && pred(S.blocks[id]); });
  }

  function viewValue(key) {
    var rec = null;
    var users = blocksUsing(function (b) {
      return (b.values || []).some(function (v) {
        if (v.key === key) { rec = rec || v; return true; }
        return false;
      });
    });
    var extra = (S.ms.values || {})[key] || {};
    rec = rec || extra || {};

    var what = card('What this is', '',
      '<p class="meta"><b>' + esc(rec.description || 'No description has been derived for this fragment yet.') + '</b> ' +
      'A fragment file holds only its value, so this description is derived from the code that writes it and cached ' +
      'beside the fragments. It is hand-editable when the derivation is wrong.</p>');

    var where = card('Where it comes from', '',
      '<dl class="stat">' +
      '<dt>Fragment</dt><dd>' + esc(rec.path || 'unknown') + '</dd>' +
      '<dt>Written by</dt><dd>' + esc(rec.producer || 'unknown') + '</dd>' +
      '</dl>' +
      (extra.code ? '' : '<p class="meta" style="margin-top:.5rem">The producing code is read on demand; nothing is cached in the page.</p>'));

    var code = extra.code ? card(String(rec.producer || 'source'), extra.lines || '', '<pre>' + esc(extra.code) + '</pre>', true) : '';

    var used = card('Reported in', String(users.length),
      users.length
        ? users.map(function (id) {
            var b = S.blocks[id];
            return '<button class="hit" type="button" data-open="block:' + esc(id) + '">' +
              '<b>' + esc(fileLine(b)) + '</b><span>' + esc(titleFor(b)) + '</span></button>';
          }).join('')
        : '<p class="meta">Not currently reported in any block the server described.</p>');

    return {
      eyebrow: 'Provenance', chip: ['c', 'computed'],
      title: key,
      sub: (rec.path || '') + (rec.producer ? ' · written by ' + rec.producer : ''),
      tabs: [
        { name: 'Code', body: what + where + code },
        { name: 'Used', n: users.length || 0, body: used }
      ]
    };
  }

  function viewCite(key) {
    var rec = (S.ms.cites || {})[key] || {};
    var users = blocksUsing(function (b) { return (b.cites || []).indexOf(key) !== -1; });
    var status = rec.status || null;

    var evidence;
    if (rec.quotes && rec.quotes.length) {
      evidence = card('Supporting passages', rec.source || '',
        rec.quotes.map(function (q) {
          return '<p class="quote' + (q.status === 'verbatim' ? '' : ' p') + '">' + esc(q.text) + '</p>';
        }).join('') +
        (rec.reasoning ? '<p class="meta">' + esc(rec.reasoning) + '</p>' : ''));
    } else {
      evidence = card('No evidence loaded', '',
        '<p class="meta">This page carries no evidence record for <b>' + esc(key) + '</b>. ' +
        'The evidence pass writes those records; until it has run on this pair, the underline stays neutral ' +
        'rather than claiming a status it cannot support.</p>');
    }

    var claim = card('Cited in', String(users.length),
      users.length
        ? users.map(function (id) {
            var b = S.blocks[id];
            return '<button class="hit" type="button" data-open="block:' + esc(id) + '">' +
              '<b>' + esc(fileLine(b)) + '</b><span>' + esc(titleFor(b)) + '</span></button>';
          }).join('')
        : '<p class="meta">No block in the map cites this key.</p>');

    return {
      eyebrow: 'Evidence',
      chip: status ? [status === 'verbatim' ? 'v' : status === 'paraphrase' ? 'p' : 'm', status] : null,
      title: rec.title || key,
      sub: key + (users.length ? ' · ' + fileLine(S.blocks[users[0]]) : ''),
      tabs: [
        { name: 'Evidence', n: (rec.quotes || []).length || 0, body: evidence },
        { name: 'Claim', n: users.length || 0, body: claim }
      ]
    };
  }

  /* Insertion is a coordinated multi-file write, and that is the argument for
     this tool existing: a citation touches three places, a number three, an
     exhibit four. None of those is a single-block edit, so none of them can go
     down the `edit` channel. They are requests to the agent, which is reached
     through the chat on this block. Saying so plainly beats a button that
     pretends to do it. */
  var INSERTS = {
    'insert:cite': {
      title: 'Add a citation here', chip: ['v', 'citation'],
      writes: [['the paragraph', '\\citep{key} at the cursor'],
               ['references.bib', '+1 entry, from the Crossref record'],
               ['Zotero', 'a record and a PDF, if it is not already held']],
      gate: 'Nothing is inserted on a key that fails the identity gate: a DOI, Crossref and OpenAlex agreeing, ' +
            'and a Zotero record with indexed fulltext. A citation you cannot resolve is a citation you cannot check.',
      ask: 'Cite a paper here that supports this claim. Check the identity gate before inserting.'
    },
    'insert:value': {
      title: 'Add a number here', chip: ['c', 'computed value'],
      writes: [['a new fragment file', 'the value alone'],
               ['the producing script', '+1 write_frag line, after the model'],
               ['the paragraph', '\\input{...} at the cursor']],
      gate: 'There is no field for a literal number. A value enters the manuscript only as an \\input of a file ' +
            'some script wrote, so the no-hardcoded-results rule is enforced by there being no other door. ' +
            'Inserting a number is a change to your analysis code.',
      ask: 'Add a computed value here. Say what it should estimate; write it into the right script and \\input the fragment.'
    },
    'insert:exhibit': {
      title: 'Add a table or figure', chip: ['c', 'exhibit'],
      writes: [['a script', 'new or extended, reusing an existing sample'],
               ['the generated .tex', 'its output'],
               ['the manuscript', 'a NEW block: float + \\include + caption + \\label'],
               ['the runfile', '+1 line, so it rebuilds with everything else']],
      gate: 'The runfile line matters more than it looks. An exhibit that is not in the runfile goes stale the next ' +
            'time you rebuild, and you will not notice until a referee does.',
      ask: 'Add an exhibit after this paragraph. Describe what it should show.'
    },
    'insert:paragraph': {
      title: 'Add a paragraph after this one', chip: null,
      writes: [['the manuscript', 'a NEW block spliced between this paragraph and the next']],
      gate: 'This is a new block, not an edit to this one. Content-derived ids mean nothing below it moves.',
      ask: 'Draft a new paragraph after this one that says: '
    },
    'import:reviewer': {
      title: 'Bring outside markup in', chip: null,
      writes: [['nothing yet', 'a PDF, a .docx of tracked changes, or a response letter']],
      gate: 'Every annotation is matched back to the paragraph it sits on using the text the commenter highlighted, ' +
            'not the page number, so the anchors survive your rewrites. Anything that cannot be placed confidently ' +
            'waits in a tray rather than attaching itself to the wrong sentence.',
      ask: 'Read in the marked-up file and anchor each comment to its paragraph.'
    }
  };

  function viewPanel(key) {
    var d = INSERTS[key];
    if (!d) {
      return { eyebrow: 'Nothing open', chip: null, title: String(key), sub: '',
        tabs: [{ name: 'Detail', body: '<p class="empty">Nothing to show for this.</p>' }] };
    }
    var id = S.sel.blockId;
    var b = id ? S.blocks[id] : null;
    var role = 'ask:' + key + (id ? ':' + id : '');

    var writes = card('What gets written', String(d.writes.length),
      '<dl class="stat">' + d.writes.map(function (w) {
        return '<dt>' + esc(w[0]) + '</dt><dd>' + esc(w[1]) + '</dd>';
      }).join('') + '</dl>' +
      '<p class="meta" style="margin-top:.5rem">A text editor could only ever do the manuscript line. ' +
      'Doing all of them together is why this lives here.</p>');

    var gate = card('', '', '<div class="locked"><span>⊘</span><div>' + esc(d.gate) + '</div></div>');

    var ask = b
      ? card('Ask for it', '⌘↵',
          composer(role, d.ask, load(draftKey(S.docKey, role)) || d.ask,
            'goes to the chat on ' + fileLine(b)))
      : card('', '', '<p class="empty">Select a paragraph first, so the request has somewhere to anchor.</p>');

    return {
      eyebrow: key.split(':')[0] === 'import' ? 'Read in comments' : 'Insert',
      chip: d.chip,
      title: d.title,
      sub: b ? 'at the cursor · ' + fileLine(b) : '',
      tabs: [{ name: 'What happens', body: writes + gate + ask }]
    };
  }

  // ------------------------------------------------------- editing and saving

  function wireInspector() {
    var src = ibodyEl.querySelector('textarea.src[data-role="src"]');
    if (src) {
      var id = src.getAttribute('data-block');

      // Open showing the whole paragraph, not a porthole to drag open.
      autosize(src);

      if (S.caret && S.caret.id === id) {
        try { src.setSelectionRange(S.caret.start, S.caret.end); } catch (e) { /* ignore */ }
        src.scrollTop = S.caret.scrollTop;
      }

      src.addEventListener('input', function () {
        autosize(src);
        setDraft(id, src.value);
        rememberCaret(id, src);
        S.save[id] = { state: 'typing' };
        renderBanners();
        renderSaveBox(id);   // or the line still claims there is nothing unsaved
        scheduleSave(id);
      });
      src.addEventListener('keyup', function () { rememberCaret(id, src); });
      src.addEventListener('click', function () { rememberCaret(id, src); });
      src.addEventListener('focus', function () { S.focusedBlock = id; });
      src.addEventListener('blur', function () {
        S.focusedBlock = null;
        rememberCaret(id, src);
        if (S.saveTimer) { clearTimeout(S.saveTimer); S.saveTimer = null; }
        trySave(id);
        flushDeferred(id);
      });
    }

    var boxes = ibodyEl.querySelectorAll('.composer textarea[data-role]');
    for (var i = 0; i < boxes.length; i++) {
      (function (box) {
        var role = box.getAttribute('data-role');
        box.addEventListener('input', function () {
          store(draftKey(S.docKey, role), box.value);
        });
        box.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendComposer(role); }
        });
      })(boxes[i]);
    }
  }

  function rememberCaret(id, el) {
    S.caret = { id: id, start: el.selectionStart, end: el.selectionEnd, scrollTop: el.scrollTop };
  }

  function scheduleSave(id) {
    if (S.saveTimer) clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(function () { S.saveTimer = null; trySave(id); }, SAVE_DEBOUNCE_MS);
  }

  /* Behaviour 2. No button: a pause of about a second, or a blur, and only if
     what would land is plausibly balanced. When it is not, the draft is HELD
     and the panel says why, rather than nothing happening for reasons the
     author has to guess. */
  function trySave(id) {
    var text = draftOf(id);
    if (text === null) return;
    var b = S.blocks[id];
    if (!b || b.editable === false) return;

    if (text === String(b.source || '')) { clearDraft(id); setSave(id, { state: 'clean' }); return; }

    var v = validateLatex(text);
    if (!v.ok) { setSave(id, { state: 'held', reason: v.reason }); return; }

    S.sent[id] = text;
    if (!send({ type: 'edit', block: id, source: text })) {
      setSave(id, { state: 'offline' });
      return;
    }
    setSave(id, { state: 'pending' });
  }

  /* The save line changes while the author is still typing into the editor a
     few pixels above it. Rebuilding the panel to show that would replace the
     textarea under their cursor, which is exactly the fight behaviour 3
     forbids the watcher from picking. So the save line is refreshed in place
     whenever the block has focus, and only rebuilt when it does not. */
  function setSave(id, s) {
    S.save[id] = s;
    if (S.sel && S.sel.blockId === id) {
      if (S.focusedBlock === id && ibodyEl.querySelector('[data-role="savebox"]')) {
        renderSaveBox(id);
        renderBanners();
      } else {
        renderInspector();
      }
    }
    updateMeta();
  }

  function sendComposer(role) {
    var box = ibodyEl.querySelector('[data-role="' + role + '"]');
    if (!box) return;
    var body = box.value.trim();
    if (!body) return;
    var id = S.sel && S.sel.blockId;
    if (!id) return;
    if (!send({ type: 'chat', block: id, body: body })) return;
    // Optimistic, so the message does not appear to vanish; the server's own
    // chat frame replaces it when it arrives.
    (S.chats[id] = S.chats[id] || []).push({
      id: 'local-' + Date.now(), who: 'you', body: body,
      ts: new Date().toISOString(), state: 'sent'
    });
    box.value = '';
    drop(draftKey(S.docKey, role));
    renderInspector();
    hydrate();
  }

  function insertAtCursor(snippet, back) {
    var src = ibodyEl.querySelector('textarea.src[data-role="src"]');
    if (!src) return;
    var id = src.getAttribute('data-block');
    var out = spliceAt(src.value, src.selectionStart, snippet);
    src.value = out.text;
    var caret = out.caret - (back || 0);
    try { src.setSelectionRange(caret, caret); } catch (e) { /* ignore */ }
    src.focus();
    setDraft(id, src.value);
    rememberCaret(id, src);
    scheduleSave(id);
  }

  // ---------------------------------------------------------------- patching

  function applyBlockHtml(id, html) {
    var old = blockEl(id);
    var node = nodeFromHtml(html);
    if (!node) return;
    if (!node.hasAttribute('data-mx')) node.setAttribute('data-mx', id);
    if (old && old.parentNode) old.parentNode.replaceChild(node, old);
    else docInner.appendChild(node);
    hydrateSpans(node);
    if (anchorEl === old) anchorEl = node;
  }

  /* Behaviour 3. A patch for the block under the cursor waits for the blur.
     Otherwise the author's own save re-renders the paragraph they are typing
     into and takes the caret with it. */
  function onPatch(msg) {
    var ui = captureUI();
    var blocks = msg.blocks || {};
    var touched = {};

    Object.keys(blocks).forEach(function (raw) {
      var id = normId(raw);
      touched[id] = true;
      if (msg.blockdata && msg.blockdata[raw]) S.blocks[id] = msg.blockdata[raw];
      if (id === S.focusedBlock) {
        S.deferredPatches[id] = blocks[raw];
        var el = blockEl(id);
        if (el) el.classList.add('is-stale');
        return;
      }
      applyBlockHtml(id, blocks[raw]);
    });

    (msg.removed || []).forEach(function (r) {
      var id = normId(typeof r === 'string' ? r : (r && r.id));
      touched[id] = true;
      var el = blockEl(id);
      if (el && el.parentNode) el.parentNode.removeChild(el);
      delete S.blocks[id];
      // The draft is NOT dropped. A block disappearing from the file is not the
      // author discarding what they typed into it.
    });

    (msg.added || []).forEach(function (a) {
      if (!a || typeof a === 'string') return;   // nothing to insert without html
      var id = normId(a.id);
      touched[id] = true;
      if (a.block) S.blocks[id] = a.block;
      var node = nodeFromHtml(a.html || '');
      if (!node) return;
      node.setAttribute('data-mx', id);
      var after = a.after ? blockEl(normId(a.after)) : null;
      if (after && after.parentNode) after.parentNode.insertBefore(node, after.nextSibling);
      else docInner.appendChild(node);
    });

    hydrate();
    // Rebuild the panel only when the patch actually reached what is open, and
    // never while its block has focus: an untouched selection is unchanged, and
    // a touched-but-focused one is deferred until blur anyway.
    if (S.sel && touched[S.sel.blockId] && S.sel.blockId !== S.focusedBlock) renderInspector();
    restoreUI(ui);
    updateMeta();
  }

  function flushDeferred(id) {
    if (!Object.prototype.hasOwnProperty.call(S.deferredPatches, id)) return;
    var html = S.deferredPatches[id];
    delete S.deferredPatches[id];
    var ui = captureUI();
    applyBlockHtml(id, html);
    hydrate();
    renderInspector();
    restoreUI(ui);
  }

  // --------------------------------------------------------------- websocket

  function send(payload) {
    if (!S.sock || S.sock.readyState !== 1) return false;
    try { S.sock.send(JSON.stringify(payload)); return true; } catch (e) { return false; }
  }

  function setLive(state, note) {
    S.sockState = state;
    if (!liveEl) return;
    liveEl.classList.toggle('is-offline', state === 'offline');
    liveEl.classList.toggle('is-connecting', state === 'connecting');
    liveTextEl.textContent = note || (state === 'live' ? 'watching'
      : state === 'connecting' ? 'connecting…' : 'not connected');
  }

  function connect() {
    if (typeof WebSocket === 'undefined' || window.location.protocol === 'file:') {
      // Opened straight off disk. Everything still reads and every draft is
      // still kept; only the writing half is unavailable.
      setLive('offline', 'static page, nothing is being written');
      return;
    }
    var scheme = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
    var url = scheme + window.location.host + '/ws';
    var sock;
    try { sock = new WebSocket(url); } catch (e) { setLive('offline'); return; }
    S.sock = sock;
    setLive('connecting');

    sock.onopen = function () { S.retry = 0; setLive('live'); };
    sock.onclose = function () {
      S.sock = null;
      setLive('offline');
      S.retry = Math.min(S.retry + 1, 6);
      setTimeout(connect, 500 * Math.pow(2, S.retry - 1));
    };
    sock.onerror = function () { setLive('offline'); };
    sock.onmessage = function (e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      handle(msg);
    };
  }

  function handle(msg) {
    if (!msg || !msg.type) return;
    switch (msg.type) {
      case 'patch':
        onPatch(msg);
        break;
      case 'state': {
        var id = normId(msg.block);
        S.blockState[id] = msg.state;
        hydrate();
        if (S.sel && S.sel.blockId === id) renderInspector();
        break;
      }
      case 'chat': {
        var cid = normId(msg.block);
        (S.chats[cid] = S.chats[cid] || []).push(msg.message || {});
        hydrate();
        if (S.sel && S.sel.blockId === cid) renderInspector();
        break;
      }
      case 'saved': {
        var sid = normId(msg.block);
        var b = S.blocks[sid];
        var sent = S.sent[sid];
        if (b && sent !== undefined) b.source = sent;
        // Only clear the draft if nothing was typed after the send, or the
        // newer keystrokes would be thrown away by the confirmation.
        if (draftOf(sid) === sent) clearDraft(sid);
        else scheduleSave(sid);
        setSave(sid, { state: 'saved', at: msg.at || new Date().toISOString() });
        break;
      }
      case 'held': {
        var hid = normId(msg.block);
        setSave(hid, { state: 'held', reason: msg.reason || 'The server would not take it.' });
        break;
      }
      default:
        break;
    }
  }

  // ---------------------------------------------------------- skin, hue, meta

  function applySkin(name) {
    appEl.setAttribute('data-skin', name);
    var bs = document.querySelectorAll('.skinctl .tb[data-skin]');
    for (var i = 0; i < bs.length; i++) {
      bs[i].setAttribute('aria-pressed', bs[i].getAttribute('data-skin') === name ? 'true' : 'false');
    }
    store(MS_PREF_PREFIX + 'skin', name);
  }

  /* One hue, declared on the ROOT so both skins and the neutrals read it. The
     status colours are excluded from the palette on purpose: they mean
     something, so they must not rotate with the decoration. */
  function applyHue(hex) {
    var hs = hexToHS(hex);
    document.documentElement.style.setProperty('--h', String(hs.h));
    document.documentElement.style.setProperty('--sat', hs.s.toFixed(2));
    store(MS_PREF_PREFIX + 'hue', hex);
  }

  function updateMeta() {
    if (!metaEl) return;
    var dirty = Object.keys(S.drafts).length;
    var st = S.ms.stats || {};
    var bits = [];
    if (st.files) bits.push(st.files + ' files');
    if (st.cites) bits.push(st.cites + ' citations');
    bits.push(dirty ? dirty + ' unsaved' : 'no unsaved drafts');
    metaEl.textContent = bits.join(' · ');
  }

  // ------------------------------------------------------------------ events

  function openKey(key) {
    var parts = String(key).split(':');
    var kind = parts[0];
    var rest = parts.slice(1).join(':');
    if (kind === 'block') { select('block', normId(rest), normId(rest)); return; }
    if (kind === 'cite') { select('cite', rest, S.sel && S.sel.blockId); return; }
    if (kind === 'value') { select('value', rest, S.sel && S.sel.blockId); return; }
    select('panel', key, S.sel && S.sel.blockId);
  }

  function onClick(e) {
    var skin = e.target.closest('.skinctl .tb[data-skin]');
    if (skin) { applySkin(skin.getAttribute('data-skin')); return; }

    var goto_ = e.target.closest('[data-goto]');
    if (goto_) {
      e.preventDefault();
      var gid = normId(goto_.getAttribute('data-goto'));
      if (S.blocks[gid]) select('block', gid, gid, null, { keepDoc: false });
      // After the render, not before it, or the restore lands on top of us.
      requestAnimationFrame(function () {
        var gel = blockEl(gid);
        if (gel) gel.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' });
      });
      return;
    }

    var pin = e.target.closest('[data-goto-chat]');
    if (pin) {
      e.preventDefault(); e.stopPropagation();
      var pid = normId(pin.getAttribute('data-goto-chat'));
      select('block', pid, pid, 1);
      return;
    }

    var act = e.target.closest('[data-act]');
    if (act) {
      e.preventDefault(); e.stopPropagation();
      doAct(act.getAttribute('data-act'));
      return;
    }

    var opener = e.target.closest('[data-open]');
    if (opener) { e.preventDefault(); e.stopPropagation(); openKey(opener.getAttribute('data-open')); return; }

    var cite = e.target.closest('.cite[data-key]');
    if (cite) {
      e.stopPropagation();
      var host = cite.closest('[data-mx]');
      select('cite', cite.getAttribute('data-key'), host ? host.getAttribute('data-mx') : null);
      return;
    }

    var val = e.target.closest('.val[data-key]');
    if (val) {
      e.stopPropagation();
      var vhost = val.closest('[data-mx]');
      select('value', val.getAttribute('data-key'), vhost ? vhost.getAttribute('data-mx') : null);
      return;
    }

    var blk = e.target.closest('[data-mx]');
    if (blk) {
      var bid = blk.getAttribute('data-mx');
      select('block', bid, bid);
    }
  }

  function doAct(act) {
    var id = S.sel && S.sel.blockId;
    if (act === 'revert' && id) {
      var b = S.blocks[id];
      clearDraft(id);
      if (b) S.save[id] = { state: 'clean' };
      renderInspector();
      return;
    }
    if (act === 'discard' && id) { clearDraft(id); renderInspector(); return; }
    if (act === 'ins:footnote') { insertAtCursor('\\footnote{}', 1); return; }
    if (act.indexOf('send:') === 0) { sendComposer(act.slice(5)); return; }
    if (act === 'ask:rerun' && id) { select('block', id, id, 1); return; }
    if (act.indexOf('compile:') === 0) {
      // Compiling delegates to the tools that already exist, which live on the
      // agent side rather than in the server.
      if (id) { select('block', id, id, 1); }
      setLive(S.sockState, 'ask in a chat to compile ' + act.slice(8));
      return;
    }
  }

  function onKeydown(e) {
    var t = e.target;
    if ((e.key === 'Enter' || e.key === ' ') && t && t.matches &&
        t.matches('.blk, .exhibit, .cite, .val, [data-goto]')) {
      e.preventDefault();
      t.click();
    }
  }

  function reduceMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function markOutline() {
    if (!outlineEl || !docEl) return;
    var links = outlineEl.querySelectorAll('a[data-goto]');
    var top = docEl.getBoundingClientRect().top + 12;
    var on = null;
    for (var i = 0; i < links.length; i++) {
      var el = blockEl(normId(links[i].getAttribute('data-goto')));
      if (el && el.getBoundingClientRect().top <= top) on = links[i];
    }
    for (var j = 0; j < links.length; j++) links[j].classList.toggle('on', links[j] === on);
  }

  // -------------------------------------------------------------------- boot

  function boot() {
    appEl = document.getElementById('app');
    if (!appEl) return;
    railEl = document.getElementById('rail');
    docEl = document.getElementById('doc');
    inspEl = document.getElementById('insp');
    docInner = document.getElementById('doc-inner');
    ibodyEl = document.getElementById('ibody');
    tabsEl = document.getElementById('tabs');
    eyebrowEl = document.getElementById('eyebrow');
    ititleEl = document.getElementById('ititle');
    isubEl = document.getElementById('isub');
    jumpBtn = document.getElementById('jump');
    liveEl = document.getElementById('live');
    liveTextEl = document.getElementById('live-text');
    metaEl = document.getElementById('toolbar-meta');
    outlineEl = document.getElementById('outline');
    pathEl = document.getElementById('doc-path');
    hueWheel = document.getElementById('hue-wheel');

    S.ms = window.MS || {};
    S.blocks = S.ms.blocks || {};
    S.chats = S.ms.chats || {};
    S.docKey = String(S.ms.title || window.location.pathname || 'doc');

    restoreDrafts();
    hydrate();
    updateMeta();

    if (pathEl && !pathEl.textContent) {
      var first = S.order.length ? S.blocks[S.order[0]] : null;
      pathEl.textContent = first ? String(first.file || '') : '';
    }

    // The inspector is PINNED, not following. Scrolling the manuscript never
    // clears what is open; it only reveals the way back.
    if (window.IntersectionObserver && docEl) {
      io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.target === anchorEl && jumpBtn) jumpBtn.hidden = en.isIntersecting;
        });
      }, { root: docEl, threshold: 0 });
    }
    if (jumpBtn) {
      jumpBtn.addEventListener('click', function () {
        if (!anchorEl) return;
        anchorEl.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'center' });
      });
    }

    document.addEventListener('click', onClick);
    document.addEventListener('keydown', onKeydown);
    if (docEl) {
      var ticking = false;
      docEl.addEventListener('scroll', function () {
        if (ticking) return;
        ticking = true;
        window.requestAnimationFrame(function () { ticking = false; markOutline(); });
      }, { passive: true });
    }

    var savedSkin = load(MS_PREF_PREFIX + 'skin');
    applySkin(savedSkin === 'glass' ? 'glass' : 'instrument');
    var savedHue = load(MS_PREF_PREFIX + 'hue');
    if (savedHue && hueWheel) { hueWheel.value = savedHue; applyHue(savedHue); }
    if (hueWheel) hueWheel.addEventListener('input', function () { applyHue(hueWheel.value); });

    markOutline();
    connect();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  /* Driving the page is a message, not browser automation: both ends of the
     channel are ours. A host that is not a websocket (the standalone shell, or
     a check running against the page) delivers the same frames through here,
     so there is one code path for a patch however it arrived. */
  api.handle = handle;
  api.select = select;

  return api;
});
