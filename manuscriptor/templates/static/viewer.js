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
    // Empty stays empty: '' is the document chat's key, and prefixing it
    // would file document messages under a block named "b-".
    if (v === null || v === undefined || v === '') return '';
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

  /* The status colours are excluded from the hue rotation, but the wheel can
     still PICK the computed violet as the accent, and then every button, tab
     underline and link occupies the channel that means "this number came from
     code". Computed is the one status rendered as coloured inline content
     (the other three only ever mark underlines and dots), so its band alone
     is reserved: a pick inside it is steered to the nearer band edge, keeping
     the chosen saturation and lightness. */
  var COMPUTED_HUE = 257, COMPUTED_BAND = 24;
  function clampHue(h) {
    var d = h - COMPUTED_HUE;
    if (d > 180) d -= 360;
    if (d < -180) d += 360;
    if (Math.abs(d) >= COMPUTED_BAND) return h;
    var out = COMPUTED_HUE + (d >= 0 ? COMPUTED_BAND : -COMPUTED_BAND);
    return ((out % 360) + 360) % 360;
  }

  /* Chats read newest first, strictly by time (author's call, twice): the
     reply that just landed is the thing being waited for, and it tops the
     list even when it answers an older comment. Sorted by timestamp rather
     than reversed, because the server's list is ordered comment-by-comment
     with replies attached, which is not globally chronological. */
  function newestFirst(msgs) {
    return (msgs || []).slice().sort(function (a, b) {
      return String((b && b.ts) || '').localeCompare(String((a && a.ts) || ''));
    });
  }

  /* The live feed is newest-first too, but by REVERSING rather than sorting.
     Its timestamps are second-resolution and its one writer emits a dozen
     events inside a second on a busy turn; a sort is stable, so each such run
     would come back oldest-first inside an otherwise newest-first list. The
     file is written in the order things happened, so a reverse is exact.
     `newestFirst` is right for chats, which arrive minutes apart and out of
     order across two processes. */
  /* What to call a block. The server decides it in `source/blocks.label` -- its
     own words, meaning an exhibit's caption or a paragraph's opening clause,
     and only then the heading it sits under -- and the page must not decide it
     a second way, or the ticker and the inspector end up calling one paragraph
     two things. `parent_heading` is the fallback for a blob written before
     `label` existed, which a static export on disk can be, and it is the reason
     an old export names every paragraph of a section alike. */
  function blockName(b) {
    if (!b) return null;
    return b.label || b.caption || b.parent_heading || null;
  }

  function feedNewestFirst(entries) {
    return (entries || []).slice().reverse();
  }

  /* WHAT IT HAS DONE, which is a different question from what it is doing.
     The live feed above is a ring the drain's next restart overwrites; this
     reads the append-only ledger beside it, threaded by the request that
     caused the work. One row per comment: what was asked, where it landed,
     what the agent said while doing it, and how it ended.

     The server does the join and the ordering. This decides only how a row
     reads, and it is exported so that decision can be tested without a page. */
  var HISTORY_MARK = { done: '✓', orphaned: '✗', working: '·', review: '·', queued: '·' };

  function historyMark(state) {
    return HISTORY_MARK[String(state || '')] || '·';
  }

  /* The one-line summary of a row, as the author reads it collapsed. Named
     rather than inlined because the title attribute wants the same words and
     two spellings of one sentence is how the ticker came to say one thing on
     load and another after a frame. */
  function historyTitle(item) {
    if (!item) return '';
    var bits = [item.id];
    if (item.asked) bits.push('“' + item.asked + '”');
    if (item.section || item.where) bits.push(item.section || item.where);
    bits.push(historyMark(item.state) + ' ' + (item.state || ''));
    if (item.reply) bits.push('— ' + item.reply);
    return bits.join(' · ');
  }

  /* One collapsible row per work item, newest first (the server orders them).
     A `<details>` rather than a hand-rolled toggle: it opens with a click or
     with the keyboard, it is announced as a disclosure, and it survives the
     panel being swapped out under it because the open ones are read back and
     re-applied. `open` names the rows to open, so a redraw does not fold the
     row the author is reading.

     A line the session could not tell apart between several comments in flight
     is shown under EACH of them and marked as shared, which is what it is. See
     `supervisor.Attributor`: picking one would be inventing the link. */
  function historyHTML(items, open) {
    items = items || [];
    if (!items.length) {
      return '<p class="empty">Nothing worked yet. When the agent picks a comment up, ' +
        'what it read, what it concluded and what it changed are kept here — ' +
        'through a restart, and through a reload.</p>';
    }
    open = open || {};
    return items.map(function (it, i) {
      var shut = open[it.id] === false || (open[it.id] === undefined && i > 0);
      var lines = (it.lines || []).map(function (l) {
        var who = l.who === 'teammate' ? 'teammate' : 'agent';
        return '<div class="fe ' + esc(l.kind) + ' ' + who + (l.shared ? ' shared' : '') + '"' +
          (l.shared ? ' title="the session was carrying several comments at once, ' +
            'so this line could not be told apart between them"' : '') +
          '><b>' + who + '</b> <span>' + esc(l.text) + '</span>' +
          '<time>' + esc(ago(l.ts)) + '</time></div>';
      }).join('');
      if (!lines) {
        lines = '<div class="fe note agent"><b>agent</b><span>No lines were recorded for this one.</span>' +
          '<time></time></div>';
      }
      var reply = it.reply
        ? '<div class="fe text agent hireply"><b>reply</b> <span>' + esc(it.reply) +
          '</span><time></time></div>'
        : '';
      var place = it.section || it.where;
      var where = place
        ? (it.block
            ? '<button class="tkgo" type="button" data-act="hist:goto:' + esc(it.block) + '">' +
              esc(place) + '</button>'
            : '<span class="tklabel">' + esc(place) + '</span>')
        : '';
      return '<details class="hi is-' + esc(it.state) + '" data-hist="' + esc(it.id) + '"' +
        (shut ? '' : ' open') + '>' +
        '<summary title="' + esc(historyTitle(it)) + '">' +
        '<code>' + esc(it.id) + '</code>' +
        '<span class="hiasked">' + esc(it.asked || '') + '</span>' +
        where +
        '<span class="histate">' + esc(historyMark(it.state)) + ' ' + esc(it.state) + '</span>' +
        '<time>' + esc(ago(it.at)) + '</time>' +
        '</summary>' +
        '<div class="feed-live hilines">' + lines + reply + '</div>' +
        '</details>';
    }).join('');
  }

  function hslToHex(h, s, l) {
    h = ((h % 360) + 360) % 360;
    var c = (1 - Math.abs(2 * l - 1)) * s,
        x = c * (1 - Math.abs(((h / 60) % 2) - 1)),
        m = l - c / 2,
        rgb = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
            : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return '#' + rgb.map(function (v) {
      var b = Math.round((v + m) * 255);
      return (b < 16 ? '0' : '') + b.toString(16);
    }).join('');
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

  /* The standing agent state, for the title bar. "3 queued · 1 working", and
     "idle" when there is nothing. A status line, not a dashboard: the author
     glances at it, he does not read it. */
  var QUEUE_ORDER = ['queued', 'working'];

  function queueSummary(queue) {
    queue = queue || [];
    var counts = {}, seen = [];
    for (var i = 0; i < queue.length; i++) {
      var st = (queue[i] && queue[i].state) || 'queued';
      if (counts[st] === undefined) { counts[st] = 0; seen.push(st); }
      counts[st]++;
    }
    var order = QUEUE_ORDER.filter(function (s) { return counts[s]; })
      .concat(seen.filter(function (s) { return QUEUE_ORDER.indexOf(s) === -1; }));
    if (!order.length) return 'idle';
    return order.map(function (s) {
      return counts[s] + ' ' + (s === 'review' ? 'to review' : s);
    }).join(' · ');
  }

  /* One ticker line: what happened, to the WORK the author asked for.

     The ticker answers "what is going on right now", and the answer to that is
     the request, not its address. Naming the block instead -- which this did --
     makes two requests on one paragraph the same line printed twice, and says
     nothing about which of them is moving. The block name is still here, in the
     hover title, because "where" is the second question and not the first.

     `label` remains the one name for a block and the queue, the inspector title
     and the agent's work item all still use it. A hex id is never a name: the
     author did not choose it, and it changes the moment the paragraph is
     edited, so it would be the least stable thing on the page. */
  var TICKER_WORDS = {
    queued: 'queued', working: 'working', done: 'done', orphaned: 'orphaned',
    review: 'flagged for review'
  };

  function tickerText(e) {
    e = e || {};
    // An extension says a whole sentence; the agent loop supplies parts. Both
    // have to render, and the contract said `notify(text)` while this read
    // fields that a string does not have.
    if (e.text) return String(e.text);
    var what;
    if (e.kind === 'patch') {
      what = 'edited';
      if (e.n > 1) what += ', ' + e.n + ' blocks';
    } else {
      what = TICKER_WORDS[e.state] || String(e.state || '');
    }
    // A `patch` reports an edit landing, which nobody asked for in words, so
    // it names the place instead. A `state` answers a comment and ALWAYS has a
    // request behind it -- the server clips one onto every state frame and onto
    // every seeded entry -- so a state entry reaching the fallback means the
    // request was dropped in transit, which is what happened for months. The
    // fallback stays because a bare " · done" would tell the author nothing.
    var lead = e.asked ? '“' + e.asked + '”' : tickerWhere(e);
    return lead + ' · ' + what;
  }

  /* Where an entry is, for the entries that have no request to lead with and
     for the hover title of the ones that do. */
  function tickerWhere(e) {
    e = e || {};
    return e.section || e.where || 'the manuscript';
  }

  /* The hover title: the request in full is already clipped server-side, so
     what hover adds is the location. Both, when both exist. */
  function tickerTitle(e) {
    e = e || {};
    if (e.text) return String(e.text);
    if (!e.asked) return tickerWhere(e);
    return '“' + e.asked + '” · ' + tickerWhere(e);
  }

  /* An edit renames its block, so every queue entry has to travel with the
     rename or it addresses a paragraph the page no longer has. That is the same
     defect that froze the margin on `working`, one layer up. */
  function renameQueue(queue, renamed) {
    queue = queue || [];
    if (!renamed) return queue;
    var map = {};
    Object.keys(renamed).forEach(function (k) { map[normId(k)] = normId(renamed[k]); });
    return queue.map(function (e) {
      if (!e || !e.block) return e;
      var from = normId(e.block), to = map[from];
      if (!to || to === from) return e;
      var copy = {};
      Object.keys(e).forEach(function (k) { copy[k] = e[k]; });
      copy.block = to;
      return copy;
    });
  }

  /* What should happen to this draft? Separated from the sending so it can be
     tested, because one of its branches used to be a silent `return` and that
     silence cost the author an abstract.

     `block` is the block as the CURRENT build knows it, or undefined if the id
     names a block that no longer exists. Ids are content-derived, so an edit
     renames its own block: the id a save is keyed to stops existing the moment
     the previous save lands. Treating that as nothing-to-do drops the text and
     says nothing, which is the one outcome an editor may never produce. It is a
     held state now, with a reason, and the draft stays where it is. */
  function saveDecision(text, block, validity) {
    if (text === null || text === undefined) return { action: 'none' };
    if (!block) {
      return { action: 'held',
               reason: 'This paragraph was rewritten under this editor, so the '
                     + 'draft could not be matched to it. Reopen the paragraph '
                     + 'to edit the current text.' };
    }
    if (block.editable === false) return { action: 'none' };
    if (text === String(block.source || '')) return { action: 'clean' };
    if (validity && !validity.ok) return { action: 'held', reason: validity.reason };
    return { action: 'send' };
  }

  /* The underline for a citation span, from the keys it carries.
     The render splits a stack into one span per key, so this is normally asked
     about one key. When it cannot split (a narrative citation, or names the
     surnames say are out of order) the span keeps all its keys, and then the
     colour is the WEAKEST of them: taking the first key's status let a five-key
     stack read as verbatim while three of its sources had no support at all.
     Red for anything examined and unsupported, amber for paraphrase, green only
     when every examined key is verbatim, neutral when none was examined. */
  function citeStatusClass(keys, cites) {
    var worst = '';
    for (var i = 0; i < (keys || []).length; i++) {
      var rec = (cites || {})[keys[i]];
      var s = rec && rec.status;
      if (s === 'missing') return 'miss';
      if (s === 'paraphrase') worst = 'para';
      else if (s === 'verbatim' && worst !== 'para') worst = 'verbatim';
    }
    return worst;
  }

  /* Which of four situations a citation is in. "Red" was covering two of them
     and the panel described a third that was not happening.

       none        no record at all: the pass has not seen this key. Neutral.
       unreadable  seen, but no fulltext to read. A library problem: fetch the PDF.
       unsupported READ, and no passage supported the claim. A writing problem,
                   and the sentence belongs on a review list.
       supported   at least one passage came back.

     Told apart by the facts the pass records rather than by the colour, since
     unreadable and unsupported are both red and only one of them is about the
     manuscript. */
  function evidenceState(rec) {
    if (!rec || !rec.status) return 'none';
    if (rec.quotes && rec.quotes.length) return 'supported';
    return rec.fulltext ? 'unsupported' : 'unreadable';
  }

  /* How wide the inspector may be, given the window. Both ends matter: too
     narrow and the source editor stops being somewhere you can write LaTeX (the
     20rem floor the layout already used), too wide and the manuscript is a strip
     in its own editor. Kept pure so the arithmetic is tested rather than
     inspected: an off-by-one here is a panel the reader cannot recover from. */
  function clampSplit(want, windowWidth) {
    var min = 20 * 16;                                   // 20rem, the editor floor
    var max = Math.max(min, Math.round(windowWidth * 0.62));
    if (!(want > 0)) return null;                        // no opinion: use the default
    return Math.max(min, Math.min(max, Math.round(want)));
  }

  var api = {
    validateLatex: validateLatex,
    saveDecision: saveDecision,
    citeStatusClass: citeStatusClass,
    evidenceState: evidenceState,
    clampSplit: clampSplit,
    normId: normId,
    draftKey: draftKey,
    spliceAt: spliceAt,
    hexToHS: hexToHS,
    clampHue: clampHue,
    newestFirst: newestFirst,
    blockName: blockName,
    feedNewestFirst: feedNewestFirst,
    historyMark: historyMark,
    historyTitle: historyTitle,
    historyHTML: historyHTML,
    queueSummary: queueSummary,
    tickerText: tickerText,
    tickerTitle: tickerTitle,
    tickerKey: tickerKey,
    renameQueue: renameQueue,
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
    deferredPanels: {},     // blocks whose PANEL waits for a blur (never the render)
    drafts: {},             // behaviour 1: block id -> unsaved source
    restored: {},           // block id -> the draft came back from storage
    save: {},               // block id -> {state, reason, at}
    sent: {},               // block id -> the text last sent, to confirm against
    blockState: {},         // block id -> queued|working|done|locked
    queue: [],              // the agent's standing work, oldest first
    ticker: [],             // what actually happened, newest first
    histOpen: {},           // history row id -> open, so a redraw keeps his place
    tickerKey: '',          // so a refresh ages the entries without re-animating
    caret: null,            // {id, start, end, scrollTop}
    extView: null,          // an extension's panel, when one is open
    back: [],               // where a detour came from, newest last
    insert: null,           // an insert form open inline on the References tab
    sock: null,
    sockState: 'connecting',
    saveTimer: null,
    saveFor: null,          // the block the pending save is for, renames included
    retry: 0
  };

  var appEl, railEl, docEl, inspEl, docInner, ibodyEl, tabsEl,
      eyebrowEl, ititleEl, isubEl, jumpBtn, backBtn, headSaveEl, liveEl, liveTextEl, metaEl,
      outlineEl, pathEl, hueWheel, agentEl, tickerEl;

  /* The panel's three permanent children. Everything the inspector draws goes
     into `ipanelEl`, so the source editor can be a SIBLING of the rebuild
     rather than a casualty of it -- see `ensurePanel`. */
  var bannersEl, srchostEl, ipanelEl, srcInEl, inclEl;

  var anchorEl = null, io = null, warnedBareId = false;
  var railSync = false;   // one syncRailButton per frame while a window resizes

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
  /* The draft is gone; the NOTICE that one was restored is not. It reports what
     happened when this block was opened, and that stays true after the text it
     was holding reaches the file. Clearing it here made it a save-state banner
     in disguise: it sits above the editor, so it vanished from under the
     author's cursor the moment his first save landed and took the editor 33px
     up the panel with it. `retireRestored` drops it when he leaves the block,
     which is a moment the panel is rebuilt for anyway. */
  function clearDraft(id) {
    delete S.drafts[id];
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

  /* A reload must not eat work either, so the drafts come back before anything
     is rendered, from BOTH stores: this browser's localStorage, and the server's
     own draft file.

     Two stores because each covers what the other cannot. localStorage survives
     a reload but not a new origin, and until the port became stable every launch
     was a new origin. The server's file survives a relaunch, a crash, and a
     window closed in anger, but only holds what reached it, so a draft typed
     while the socket was down is in the browser alone. Where both have a block,
     the local copy wins: it is the one the author can still see on screen.

     A draft that now matches the file is dropped from either store: the change
     landed while they were away. */
  function restoreDrafts() {
    var served = (S.ms.drafts && typeof S.ms.drafts === 'object') ? S.ms.drafts : {};
    var ids = Object.keys(S.blocks);
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      var text = load(draftKey(S.docKey, id));
      var fromServer = false;
      if (text === null && Object.prototype.hasOwnProperty.call(served, id)) {
        text = String(served[id]);
        fromServer = true;
      }
      if (text === null) continue;
      if (text === String(S.blocks[id].source || '')) {
        drop(draftKey(S.docKey, id));
        if (fromServer) sendDraft(id, '');     // it saved; stop holding it
        continue;
      }
      S.drafts[id] = text;
      S.restored[id] = true;
      if (fromServer) store(draftKey(S.docKey, id), text);
    }
  }

  /* Park a draft on the server, which is the copy that survives this window.
     Silent when there is no socket: the local copy still holds it, and the
     reconnect flushes every draft it is carrying. */
  function sendDraft(id, text) {
    if (!id) return false;
    return send({ type: 'draft', block: id, source: text });
  }

  /* On reconnect every draft is re-offered as a save rather than merely parked.
     `trySave` parks it first and then decides, so this both writes the text to
     the server's store and replaces the stale "Not connected" line with what is
     actually true of each block: saved, or held and why. A draft that went stale
     while the socket was down is refused by the server's own stale-block guard
     rather than overwriting whatever landed in the meantime. */
  function flushDrafts() {
    var ids = Object.keys(S.drafts);
    for (var i = 0; i < ids.length; i++) trySave(ids[i]);
  }

  // -------------------------------------------------------------- hydration

  function nodeFromHtml(html) {
    var t = document.createElement('template');
    t.innerHTML = String(html === null || html === undefined ? '' : html).trim();
    return t.content.firstElementChild;
  }

  /* A block can render as SEVERAL elements. The front matter is a title, a
     byline, an abstract label, the abstract and a keywords line, with the anchor
     on the first of them, so a patch that swapped one element updated the title
     and left the abstract alone. */
  function nodesFromHtml(html) {
    var t = document.createElement('template');
    t.innerHTML = String(html === null || html === undefined ? '' : html).trim();
    return Array.prototype.slice.call(t.content.children);
  }

  /* The elements this block occupies: its anchor, plus the siblings after it that
     carry no anchor of their own. Symmetrical with the server's block_html, and
     for an ordinary paragraph it is a list of one. */
  function blockRun(el) {
    var run = [el], n = el.nextElementSibling;
    while (n && !n.hasAttribute('data-mx') && !n.querySelector('[data-mx]')) {
      run.push(n);
      n = n.nextElementSibling;
    }
    return run;
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

    /* A block whose LaTeX renders nothing (\newpage, a spacing run) keeps its
       anchor in the DOM but must not occupy a line or take a click: the
       2026-07-22 audit found them as invisible 11px clickable slivers.
       styles.css collapses `p[data-mx]:empty`, but the ¶ tag inserted below
       defeats :empty forever after; the class is the same rule made
       hydration-proof. A void block that carries a chat or a state is kept
       visible, because a pin pointing at nothing the author can find is worse
       than a sliver. */
    var st = S.blockState[id];
    var chat = (S.chats[id] || []).length;
    var isVoid = !isExhibit && !st && !chat &&
      el.textContent.trim() === '' &&
      !el.querySelector('img, table, math, figure');
    el.classList.toggle('is-void', isVoid);
    if (isVoid) {
      el.setAttribute('tabindex', '-1');
      markDirty(id, draftOf(id) !== null);
      return id;
    }

    /* Two stacked lines, not one run: the paragraph ordinal is what the author
       and the agent talk in, the source line is what an editor jumps to, and
       side by side they read as one number with a separator in it. */
    if (!isExhibit && !el.classList.contains('is-heading')) {
      var tag = document.createElement('span');
      tag.className = 'tag';
      var para = document.createElement('span');
      para.className = 'tag-para';
      para.textContent = '¶' + ordinal;
      tag.appendChild(para);
      if (b.line_start) {
        var ln = document.createElement('span');
        ln.className = 'tag-line';
        ln.textContent = String(b.line_start);
        tag.appendChild(ln);
      }
      el.insertBefore(tag, el.firstChild);
    }

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
      var cls = citeStatusClass(keys, S.ms.cites);
      if (cls) c.classList.add(cls);
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
    // A collapsed block is deliberate for layout commands and a defect for
    // anything else; say which ids were hidden so a vanished paragraph is
    // diagnosable from the console rather than genuinely invisible.
    var voids = docInner.querySelectorAll('.is-void');
    if (voids.length && window.console) {
      var ids = [];
      for (var v = 0; v < voids.length; v++) ids.push(voids[v].getAttribute('data-mx'));
      console.info('collapsed ' + ids.length + ' void block(s): ' + ids.join(' '));
    }
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
    rememberHistoryOpen();
    return {
      rail: railEl ? railEl.scrollTop : 0,
      doc: docEl ? docEl.scrollTop : 0,
      insp: ibodyEl ? ibodyEl.scrollTop : 0,
      field: field
    };
  }

  /* Which history rows are open, read off the DOM into state. Every path that
     rebuilds the panel goes through here or through `renderAgentHistory`, so a
     row the author opened cannot be folded by a redraw he did not ask for --
     which is what a `<details>` does by default the moment its markup is
     replaced. */
  function rememberHistoryOpen() {
    if (!ibodyEl) return;
    var rows = ibodyEl.querySelectorAll('details[data-hist]');
    for (var i = 0; i < rows.length; i++) {
      S.histOpen[rows[i].getAttribute('data-hist')] = rows[i].open;
    }
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

  // Opening a citation's evidence or a value's code from a paragraph is a
  // detour. Remember where it started, or the only way back is to find the
  // paragraph again in the manuscript.
  function selectDetour(kind, key, blockId) {
    if (S.sel && (S.sel.kind !== kind || S.sel.key !== key)) {
      S.back.push({ kind: S.sel.kind, key: S.sel.key, blockId: S.sel.blockId, tab: S.tab });
      if (S.back.length > 12) S.back.shift();
    }
    select(kind, key, blockId);
  }

  function openInsert(kind) {
    S.insert = kind;
    if (S.sel && S.sel.kind === 'block') { S.tab = 2; renderInspector(); }
  }

  function closeInsert() {
    S.insert = null;
    renderInspector();
  }

  function goBack() {
    var prev = S.back.pop();
    if (prev) select(prev.kind, prev.key, prev.blockId, prev.tab, { fromBack: true });
  }

  function backLabel() {
    var prev = S.back[S.back.length - 1];
    if (!prev) return '';
    if (prev.kind === 'block') {
      var b = S.blocks[prev.key];
      return blockName(b) || 'the paragraph';
    }
    return prev.key;
  }

  /* The "draft restored" notice belongs to one VISIT to a block, not to the
     draft: it reports what was waiting when the panel opened, which stays true
     after that text reaches the file. Retiring it when the author leaves is
     what keeps it out of the save's way -- it used to be dropped by
     `clearDraft`, which is to say it vanished from above the editor the moment
     his first save landed and took the box he was typing in 33px up the panel.
     Called only where the panel is being rebuilt anyway. */
  function retireRestored() {
    var was = S.sel && S.sel.blockId;
    if (was) delete S.restored[was];
  }

  function select(kind, key, blockId, tab, opts) {
    var els = document.querySelectorAll('.sel');
    for (var i = 0; i < els.length; i++) els[i].classList.remove('sel');

    if (kind === 'block' && !(opts && opts.fromBack)) S.back.length = 0;
    if (blockId !== (S.sel && S.sel.blockId)) retireRestored();
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
    else if (S.sel.kind === 'ext' && S.extView) view = S.extView.build(api.ext);
    else view = viewPanel(S.sel.key);
    if (!view) return;

    S.tab = Math.min(S.tab, view.tabs.length - 1);
    S.tabMemory[selKey()] = S.tab;

    if (backBtn) {
      var label = backLabel();
      backBtn.hidden = !label;
      backBtn.innerHTML = label ? '\u2190 <span>' + esc(label) + '</span>' : '';
    }

    eyebrowEl.innerHTML = esc(view.eyebrow) +
      (view.chip ? ' <span class="chip ' + esc(view.chip[0]) + '">' + esc(view.chip[1]) + '</span>' : '');
    ititleEl.textContent = view.title;
    isubEl.textContent = view.sub;

    // The save state belongs beside what is being saved, not at the far end of
    // a panel the paragraph may have scrolled past.
    if (headSaveEl) {
      headSaveEl.innerHTML =
        (S.sel.kind === 'block' && S.blocks[S.sel.key]) ? saveStateHtml(S.sel.key, S.blocks[S.sel.key]) : '';
    }

    tabsEl.innerHTML = '';
    view.tabs.forEach(function (t, i) {
      var b = document.createElement('button');
      b.type = 'button';
      b.innerHTML = esc(t.name) + (t.n ? ' <span class="count">' + esc(t.n) + '</span>' : '');
      if (i === S.tab) b.className = 'on';
      b.addEventListener('click', function () { S.tab = i; renderInspector(); });
      tabsEl.appendChild(b);
    });

    var tab = view.tabs[S.tab];
    ensurePanel();
    ipanelEl.innerHTML = tab.body;
    mountEditor(tab.editor || null);
    // Measured against the panel's siblings, so it has to run once more after
    // layout settles or a long block sizes itself against a half-built card.
    requestAnimationFrame(autosizeAll);
    renderBanners();

    wireInspector();
    restoreUI(ui, keepDoc);
  }

  /* The banners live in their own container so they can be refreshed without
     rebuilding the panel, which would take the editor and the caret with it.
     They sit ABOVE the editor in the panel's flow, so anything that appears or
     vanishes here moves the box the author is typing in -- which is why what
     is left here may only change when the SELECTION changes, never when the
     save state does.

     A second "Unsaved" bar used to live here and did exactly that. Reported
     2026-08-04: "the 'unsaved' popup when typing moves the text window which
     messes with inserts." It was 33px tall, it arrived on the first keystroke,
     `clearDraft` took it away again about a second later, and it came back on
     the next keystroke -- so the editor walked up and down the panel for the
     whole of a typing session. It was also a SECOND rendering of a fact the
     header's save line already carries permanently, so the repair was to
     delete it rather than to hold it still: one save badge, always present,
     out of the editor's flow, changing only its wording. */
  function renderBanners() {
    var box = ibodyEl.querySelector('[data-role="banners"]');
    if (!box) return;
    var id = S.sel && S.sel.blockId;
    var html = '';
    if (id && S.restored[id]) {
      html += '<div class="restored">Unsaved draft restored. It was kept when you clicked away.</div>';
    }
    box.innerHTML = html;
  }

  // ------------------------------------------------------- the source editor
  //
  // A <textarea> carries its own undo history, and that history belongs to the
  // ELEMENT. Destroy the element and Cmd+Z has nothing left to walk back. The
  // panel used to be drawn with one `ibodyEl.innerHTML = ...`, so every blur,
  // every tab switch, every reselection and every deferred patch built a brand
  // new textarea and threw the author's typing history away with the old one.
  // Undo therefore worked only inside one uninterrupted burst and looked
  // broken everywhere else.
  //
  // Measured in Chrome 151 on 2026-07-28, because the design turns on which of
  // these actually costs the history:
  //
  //   blur, then focus again ............ history KEPT
  //   the element is replaced ........... history LOST   <- the bug
  //   the element is MOVED to a new
  //     parent .......................... history LOST   <- so no slot-and-mount
  //   hidden with `hidden`, then shown .. history KEPT
  //   `.value` assigned from script ..... history TRUNCATED at the assignment
  //
  // So the editor is built once and never moved: the rebuild writes into
  // `ipanelEl`, which is its sibling. The last line is the reason an agent's
  // write is not undoable, which is the scope that was asked for.

  var INSBARS =
    '<div class="insbars">' +
    '<div class="insbar"><span>At cursor</span>' +
    '<button class="ins" type="button" data-open="insert:cite">Citation</button>' +
    '<button class="ins" type="button" data-open="insert:value">Number</button>' +
    '<button class="ins" type="button" data-act="ins:footnote">Footnote</button>' +
    '</div>' +
    '<div class="insbar"><span>After this ¶</span>' +
    '<button class="ins" type="button" data-open="insert:exhibit">Exhibit</button>' +
    '<button class="ins" type="button" data-open="insert:paragraph">New paragraph</button>' +
    '</div></div>';

  /* The panel's permanent shape, built once: the banners, the editor's card,
     and the container everything else is redrawn into. */
  function ensurePanel() {
    if (ipanelEl && ipanelEl.parentNode === ibodyEl) return;
    ibodyEl.innerHTML = '';

    bannersEl = document.createElement('div');
    bannersEl.setAttribute('data-role', 'banners');
    ibodyEl.appendChild(bannersEl);

    var holder = document.createElement('div');
    holder.innerHTML = card('', 'editable', INSBARS + '<div data-role="inclnote"></div>', true);
    srchostEl = holder.firstChild;
    srchostEl.setAttribute('data-role', 'srchost');
    srchostEl.hidden = true;
    srcInEl = srchostEl.querySelector('.in');
    inclEl = srchostEl.querySelector('[data-role="inclnote"]');
    ibodyEl.appendChild(srchostEl);

    ipanelEl = document.createElement('div');
    ipanelEl.setAttribute('data-role', 'ipanel');
    ibodyEl.appendChild(ipanelEl);
  }

  /* The editor for the block the source tab is showing, or none at all.
     `spec` is what `sourceTab` decided; a null one means the panel is showing
     something that is not an editable block's source. */
  function mountEditor(spec) {
    ensurePanel();
    if (!spec) { srchostEl.hidden = true; return; }

    var el = srcInEl.querySelector('textarea[data-role="src"]');
    var fresh = false;
    /* A different block is a different undo history, and Blink's undo stack is
       per FRAME rather than per element: with two editors in the document at
       once, a second Cmd+Z pressed in one of them walks back the OTHER one
       (measured, same session). One editor at a time, replaced outright when
       the block changes, is what keeps per-block meaning per-block -- the old
       element leaves the document and its steps go invalid with it. */
    if (!el || el.getAttribute('data-block') !== spec.id) {
      if (el && el.parentNode) el.parentNode.removeChild(el);
      el = makeEditor(spec.id);
      srcInEl.insertBefore(el, srcInEl.firstChild);
      fresh = true;
    }
    /* Only when it really moved. Assigning `.value` truncates the undo history
       at the assignment, so doing it on every render would undo the repair. */
    if (el.value !== spec.text) el.value = spec.text;
    inclEl.innerHTML = spec.includes || '';
    srchostEl.hidden = false;

    // Open showing the whole paragraph, not a porthole to drag open.
    autosize(el);
    if (fresh && S.caret && S.caret.id === spec.id) {
      try { el.setSelectionRange(S.caret.start, S.caret.end); } catch (e) { /* ignore */ }
      el.scrollTop = S.caret.scrollTop;
    }
  }

  function makeEditor(id) {
    var src = document.createElement('textarea');
    src.className = 'src';
    src.spellcheck = false;
    src.setAttribute('data-role', 'src');
    src.setAttribute('data-block', id);

    /* Read the id off the element on every use, never capture it.
       Ids are content-derived, so the FIRST save renames this very block, and
       the element outlives every rename, so a captured id goes stale exactly
       during a burst of continuous typing. Every keystroke after that
       addressed a block the build no longer had: the drafts were written under
       a dead key and the saves silently did nothing. `applyRenames` keeps
       `data-block` current, so asking the element is asking the live build. */
    var cur = function () { return src.getAttribute('data-block'); };

    src.addEventListener('input', function () {
      /* Undo is frame-wide. Pressing it in the chat composer fires a
         `historyUndo` input event on THIS box even while it is hidden behind
         another tab (measured, Chrome 151). Only what the author does in the
         box he is actually in may reach the manuscript. */
      if (document.activeElement !== src) return;
      var live = cur();
      autosize(src);
      setDraft(live, src.value);
      rememberCaret(live, src);
      S.save[live] = { state: 'typing' };
      renderBanners();
      renderSaveBox(live);   // or the line still claims there is nothing unsaved
      scheduleSave(live);
    });
    src.addEventListener('keyup', function () { rememberCaret(cur(), src); });
    src.addEventListener('click', function () { rememberCaret(cur(), src); });
    src.addEventListener('focus', function () { S.focusedBlock = cur(); });
    src.addEventListener('blur', function () {
      var live = cur();
      S.focusedBlock = null;
      rememberCaret(live, src);
      if (S.saveTimer) { clearTimeout(S.saveTimer); S.saveTimer = null; }
      trySave(live);
      flushDeferred(live);
    });
    return src;
  }

  /* The editor, only when it is the one on screen. A hidden editor is still in
     the document -- that is what keeps its history -- so anything that acts on
     "the editor" has to ask whether it is showing. */
  function activeEditor() {
    if (!srchostEl || srchostEl.hidden) return null;
    return srcInEl.querySelector('textarea[data-role="src"]');
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
    var source = sourceTab(id, b);
    source.name = 'Source';
    return {
      eyebrow: b.kind === 'paragraph' ? 'Paragraph' : (b.kind || 'Block'),
      chip: b.editable === false ? ['c', 'generated'] : null,
      title: titleFor(b),
      sub: fileLine(b) + (values.length ? ' · ' + values.length + ' computed value' + (values.length === 1 ? '' : 's') + ' inside' : ''),
      tabs: [
        source,
        { name: 'Chat', n: chat.length || 0, body: chatTab(id, chat) },
        { name: 'References', n: values.length + cites.length || 0, body: refsTab(id, b) }
      ].concat(EXT.map(function (e) {
        return e.tab ? e.tab(id, b, api.ext) : null;
      }).filter(Boolean))
    };
  }

  // The section, and only the section. The paragraph's own first line used to
  // be appended here, which repeated back to the reader the words already open
  // in the editor below it.
  function titleFor(b) {
    return blockName(b) || 'Manuscript';
  }

  /* What the Source tab shows: markup to be redrawn, and -- when the block is
     one the author may edit -- the editor it should be showing. The editor is
     described rather than emitted, because emitting it would mean building a
     new textarea on every render, which is the bug this all exists to fix. */
  function sourceTab(id, b) {
    var state = S.blockState[id];

    if (state === 'locked') {
      return { body: card('', 'locked while working',
        '<div class="locked"><span>◔</span><div><b>Locked.</b> This block is being edited right now. ' +
        'It unlocks when the change lands, and you will see the diff.</div></div>') };
    }

    /* Never hand-edit a generated block. Editing it would hardcode a result
       into the manuscript, which is the one thing this tool exists to stop. */
    if (b.editable === false) {
      /* `values` is derived from the block's \input directives, so the block
         that IS the included file has none -- and the fallback then read "the
         script that writes <this same file>", a tautology printed exactly where
         the author needed somewhere to go. The block's own `producer` is in the
         payload beside them and is the more direct answer, so it leads. */
      var producers = (b.values || []).map(function (v) { return v.producer; }).filter(Boolean);
      if (b.producer) producers.unshift(b.producer);
      var who = producers.length
        ? producers.filter(function (p, i) { return producers.indexOf(p) === i; }).join(', ')
        : 'the script that writes ' + (b.file || 'this file');
      return { body: card('This block is generated', fileLine(b),
        '<div class="locked"><span>⊘</span><div><b>Not editable here.</b> This file is written by <b>' +
        esc(who) + '</b>. Editing it would hardcode a result into the manuscript. ' +
        'Change the script and it will rebuild.</div></div>' +
        '<div class="row" style="margin-top:.6rem"><button class="btn" data-act="ask:rerun">Ask for a re-run</button></div>') +
        card('Read-only source', '', '<textarea class="src" readonly data-role="ro">' + esc(b.source || '') + '</textarea>', true) };
    }

    var includes = b.includes || [];
    var incl = includes.length
      ? '<p class="meta" style="padding:.5rem .7rem .6rem">You are holding the unflattened source, so ' +
        includes.map(function (i) { return '<span class="inputtok">' + esc(i.directive) + '</span>'; }).join(' ') +
        ' survive' + (includes.length === 1 ? 's' : '') + ' your edit. The values shown in the page are read from those files.</p>'
      : '';

    return { body: '', editor: { id: id, text: sourceOf(id), includes: incl } };
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

  /* The editor on screen, which is the live one when a block is open and the
     read-only copy when a generated block is. The hidden editor is skipped:
     it has no layout to measure against, and sizing it would only write a
     height it will not be wearing when it comes back. */
  function autosizeAll() {
    var el = activeEditor();
    if (!el && ipanelEl) el = ipanelEl.querySelector('textarea.src');
    if (el) autosize(el);
  }

  function saveStateHtml(id, b) {
    var s = S.save[id] || {};
    var cls = '', text;
    /* A dead socket outranks every other reason a block is unsaved. Held text
       and offline text are both unwritten, but only one of them is unwritten
       because nothing can be written at all, and that is the fact the author
       needs: measured live, an unbalanced draft typed while the server was down
       reported only the brace and said nothing about the connection. */
    if (S.sockState !== 'live' && draftOf(id) !== null) {
      return saveLineHtml(' offline',
        '<b>Not connected.</b> Your draft is held in this window and is '
        + 'written to disk the moment the server is back.') + saveButtons(id);
    }
    if (s.state === 'held') {
      cls = ' held';
      text = '<b>Held.</b> ' + esc(s.reason) + ' Nothing is written while it would not parse.';
    } else if (s.state === 'pending') {
      cls = ' pending';
      text = '<b>Saving…</b> writes on a pause, not a button.';
    } else if (s.state === 'offline') {
      /* What this line may claim is exactly what is true. It used to say the
         draft was "on disk", and on 2026-07-26 that sent an author looking for
         a file that did not exist: the only copy was in this window. It is
         written to disk on reconnect, and the reconnect is automatic. */
      cls = ' offline';
      text = '<b>Not connected.</b> Your draft is held in this window and is '
           + 'written to disk the moment the server is back.';
    } else if (s.state === 'saved') {
      text = '<b>Saved</b> to ' + esc(b.file || 'the file') + ', ' + esc(ago(s.at)) + '. Writes on a pause, not a button.';
    } else if (draftOf(id) !== null) {
      text = '<b>Unsaved.</b> It will write about a second after you stop typing.';
    } else {
      text = '<b>No unsaved changes</b> in this block.';
    }
    return saveLineHtml(cls, text) + saveButtons(id);
  }

  /* One line, whatever it says. The badge is permanent and the editor sits
     below it, so a wording long enough to wrap would move the box being typed
     in; the stylesheet holds it to a single line and elides the overflow, and
     the whole sentence goes on the `title` so nothing it can say is out of
     reach. `text` is already escaped, so stripping its markup leaves an
     attribute value that is escaped too. */
  function saveLineHtml(cls, text) {
    var plain = text.replace(/<[^>]*>/g, '');
    return '<div class="savestate' + cls + '" title="' + plain + '">' +
      '<i></i><span>' + text + '</span></div>';
  }

  /* "Revert to last good" and "Discard draft" belong to every state of the save
     line, including the offline one: a self-saving file still needs a way back. */
  function saveButtons(id) {
    var off = draftOf(id) === null ? 'disabled' : '';
    return '<div class="row" style="padding:0 .7rem .6rem">' +
      '<button class="btn" data-act="revert" ' + off + '>Revert to last good</button>' +
      '<button class="btn" data-act="discard" ' + off + '>Discard draft</button></div>';
  }

  function saveBox(id, b) {
    return '<div data-role="savebox">' + saveStateHtml(id, b) + '</div>';
  }

  /* Refresh the save line in place, and say whether that was possible. The
     caller uses the answer to decide whether it must rebuild the whole panel,
     which is the thing it may not do while the editor has focus. */
  function renderSaveBox(id) {
    if (headSaveEl && S.sel && S.sel.kind === 'block' && S.sel.key === id) {
      headSaveEl.innerHTML = saveStateHtml(id, S.blocks[id]);
      return true;
    }
    return false;
  }

  function chatMsgs(msgs) {
    return '<div class="chat">' + newestFirst(msgs).map(function (m) {
      // A review finding carries its triage: dismissing closes it, asking
      // for the fix files an ordinary comment the drain will work.
      var triage = m.state === 'review'
        ? '<div class="row" style="margin-top:.4rem">' +
          '<button class="btn" data-act="finding:fix:' + esc(m.id) + '">Ask to fix</button>' +
          '<button class="btn" data-act="finding:dismiss:' + esc(m.id) + '">Dismiss</button></div>'
        : '';
      return '<div class="msg' + (m.who === 'you' ? ' bb' : '') + '">' +
        '<div class="who">' + esc(m.who) + (m.ts ? ' · ' + esc(ago(m.ts)) : '') +
        (m.state ? ' · ' + esc(m.state) : '') + '</div>' + esc(m.body) + triage + '</div>';
    }).join('') + '</div>';
  }

  function findingById(fid) {
    var blocks = Object.keys(S.chats);
    for (var i = 0; i < blocks.length; i++) {
      var msgs = S.chats[blocks[i]] || [];
      for (var j = 0; j < msgs.length; j++) {
        if (msgs[j] && msgs[j].id === fid) return { block: blocks[i], msg: msgs[j] };
      }
    }
    return null;
  }

  function chatTab(id, chat) {
    // The composer goes at the TOP: the thing you came here to do is type.
    var key = 'chat:' + id;
    var body = card('Ask for a change here', '⌘↵',
      composer(key, 'Cut the second sentence and fold the persistence claim into the first.',
        load(draftKey(S.docKey, key)) || ''));
    if (chat.length) {
      body += card('Earlier', chat.length + ' message' + (chat.length === 1 ? '' : 's'),
        chatMsgs(chat));
    } else {
      body += card('', '', '<p class="empty">No chat on this block yet. A note here becomes a comment ' +
        'anchored to these bytes, not to a page number.</p>');
    }
    return body;
  }

  /* The document chat: the inspector's resting state is a conversation about
     the manuscript, not a blank panel. A note typed here is a comment with no
     block; the drain presents it as document-level work for the session to
     decompose, and the agent's replies land back in this panel. Still nothing
     but the log between the two sides. */
  /* WHAT THE AGENT IS DOING, in the panel the author reads when nothing is
     selected. Until now "working" was a word in the header and nothing else: a
     comment sat marked working for fourteen minutes with a dispatched teammate
     that had gone silent, and there was no surface anywhere that could have said
     so. The drain streams its session's events into a file; this renders them.

     Newest FIRST, like everything else in the inspector. It read newest-last
     for a while, on the theory that it is a conversation; it is not, it is a
     status. The line that answers "what is it doing now" was the one at the
     foot of a 22rem scroller, so the author had to scroll to learn the thing
     the panel exists to say. A teammate's line is marked, since "which of them
     is stuck" is the first question. */
  function agentFeed() {
    var f = S.ms.agent_feed || {};
    var entries = (f.entries || []);
    var state = String(f.state || 'idle');
    var inner;
    if (!entries.length && state === 'idle') {
      inner = card('The agent', 'idle',
        '<p class="empty">Nothing queued. A note above becomes a comment, and what ' +
        'the agent does about it appears here as it happens.</p>');
    } else {
      var working = (f.working || []);
      var head = state + (working.length ? ' · ' + working.join(', ') : '');
      var rows = feedNewestFirst(entries).map(function (e) {
        var who = e.who === 'teammate' ? 'teammate' : 'agent';
        return '<div class="fe ' + esc(e.kind) + ' ' + who + '">' +
          '<b>' + who + '</b> <span>' + esc(e.text) + '</span>' +
          '<time>' + esc(ago(e.ts)) + '</time></div>';
      }).join('');
      var jump = working.length
        ? '<div class="row"><button class="btn" type="button" data-act="feed:goto">' +
          'Show the paragraph it is working on</button></div>'
        : '';
      inner = card('The agent', head, '<div class="feed-live">' + rows + '</div>' + jump);
    }
    // The slot is what lets `renderAgentFeed` swap this card by itself.
    return '<div data-role="agentfeed">' + inner + '</div>';
  }

  /* The feed had two renderers -- the title-bar line and this card -- and only
     the title bar was wired to the update. `setAgent` stored every frame and
     re-drew the line, while the card kept whatever `renderHome` painted at
     boot: the empty state, from the moment before any frame arrived. Observed
     2026-07-27 in dsp-bias, where the card read "Nothing queued" beside a feed
     holding 62 entries and a title bar reading "2 working · 7 to review".

     The card is swapped on its own, not through `renderHome`, because the
     composer sits directly above it and a rebuild every frame would take the
     author's focus, caret, and draft with it. Clicks survive the swap without
     re-wiring: `[data-act]` is delegated from the document. */
  function renderAgentFeed() {
    if (S.sel || !ibodyEl) return;
    var slot = ibodyEl.querySelector('[data-role="agentfeed"]');
    if (!slot) return;
    var next = agentFeed();
    if (slot.outerHTML === next) return;
    // The scroller is the author's place in the log, so it is put back.
    var live = slot.querySelector('.feed-live');
    var at = live ? live.scrollTop : 0;
    slot.outerHTML = next;
    var back = ibodyEl.querySelector('[data-role="agentfeed"] .feed-live');
    if (back) back.scrollTop = at;
  }

  /* THE HISTORY, under the live feed. The two answer different questions and
     the card above answers only the first: what it is doing NOW, from a ring
     that the drain's next restart -- one every twenty wakes or so -- rewrites
     from empty. This is what it has done, kept in the append-only ledger and
     threaded by the request that caused it, so it survives that restart, a
     server restart, and a reload. */
  function agentHistory() {
    var items = S.ms.history || [];
    var head = items.length
      ? items.length + ' item' + (items.length === 1 ? '' : 's')
      : '';
    return '<div data-role="agenthistory">' +
      card('What it has done', head, historyHTML(items, S.histOpen)) +
      '</div>';
  }

  /* Swapped on its own for the same reason the feed card is: the composer sits
     above both, and rebuilding the panel on every frame would take the
     author's caret and draft with it. Which rows are open is the author's
     place in this card, the way scrollTop is in the feed's, so it is read off
     the DOM before the swap and written back after -- a row cannot fold itself
     because a line arrived in another one. */
  function renderAgentHistory() {
    if (S.sel || !ibodyEl) return;
    var slot = ibodyEl.querySelector('[data-role="agenthistory"]');
    if (!slot) return;
    rememberHistoryOpen();
    var next = agentHistory();
    if (slot.outerHTML === next) return;
    slot.outerHTML = next;
  }

  function renderHome() {
    if (S.sel) return;
    var ui = captureUI();
    if (backBtn) backBtn.hidden = true;
    if (jumpBtn) jumpBtn.hidden = true;
    eyebrowEl.textContent = 'Manuscript';
    ititleEl.textContent = String(S.ms.title || 'Manuscript');
    isubEl.textContent = String(S.ms.main || '');
    if (headSaveEl) headSaveEl.innerHTML = '';
    tabsEl.innerHTML = '';
    var msgs = S.chats[''] || [];
    var body = card('Ask for a change anywhere', '⌘↵',
      composer('chat:', 'Check the tenses across the results section, or ask a question about the paper.',
        load(draftKey(S.docKey, 'chat:')) || ''));
    body += card('', '', '<p class="empty">Click a paragraph to see its source and its chat, ' +
      'a citation to see its evidence, or a violet number to see the code that produced it. ' +
      'A note typed above becomes a comment on the whole manuscript, worked by the same queue.</p>');
    body += agentFeed();
    body += agentHistory();
    if (msgs.length) {
      body += card('Earlier', msgs.length + ' message' + (msgs.length === 1 ? '' : 's'),
        chatMsgs(msgs));
    }
    ensurePanel();
    ipanelEl.innerHTML = body;
    mountEditor(null);
    wireInspector();
    restoreUI(ui, true);
  }

  function renderTodos(todos) {
    var box = document.getElementById('todos');
    if (!box) return;
    S.ms.todos = todos;
    box.innerHTML = todos.map(function (t) {
      return '<label><input type="checkbox" data-todo="' + esc(t.id) + '"' +
        (t.done ? ' checked' : '') + '> ' +
        (t.done ? '<s>' + esc(t.text) + '</s>' : esc(t.text)) + '</label>';
    }).join('');
    var n = document.getElementById('todo-count');
    if (n) {
      n.textContent = todos.filter(function (t) { return t.done; }).length +
        ' of ' + todos.length;
    }
  }

  function runEvidence() {
    var btn = document.getElementById('evidence-run');
    if (btn && btn.disabled) return;
    fetch('/evidence', { method: 'POST' })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (out) {
        if (out && out.error) { pushTicker({ text: out.error, when: new Date().toISOString() }); return; }
        if (btn) { btn.disabled = true; btn.textContent = 'Evidence running…'; }
      })
      .catch(function () {
        pushTicker({ text: 'could not start the evidence pass', when: new Date().toISOString() });
      });
  }

  /* The one click that leads to a write of the Zotero library, so it is its
     own button and never part of a run. The server chains a read-only
     evidence re-run afterwards, which is what upgrades the underlines. */
  function runRepair() {
    var btn = document.getElementById('repair-run');
    if (btn && btn.disabled) return;
    fetch('/repair', { method: 'POST' })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (out) {
        if (out && out.error) { pushTicker({ text: out.error, when: new Date().toISOString() }); return; }
        if (btn) { btn.disabled = true; btn.textContent = 'Fetching PDFs…'; }
      })
      .catch(function () {
        pushTicker({ text: 'could not start the repair', when: new Date().toISOString() });
      });
  }

  /* The skill menus: picking an entry sends one document-level comment
     naming the skill, then snaps back to the label. The queue counts it,
     the drain routes it, and for a check the findings come home as review
     comments pinned to their paragraphs. */
  var SKILL_ASKS = {
    'consistency-check': 'Run the preflight (consistency-check) on this document and land each finding as a review comment.',
    'review-manuscript': 'Run the full manuscript review on this document and land each finding as a review comment.',
    'revision-audit': 'Run the revision audit across the documents in this directory and land each finding as a review comment on the document it concerns.',
    'validate-bib': 'Validate the bibliography against the citations in this document and land each problem as a review comment.',
    'declaude': 'Run the declaude rewrite on this document, decomposed per paragraph; every write is one block.',
    'write': 'Draft the section I describe in the document chat. Ask me there if the brief is not yet specific enough to write.',
    'talk': 'Build the seminar deck from this document with the talk skill, and reply with where it landed.',
    'docx-package': 'Build the Word submission package with the docx-package skill, and reply with where it landed.',
    'em-submission': 'Build the Elsevier submission zip with the em-submission skill, and reply with where it landed.'
  };

  function wireSkillMenu(id) {
    var menu = document.getElementById(id);
    if (!menu) return;
    menu.addEventListener('change', function () {
      var skill = menu.value;
      menu.selectedIndex = 0;
      if (!skill || !SKILL_ASKS[skill]) return;
      if (!send({ type: 'chat', block: '', body: SKILL_ASKS[skill], check: skill })) return;
      pushTicker({ text: 'asked for ' + skill, when: new Date().toISOString() });
    });
  }

  function showRepair(missing) {
    var btn = document.getElementById('repair-run');
    if (!btn) return;
    var n = Number(missing) || 0;
    btn.hidden = n === 0;
    btn.disabled = false;
    btn.textContent = 'Fetch ' + n + ' missing PDF' + (n === 1 ? '' : 's') + '…';
  }

  // Inserting used to replace the whole panel, which meant leaving the
  // paragraph to add something to it. The form belongs beside the inventory it
  // adds to, so it opens inline on this tab and closes back to it.
  function insertInline(kind) {
    var forms = {
      cite: ['Insert a citation',
        'Searches your library first, then Crossref and OpenAlex. Nothing is inserted on a key that fails the identity gate: DOI, Crossref and OpenAlex agreeing, and a Zotero record with indexed fulltext.',
        'persistence of provider behaviour after subsidy withdrawal',
        'Writes three places: the \\citep at your cursor, an entry in references.bib, and a Zotero record if it is new.'],
      value: ['Insert a number',
        'There is no field for a literal. A value enters the manuscript only as an \\input of a file some script wrote.',
        'the control-group mean of statin prescription in the year before enrolment',
        'Writes three places: a new fragment file, a write_frag line in the producing script, and the \\input at your cursor.'],
      exhibit: ['Insert an exhibit',
        'A new block after this paragraph, not an edit to it. Where the float finally prints is LaTeX\u2019s decision.',
        'Table 2 again but split by patient sex, with the interaction p-value in a note',
        'Writes four places, and the fourth matters most: the runfile line, without which the exhibit goes stale on the next rebuild.'],
      footnote: ['Insert a footnote',
        'Spliced at your cursor. It touches no other file, so it lands immediately.',
        'the text of the note',
        'Writes one place: \\footnote{} at your cursor in this block.']
    };
    var f = forms[kind];
    if (!f) return '';
    return card(f[0], 'inline',
      '<p class="meta">' + esc(f[1]) + '</p>' +
      '<div class="composer" style="margin-top:.55rem">' +
      '<textarea data-role="insert" placeholder="' + esc(f[2]) + '"></textarea>' +
      '<div class="row"><button class="btn pri" type="button" data-act="ins:go">Show me what it will write</button>' +
      '<button class="btn" type="button" data-act="ins:close">Cancel</button></div></div>' +
      '<p class="meta" style="margin-top:.5rem">' + esc(f[3]) + '</p>');
  }

  function refsTab(id, b) {
    var values = b.values || [];
    var cites = b.cites || [];
    var out = S.insert ? insertInline(S.insert) : '';

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

    var notes = b.footnotes || [];
    out += card('Footnotes', String(notes.length),
      notes.length
        ? notes.map(function (n, i) {
            return '<div class="hit" style="cursor:default">' +
              '<b>note ' + (i + 1) + '</b><span>' + esc(n.length > 260 ? n.slice(0, 260) + '…' : n) + '</span></div>';
          }).join('')
        : '<p class="meta">None in this block.</p>');

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
    var state = evidenceState(rec);
    if (state === 'supported') {
      evidence = card('Supporting passages', rec.source || '',
        rec.quotes.map(function (q) {
          return '<p class="quote' + (q.status === 'verbatim' ? '' : ' p') + '">' + esc(q.text) + '</p>';
        }).join('') +
        (rec.reasoning ? '<p class="meta">' + esc(rec.reasoning) + '</p>' : ''));
    } else if (state === 'unreadable') {
      // The pass finished and had nothing to read. This is the case that used to
      // report itself as "no evidence loaded", which sent the author looking for
      // a pass that had already run.
      evidence = card('No fulltext to read', 'library',
        '<p class="meta">The last pass reached <b>' + esc(key) + '</b> and found no ' +
        'fulltext for it, so no passage could be checked either way. This is a ' +
        'library gap rather than anything about the sentence: attach a PDF in ' +
        'Zotero, or use <b>Fetch missing PDFs</b> in the toolbar, which is the one ' +
        'action that writes to your library, and then run the pass again.</p>');
    } else if (state === 'unsupported') {
      evidence = card('Read, and nothing supported it', 'review',
        '<p class="meta">The pass read ' +
        (rec.fulltext_chars ? esc(String(rec.fulltext_chars)) + ' characters of ' : '') +
        '<b>' + esc(key) + '</b> and came back with no passage supporting the claim ' +
        'this sentence makes. That is the check doing its job: either the sentence ' +
        'needs a source that says it, or it needs to claim less.</p>');
    } else {
      evidence = card('Not checked yet', '',
        '<p class="meta">No pass has looked at <b>' + esc(key) + '</b>, so its ' +
        'underline claims nothing. <b>Run evidence</b> in the toolbar checks every ' +
        'pair in the manuscript.</p>');
    }

    /* Add a source to THIS citation, with the citation itself as the target.
       The alternative is the caret dance: click into the source editor, trust
       that the offset was recorded from a focused textarea, then find the form on
       another tab. A citation you clicked cannot be measured wrong. */
    var beside = card('Add a source here', '',
      '<p class="meta">Puts another key into this citation, with no cursor to '
      + 'place. A source the paper already cites needs no lookup.</p>'
      + '<div class="row"><button class="btn pri" type="button" '
      + 'data-open="insert:cite:beside:' + esc(key) + '">Add a source beside '
      + esc(key) + '</button></div>');

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
        { name: 'Evidence', n: (rec.quotes || []).length || 0, body: evidence + beside },
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
      eyebrow: key.split(':')[0] === 'import' ? 'Import comments' : 'Insert',
      chip: d.chip,
      title: d.title,
      sub: b ? 'at the cursor · ' + fileLine(b) : '',
      tabs: [{ name: 'What happens', body: writes + gate + ask }]
    };
  }

  // ------------------------------------------------------- editing and saving

  /* Only what the rebuild rebuilt. The source editor is wired once, in
     `makeEditor`, because it is built once: re-wiring it here would mean it had
     been replaced, and a replaced editor is an editor with no undo history. */
  function wireInspector() {
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

  /* The pending save names its block in state rather than in the closure, for
     the same reason the editor does: a rename landing inside the debounce window
     would otherwise fire the save against an id that no longer exists. */
  function scheduleSave(id) {
    if (S.saveTimer) clearTimeout(S.saveTimer);
    S.saveFor = id;
    S.saveTimer = setTimeout(function () {
      S.saveTimer = null;
      trySave(S.saveFor);
    }, SAVE_DEBOUNCE_MS);
  }

  /* Behaviour 2. No button: a pause of about a second, or a blur, and only if
     what would land is plausibly balanced. When it is not, the draft is HELD
     and the panel says why, rather than nothing happening for reasons the
     author has to guess. */
  function trySave(id) {
    var text = draftOf(id);
    /* Park it before deciding. Everything below this line can decline to write
       the manuscript (invalid LaTeX, a block the build no longer has, no
       socket), and every one of those is a case where the text must still
       outlive the window. */
    if (text !== null) sendDraft(id, text);
    var d = saveDecision(text, S.blocks[id], text === null ? null : validateLatex(text));
    if (d.action === 'none') return;
    if (d.action === 'clean') { clearDraft(id); setSave(id, { state: 'clean' }); return; }
    if (d.action === 'held') { setSave(id, { state: 'held', reason: d.reason }); return; }

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
      /* This used to look for the save line inside the panel body, where it has
         never been: it lives in the header. The test therefore failed every time
         and the panel was rebuilt on EVERY save, replacing the textarea under
         the author's cursor about once a second while they typed. It survived
         only because the caret is restored afterwards, which is not the same as
         not having been taken. Ask the thing that would do the refreshing
         whether it could. */
      if (S.focusedBlock === id && renderSaveBox(id)) {
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
    // No selection means the document chat: a comment about the manuscript,
    // sent with no block. The server records it block-less and the drain
    // presents it as document-level work.
    var id = S.sel ? S.sel.blockId : '';
    if (id === null || id === undefined) return;
    if (!send({ type: 'chat', block: id, body: body })) return;
    // Optimistic, so the message does not appear to vanish; the server's own
    // chat frame replaces it when it arrives.
    (S.chats[id] = S.chats[id] || []).push({
      id: 'local-' + Date.now(), who: 'you', body: body,
      ts: new Date().toISOString(), state: 'sent'
    });
    box.value = '';
    drop(draftKey(S.docKey, role));
    if (S.sel) renderInspector(); else renderHome();
    hydrate();
  }

  function insertAtCursor(snippet, back) {
    /* The editor that is SHOWING. It survives a tab switch now, so asking the
       panel for a textarea would find one belonging to a block whose source
       the author is not looking at. */
    var src = activeEditor();
    if (!src) return;
    var id = src.getAttribute('data-block');
    // A textarea that has never been focused reports selectionStart 0, which is
    // indistinguishable from a caret deliberately placed at the start. Inserting
    // on that reading silently prepends to the paragraph, and that is how two
    // empty footnotes reached a real manuscript. With no caret of its own, the
    // end of the block is the only honest guess.
    var caretKnown = document.activeElement === src ||
      (S.caret && S.caret.id === id);
    var at = caretKnown ? src.selectionStart : src.value.length;
    var out = spliceAt(src.value, at, snippet);
    src.value = out.text;
    var caret = out.caret - (back || 0);
    try { src.setSelectionRange(caret, caret); } catch (e) { /* ignore */ }
    src.focus();
    setDraft(id, src.value);
    rememberCaret(id, src);
    scheduleSave(id);
  }

  // ---------------------------------------------------------------- patching

  // Editing a paragraph changes its content-derived id, so the server sends the
  // old-to-new mapping with the patch. Ignoring it left the page unable to find
  // the element to replace: the new markup was appended at the end and the
  // stale paragraph stayed put, so the author's own edit duplicated the
  // paragraph in front of them. Everything keyed to the id moves with it.
  function applyRenames(renamed) {
    if (!renamed) return;
    // The queue is a list rather than a bag keyed by block, so it cannot ride
    // the loop below. Left behind, its entries would name the paragraph as it
    // was before the agent answered it.
    S.queue = renameQueue(S.queue, renamed);
    S.ticker = renameQueue(S.ticker, renamed);
    Object.keys(renamed).forEach(function (rawOld) {
      var from = normId(rawOld), to = normId(renamed[rawOld]);
      if (!to || from === to) return;

      var el = blockEl(from);
      if (el) el.setAttribute('data-mx', to);

      /* The open editor names its block in an attribute, and reads it there on
         every keystroke, so this line is what keeps typing after a save landing
         on the live block instead of on the id the save renamed away. */
      var wired = document.querySelectorAll('[data-block="' + from + '"]');
      for (var w = 0; w < wired.length; w++) wired[w].setAttribute('data-block', to);

      ['blocks', 'chats', 'drafts', 'blockState', 'save', 'sent'].forEach(function (bag) {
        var store = S[bag];
        if (store && Object.prototype.hasOwnProperty.call(store, from)) {
          store[to] = store[from];
          delete store[from];
        }
      });
      if (S.focusedBlock === from) S.focusedBlock = to;
      if (S.saveFor === from) S.saveFor = to;
      if (S.caret && S.caret.id === from) S.caret.id = to;
      if (S.sel && S.sel.blockId === from) S.sel.blockId = to;
      if (S.sel && S.sel.kind === 'block' && S.sel.key === from) S.sel.key = to;
      S.back.forEach(function (b) {
        if (b.blockId === from) b.blockId = to;
        if (b.kind === 'block' && b.key === from) b.key = to;
      });
    });
  }

  function applyBlockHtml(id, html) {
    var old = blockEl(id);
    var nodes = nodesFromHtml(html);
    if (!nodes.length) return;
    if (!nodes[0].hasAttribute('data-mx')) nodes[0].setAttribute('data-mx', id);

    if (old && old.parentNode) {
      var parent = old.parentNode;
      var run = blockRun(old);
      var after = run[run.length - 1].nextSibling;
      for (var i = 0; i < run.length; i++) parent.removeChild(run[i]);
      for (var j = 0; j < nodes.length; j++) parent.insertBefore(nodes[j], after);
    } else {
      for (var k = 0; k < nodes.length; k++) docInner.appendChild(nodes[k]);
    }
    for (var n = 0; n < nodes.length; n++) hydrateSpans(nodes[n]);
    if (anchorEl === old) anchorEl = nodes[0];
  }

  /* Behaviour 3. A patch for the block under the cursor waits for the blur.
     Otherwise the author's own save re-renders the paragraph they are typing
     into and takes the caret with it. */
  /* Did this page write it? The server cannot tell one writer from another, and
     should not: it has no knowledge of who is on the other end. This page does
     know what it sent, so it can keep the author's own saves out of a ticker
     that is meant to show what someone ELSE did to his manuscript. */
  function isOwnEdit(rawId, msg) {
    var data = msg.blockdata && msg.blockdata[rawId];
    if (!data) return false;
    var src = String(data.source || '');
    var keys = Object.keys(S.sent);
    for (var i = 0; i < keys.length; i++) {
      if (S.sent[keys[i]] === src) return true;
    }
    return false;
  }

  function onPatch(msg) {
    var ui = captureUI();
    applyRenames(msg.renamed);
    var blocks = msg.blocks || {};
    var touched = {};
    var theirs = Object.keys(blocks).filter(function (raw) { return !isOwnEdit(raw, msg); });

    /* A block that is new to this page has no element to replace, and
       applyBlockHtml has no position to work from. Route it to the `added`
       handler, which knows where the server said it goes. The server already
       keeps the two lists disjoint; this is the belt for that brace, because
       the fallback it protects against is an append to the end of the document
       and the symptom is a figure rendering below the bibliography. */
    var addedIds = {};
    (msg.added || []).forEach(function (a) {
      if (a && typeof a !== 'string' && a.id) addedIds[normId(a.id)] = true;
    });

    Object.keys(blocks).forEach(function (raw) {
      var id = normId(raw);
      touched[id] = true;
      if (msg.blockdata && msg.blockdata[raw]) S.blocks[id] = msg.blockdata[raw];
      /* The render updates while you type, including for the block you are
         typing in. What must not be rebuilt under a cursor is the INSPECTOR,
         because that replaces the textarea and takes the caret with it. The
         caret was never in the document paragraph, so holding that paragraph
         back protected nothing and cost the author the one thing they were
         looking for: their own sentence appearing in the manuscript. The panel
         rebuild is deferred to the blur instead. */
      if (!addedIds[id]) applyBlockHtml(id, blocks[raw]);
      if (id === S.focusedBlock) S.deferredPanels[id] = true;
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

    /* A new block goes where the server says it goes: after the block that
       precedes it in the rebuilt document, or at the very start when `after` is
       null because it is now the first thing in the manuscript. Appending to
       the end instead is what put Figure 2 below the bibliography on
       2026-07-27, with the source, the built HTML and the PDF all correct. */
    (msg.added || []).forEach(function (a) {
      if (!a || typeof a === 'string') return;   // nothing to insert without html
      var id = normId(a.id);
      touched[id] = true;
      if (a.block) S.blocks[id] = a.block;
      var nodes = nodesFromHtml(a.html || '');
      if (!nodes.length) return;
      if (!nodes[0].hasAttribute('data-mx')) nodes[0].setAttribute('data-mx', id);

      var mark = null;                       // insert before this
      if (a.after) {
        var prev = blockEl(normId(a.after));
        /* The preceding block may itself render as several elements, so step
           past its whole run rather than past its anchor alone. */
        if (prev && prev.parentNode) {
          var run = blockRun(prev);
          mark = run[run.length - 1].nextSibling;
        } else if (window.console) {
          /* It should always be there: `after` names a block of the NEW
             document, which is either untouched, patched, renamed ahead of this
             loop, or added earlier in this same document-ordered pass. If it is
             missing the page has drifted, and the append below is a guess. */
          console.warn('added block ' + id + ' names a predecessor (' + a.after +
            ') that is not on the page; appending at the end');
        }
      } else {
        mark = docInner.firstChild;
      }
      var parent = mark && mark.parentNode ? mark.parentNode : docInner;
      for (var i = 0; i < nodes.length; i++) parent.insertBefore(nodes[i], mark);
    });

    hydrate();
    // Rebuild the panel only when the patch actually reached what is open, and
    // never while its block has focus: an untouched selection is unchanged, and
    // a touched-but-focused one is deferred until blur anyway.
    if (S.sel && touched[S.sel.blockId] && S.sel.blockId !== S.focusedBlock) renderInspector();
    restoreUI(ui);
    updateMeta();

    // The ticker reports the edit LANDING, not the claim that it would. A `done`
    // with no patch behind it is a comment closed without the file changing,
    // and the author should be able to see the difference.
    if (theirs.length) {
      var first = normId(theirs[0]);
      pushTicker({
        kind: 'patch', block: first, section: sectionOf(first),
        n: theirs.length, when: new Date().toISOString()
      });
    }
  }

  /* The document was patched as it happened; what waited for the blur is the
     panel, whose source editor still shows the text as it was when the cursor
     arrived. Rebuilding it now is safe, and it is what puts a change made by
     someone else into the editor the author is about to use. */
  function flushDeferred(id) {
    if (!Object.prototype.hasOwnProperty.call(S.deferredPanels, id)) return;
    delete S.deferredPanels[id];
    var ui = captureUI();
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

    /* The server came back, which with a stable port is the ordinary way a
       server restart ends. Anything typed while it was gone reached no disk, so
       the first thing the reconnect does is hand those drafts over. */
    sock.onopen = function () { S.retry = 0; setLive('live'); flushDrafts(); };
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
    for (var e = 0; e < EXT.length; e++) {
      var fr = EXT[e].frame && EXT[e].frame[msg.type];
      if (fr) { try { fr(msg, api.ext); } catch (err) { /* one extension must not break the page */ } }
    }
    switch (msg.type) {
      case 'patch':
        onPatch(msg);
        break;
      case 'state': {
        var id = normId(msg.block);
        S.blockState[id] = msg.state;
        // Move the chat message's own label too, or it reads "working"
        // forever after the work landed.
        if (msg.id) {
          var bag2 = S.chats[id] || [];
          for (var bq = 0; bq < bag2.length; bq++) {
            if (bag2[bq] && bag2[bq].id === msg.id) bag2[bq].state = msg.state;
          }
          if (S.sel && S.sel.blockId === id) renderInspector();
          else if (!S.sel && id === '') renderHome();
        }
        // `queued` is the standing state and the header already counts it. A
        // new comment arrives as a `queued` frame, so pushing those filled the
        // ticker with the author's own three comments and pushed the agent's
        // work off the end of it. The ticker is for what MOVED.
        if (msg.state !== 'queued') {
          /* `id` and `asked` come off the frame and are not re-derived here.
             The line leads with the REQUEST, and the request was already
             clipped server-side by the same function that clips the seed; a
             second shortener in this file would be a second answer to one
             question, differing by a word or an ellipsis depending on which
             half of the ticker the author happened to be reading. `id` is what
             tells two requests on one paragraph apart, in the margin label and
             in `tickerKey` both -- dropping it here left that dedup inert for
             every entry that arrived live. */
          pushTicker({
            kind: 'state', state: msg.state, block: id,
            id: msg.id, asked: msg.asked,
            section: sectionOf(id), when: msg.at || new Date().toISOString()
          });
        }
        hydrate();
        if (S.sel && S.sel.blockId === id) renderInspector();
        break;
      }
      case 'queue': {
        S.queue = (msg.queue || []).map(function (e) {
          if (!e || !e.block) return e;
          var copy = {};
          Object.keys(e).forEach(function (k) { copy[k] = e[k]; });
          copy.block = normId(e.block);
          return copy;
        });
        renderAgent();
        break;
      }
      case 'chat': {
        var cid = normId(msg.block);
        var incoming = msg.message || {};
        // The same message arrives up to three times: added optimistically on
        // send, echoed by the server, and broadcast again when the log changes.
        // Identity is the message id, so anything already held wins.
        var bag = (S.chats[cid] = S.chats[cid] || []);
        var at = -1;
        for (var q = 0; q < bag.length; q++) {
          if (!bag[q]) continue;
          // The server's copy, or the optimistic one it is the answer to. The
          // local placeholder carries a `local-` id and the same body, and the
          // comment appeared twice until it was claimed here.
          var isSame = (incoming.id && bag[q].id === incoming.id) ||
            (String(bag[q].id || '').indexOf('local-') === 0 && bag[q].body === incoming.body);
          if (isSame) { at = q; break; }
        }
        if (at >= 0) bag[at] = incoming; else bag.push(incoming);
        hydrate();
        if (S.sel && S.sel.blockId === cid) renderInspector();
        else if (!S.sel && cid === '') renderHome();
        break;
      }
      case 'todos': {
        renderTodos(msg.todos || []);
        break;
      }
      case 'assets': {
        // A figure regenerated with no source change beside it: refetch every
        // image past the browser cache. The src path is stable; the version
        // query is what forces the miss.
        var pics = docInner ? docInner.querySelectorAll('img') : [];
        for (var pi = 0; pi < pics.length; pi++) {
          var base = pics[pi].getAttribute('src').split('?')[0];
          pics[pi].setAttribute('src', base + '?v=' + encodeURIComponent(msg.v || Date.now()));
        }
        break;
      }
      case 'cites': {
        // The verdicts moved -- the evidence pass finished, or an ordinary
        // rebuild re-read its records. Recolour every underline in place, no
        // reload.
        S.ms.cites = msg.cites || {};
        hydrateSpans(docInner);
        if (S.sel && S.sel.kind === 'cite') renderInspector();
        break;
      }
      case 'stats': {
        /* The counts in the header are derived from the whole build and belong
           to no block, so no patch can carry them. Nothing sent them at all and
           the header read whatever the page was born with: a manuscript that
           gained a citation still said "1 citations" until it was reloaded. */
        S.ms.stats = msg.stats || {};
        updateMeta();
        break;
      }
      case 'evidence': {
        var btn = document.getElementById('evidence-run');
        if (msg.done) {
          if (btn) { btn.disabled = false; btn.textContent = 'Run evidence…'; }
          showRepair(msg.missing);
          pushTicker({ text: msg.ok ? 'evidence pass finished' : 'evidence pass failed; see the terminal',
                       when: new Date().toISOString() });
        } else if (msg.line) {
          if (btn) { btn.disabled = true; btn.textContent = 'Evidence running…'; }
          pushTicker({ text: String(msg.line), when: new Date().toISOString() });
        }
        break;
      }
      case 'feed': {
        S.ms.agent_feed = msg.feed || {};
        setAgent(msg.feed);
        if (!S.sel) renderHome();
        break;
      }
      case 'history': {
        /* The ledger grew, or an outcome landed in the comment log. The card
           is swapped by itself rather than through `renderHome`, which would
           take the composer's caret and the author's open rows with it. */
        S.ms.history = msg.history || [];
        renderAgentHistory();
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

  /* The outline: one button, two meanings, decided by what is on screen rather
     than by the stored state. Wide, the rail is a column and the button hides it
     to give the manuscript the width; narrow, the rail is not a column at all
     and the button floats it over the manuscript. Reading the computed
     visibility is what lets a single control be right in both regimes -- an
     attribute alone would have the button lying at one of the two widths. */
  function railShown() {
    return !!(railEl && railEl.offsetWidth > 0);
  }

  function syncRailButton() {
    var b = document.getElementById('rail-toggle');
    if (!b) return;
    var on = railShown();
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.setAttribute('title', on ? 'Hide the outline' : 'Show the outline');
  }

  function applyRail(state) {
    if (state === 'on' || state === 'off') {
      appEl.setAttribute('data-rail', state);
      store(MS_PREF_PREFIX + 'rail', state);
    } else {
      appEl.removeAttribute('data-rail');
      drop(MS_PREF_PREFIX + 'rail');
    }
    syncRailButton();
  }

  function toggleRail() {
    applyRail(railShown() ? 'off' : 'on');
  }

  /* An overlaid rail is over the manuscript, so leaving it open after a jump
     hides the paragraph that was jumped to. As a column it stays, because it is
     costing the reader nothing. */
  function railIsOverlay() {
    return !!(railEl && window.getComputedStyle(railEl).position === 'absolute');
  }

  /* The manuscript/inspector divide, moved by the reader.
     The default (inspector takes the surplus) is the arrangement with no wasted
     gutter beside the prose, so this exists for the case the default cannot
     serve: a very wide display, or an afternoon spent in the source editor. A
     width the reader set is theirs, so it is stored; double-click hands it back. */
  function applySplit(px) {
    var w = clampSplit(px, window.innerWidth);
    if (w === null) {
      appEl.removeAttribute('data-split');
      appEl.style.removeProperty('--insp-w');
      drop(MS_PREF_PREFIX + 'insp');
      return;
    }
    appEl.setAttribute('data-split', 'user');
    appEl.style.setProperty('--insp-w', w + 'px');
    store(MS_PREF_PREFIX + 'insp', String(w));
  }

  function currentSplit() {
    return inspEl ? Math.round(inspEl.getBoundingClientRect().width) : 0;
  }

  function wireSplit() {
    var handle = document.getElementById('split');
    if (!handle || !appEl) return;

    handle.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      // Capture keeps the moves coming even when the pointer crosses the prose,
      // but a pointer id the platform does not know is not a reason to refuse
      // the drag.
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* uncaptured */ }
      appEl.setAttribute('data-dragging', '');
    });
    handle.addEventListener('pointermove', function (e) {
      if (!appEl.hasAttribute('data-dragging')) return;
      // The inspector is flush to the right edge, so its width is the distance
      // from the pointer to that edge. Read live rather than from a start offset,
      // which keeps the handle under the finger if the window resizes mid-drag.
      applySplit(window.innerWidth - e.clientX);
    });
    var end = function (e) {
      if (!appEl.hasAttribute('data-dragging')) return;
      appEl.removeAttribute('data-dragging');
      try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* already gone */ }
    };
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);

    // Back to the default, which is the layout the design argues for.
    handle.addEventListener('dblclick', function () { applySplit(0); });

    // The same control from the keyboard, because a drag handle that only takes
    // a pointer is a control some readers do not have.
    handle.addEventListener('keydown', function (e) {
      var step = e.shiftKey ? 64 : 16;
      if (e.key === 'ArrowLeft') { e.preventDefault(); applySplit(currentSplit() + step); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); applySplit(currentSplit() - step); }
      else if (e.key === 'Home' || e.key === 'Escape') { e.preventDefault(); applySplit(0); }
    });
  }

  /* One hue, declared on the ROOT so both skins and the neutrals read it. The
     status colours are excluded from the palette on purpose: they mean
     something, so they must not rotate with the decoration. */
  function applyHue(hex) {
    var hs = hexToHS(hex);
    document.documentElement.style.setProperty('--h', String(clampHue(hs.h)));
    document.documentElement.style.setProperty('--sat', hs.s.toFixed(2));
    store(MS_PREF_PREFIX + 'hue', hex);
  }

  // ------------------------------------------------- the agent, in the header
  //
  // A session that edits the author's prose while he is reading is only
  // acceptable if he can glance up and see what it is doing. The margin pin
  // answers "is anything happening on THIS paragraph"; these two answer "what
  // is happening at all", which is the question he actually has.

  var TICKER_SHOWN = 5;    // a handful, not a scrollback
  var TICKER_KEEP = 12;

  /* What to call a block in front of the author. Its section first, because
     that is how he thinks about the paper. Some blocks have none -- an abstract
     sits above every heading -- and a real session picking one up reported "the
     manuscript · working", which told him nothing. A file and a line is
     somewhere he can go. */
  function sectionOf(id) {
    var b = S.blocks[id];
    var name = blockName(b);
    if (name) return name;
    for (var i = 0; i < S.queue.length; i++) {
      var e = S.queue[i];
      if (e && e.block === id && (e.section || e.where)) return e.section || e.where;
    }
    if (b && b.file) return String(b.file) + (b.line_start ? ':' + b.line_start : '');
    return null;
  }

  /* The one-line summary is deliberately terse, so the detail goes where it
     costs nothing to carry: the oldest few, in the order they will be worked. */
  function queueTitle() {
    if (!S.queue.length) return 'Nothing queued.';
    return S.queue.slice(0, 4).map(function (e) {
      return (e.section || e.where || 'the manuscript') + ' · ' + e.state +
        (e.since ? ' ' + ago(e.since) : '') + (e.body ? ' — ' + e.body : '');
    }).join('\n');
  }

  function setAgent(feed) {
    S.ms.agent_feed = feed || S.ms.agent_feed || {};
    renderAgent();
  }

  function renderAgent() {
    if (agentEl) {
      var text = agentEl.querySelector('.atext');
      if (text) text.textContent = queueSummary(S.queue);
      var feed = S.ms.agent_feed || {};
      var working = feed.state === 'working' ||
        S.queue.some(function (e) { return e && e.state === 'working'; });
      agentEl.classList.toggle('is-working', working);
      agentEl.classList.toggle('is-idle', S.queue.length === 0);
      agentEl.setAttribute('title', queueTitle());
    }
    renderTicker();
    // Here rather than in `setAgent`, so the 15-second tick ages the card's own
    // timestamps too and does not leave it claiming work started "just now".
    renderAgentFeed();
    // The history ages the same way: "✓ done 4m" is only true for a minute.
    renderAgentHistory();
  }

  function tickerKey(items) {
    return items.map(function (e) {
      // `id` is here because the block no longer identifies a line: two
      // requests on one paragraph share kind, state, block and often the
      // second they landed in, and without it the second never re-renders.
      return [e.kind, e.state, e.id, e.block, e.when, e.n].join('|');
    }).join(';');
  }

  /* Re-rendering wholesale every refresh would restart the entry animation on
     the newest line every fifteen seconds, which is a status line demanding
     attention it has not earned. Same entries: only their ages move. */
  function renderTicker() {
    if (!tickerEl) return;
    var items = S.ticker.slice(0, TICKER_SHOWN);
    var key = tickerKey(items);
    if (key === S.tickerKey) {
      var times = tickerEl.querySelectorAll('time');
      for (var i = 0; i < times.length && i < items.length; i++) {
        times[i].textContent = items[i].when ? ago(items[i].when) : '';
      }
      return;
    }
    S.tickerKey = key;
    tickerEl.hidden = items.length === 0;
    /* The title of an entry is a place, so it goes there. Reading "Discussion ·
       flagged for review" and then having to find the Discussion yourself is the
       page knowing where it means and not saying. A document-level entry has no
       block to jump to, so it stays plain rather than pretending. */
    /* The label is clamped in CSS rather than cut here: a caption is a whole
       sentence and five of them would push the rest of the ticker off the end
       of the row, but the full text belongs in the DOM for the tooltip and for
       anyone reading it aloud. Which means even the plain case needs an
       element to clamp. */
    tickerEl.innerHTML = items.map(function (e, i) {
      var full = tickerText(e);
      var label = esc(full);
      var hover = esc(tickerTitle(e));
      var id = e.block ? normId(e.block) : '';
      var title = (id && S.blocks[id])
        ? '<button type="button" class="tkgo" title="' + hover + '" data-goto="' +
          esc(id) + '">' + label + '</button>'
        : '<span class="tklabel" title="' + hover + '">' + label + '</span>';
      return '<span class="tk' + (i ? '' : ' now') + '">' + title +
        '<time>' + esc(e.when ? ago(e.when) : '') + '</time></span>';
    }).join('');
  }

  function pushTicker(e) {
    S.ticker.unshift(e);
    if (S.ticker.length > TICKER_KEEP) S.ticker.length = TICKER_KEEP;
    renderTicker();
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
    for (var e = 0; e < EXT.length; e++) {
      var fn = EXT[e].open && EXT[e].open[key];
      if (fn) { S.extView = { key: key, build: fn }; select('ext', key, S.sel && S.sel.blockId); return; }
    }

    var parts = String(key).split(':');
    var kind = parts[0];
    var rest = parts.slice(1).join(':');
    if (kind === 'block') { select('block', normId(rest), normId(rest)); return; }
    if (kind === 'cite') { selectDetour('cite', rest, S.sel && S.sel.blockId); return; }
    if (kind === 'value') { selectDetour('value', rest, S.sel && S.sel.blockId); return; }
    // An insert opens inline on References rather than taking over the panel.
    if (kind === 'insert') { openInsert(rest); return; }
    selectDetour('panel', key, S.sel && S.sel.blockId);
  }

  function onClick(e) {
    var skin = e.target.closest('.skinctl .tb[data-skin]');
    if (skin) { applySkin(skin.getAttribute('data-skin')); return; }

    // A section's subsections fold away. The state lives on the button, so a
    // patch that rebuilds the rail cannot silently reopen what was closed.
    var twist = e.target.closest('[data-twist]');
    if (twist) {
      e.preventDefault();
      var top = twist.getAttribute('data-twist');
      var open = twist.getAttribute('aria-expanded') !== 'false';
      twist.setAttribute('aria-expanded', open ? 'false' : 'true');
      twist.setAttribute('aria-label', open ? 'Expand this section' : 'Collapse this section');
      var kids = document.querySelectorAll('.outline a[data-under="' + top + '"]');
      for (var k = 0; k < kids.length; k++) kids[k].classList.toggle('is-hidden', open);
      return;
    }

    var goto_ = e.target.closest('[data-goto]');
    if (goto_) {
      e.preventDefault();
      if (railEl && railEl.contains(goto_) && railIsOverlay()) applyRail('off');
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
      var name = act.getAttribute('data-act');
      var claimed = false;
      for (var e = 0; e < EXT.length; e++) {
        var fn = EXT[e].act && EXT[e].act[name];
        if (fn) { claimed = true; try { fn(api.ext); } catch (err) { /* ignore */ } }
      }
      if (!claimed) doAct(name);
      return;
    }

    var opener = e.target.closest('[data-open]');
    if (opener) { e.preventDefault(); e.stopPropagation(); openKey(opener.getAttribute('data-open')); return; }

    var cite = e.target.closest('.cite[data-key]');
    if (cite) {
      e.stopPropagation();
      var host = cite.closest('[data-mx]');
      selectDetour('cite', cite.getAttribute('data-key'), host ? host.getAttribute('data-mx') : null);
      return;
    }

    var val = e.target.closest('.val[data-key]');
    if (val) {
      e.stopPropagation();
      var vhost = val.closest('[data-mx]');
      selectDetour('value', val.getAttribute('data-key'), vhost ? vhost.getAttribute('data-mx') : null);
      return;
    }

    var blk = e.target.closest('[data-mx]');
    if (blk) {
      var bid = blk.getAttribute('data-mx');
      select('block', bid, bid);
      return;
    }

    // A click on the document's own background, between and beside the
    // paragraphs, is the way back out to the document chat.
    if (e.target.closest('.doc')) deselect();
  }

  /* Selection has always had a way in and never a way out: once a paragraph
     was open there was no returning to the document chat. Escape and a click
     on the document's own background both come back here. */
  function deselect() {
    if (!S.sel) return;
    retireRestored();
    S.sel = null;
    S.extView = null;
    S.insert = null;
    var on = document.querySelectorAll('.blk.sel, .exhibit.sel');
    for (var i = 0; i < on.length; i++) on[i].classList.remove('sel');
    anchorEl = null;
    renderHome();
  }

  /* Open a paragraph and scroll to it. One implementation, because the feed
     card and the history card both offer the jump and two copies of it would
     be two answers to "what does selecting a block from the panel do". */
  function gotoBlock(block) {
    var gid = block ? normId(block) : null;
    if (!gid || !S.blocks[gid]) return;
    select('block', gid, gid, null, { keepDoc: false });
    requestAnimationFrame(function () {
      var el = blockEl(gid);
      if (el) el.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'start' });
    });
  }

  function doAct(act) {
    if (act === 'rail:toggle') { toggleRail(); return; }
    if (act === 'feed:goto') {
      /* The feed names the comment being worked; the comment names the block. */
      var w = ((S.ms.agent_feed || {}).working || [])[0];
      var hit = w && S.queue.filter(function (e) { return e && e.id === w; })[0];
      gotoBlock(hit && hit.block);
      return;
    }
    if (act.indexOf('hist:goto:') === 0) {
      // A history row names its block outright, so no lookup is needed.
      gotoBlock(act.slice('hist:goto:'.length));
      return;
    }
    if (act === 'evidence:run') { runEvidence(); return; }
    if (act === 'repair:run') { runRepair(); return; }
    if (act.indexOf('finding:dismiss:') === 0) {
      var dfid = act.slice('finding:dismiss:'.length);
      var dhit = findingById(dfid);
      if (send({ type: 'dismiss', id: dfid })) {
        if (dhit) dhit.msg.state = 'done';
        if (S.sel) renderInspector(); else renderHome();
      }
      return;
    }
    if (act.indexOf('finding:fix:') === 0) {
      var ffid = act.slice('finding:fix:'.length);
      var fhit = findingById(ffid);
      if (!fhit) return;
      var ask = 'Address the ' + (fhit.msg.who || 'review') + ' finding ' + ffid + ': ' + fhit.msg.body;
      if (send({ type: 'chat', block: fhit.block, body: ask })) {
        (S.chats[fhit.block] = S.chats[fhit.block] || []).push({
          id: 'local-' + Date.now(), who: 'you', body: ask,
          ts: new Date().toISOString(), state: 'sent'
        });
        if (S.sel) renderInspector(); else renderHome();
        hydrate();
      }
      return;
    }
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
    if (act === 'ins:close') { closeInsert(); return; }
    if (act === 'ins:go') {
      var f = ibodyEl.querySelector('[data-role="insert"]');
      var said = f ? f.value.trim() : '';
      if (!said) return;
      // The protocol carries `edit`, which writes one block. A citation, a
      // number and an exhibit are each a coordinated write across three or four
      // files, so the request goes to the block's chat rather than pretending a
      // button can do it. Naming what will happen beats a control that lies.
      send({ type: 'chat', block: S.sel.key, body: 'Insert a ' + S.insert + ' here: ' + said });
      closeInsert();
      S.tab = 1;
      renderInspector();
      return;
    }
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

    /* Ctrl+Z, for the plain browser tab. Cmd+Z is left strictly alone: the
       browser and the Mac app's Edit menu (whose key equivalent IS Cmd+Z) both
       already reach the textarea's own history, and a second handler on it
       would undo twice. Ctrl+Z reaches nothing on macOS -- measured, Chrome
       151 -- so binding it takes nothing away and gives the same key on every
       platform. `preventDefault` first, or a Linux browser would run its own
       undo as well as this one. Nothing else in the page binds Z. */
    if (e.ctrlKey && !e.metaKey && !e.altKey && String(e.key).toLowerCase() === 'z') {
      if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) {
        e.preventDefault();
        try { document.execCommand(e.shiftKey ? 'redo' : 'undo'); } catch (err) { /* no editor */ }
        return;
      }
    }

    if ((e.key === 'Enter' || e.key === ' ') && t && t.matches &&
        t.matches('.blk, .exhibit, .cite, .val, [data-goto]')) {
      e.preventDefault();
      t.click();
    }
    if (e.key === 'Escape') {
      var pop = document.getElementById('hue-pop');
      if (pop && !pop.hidden) { pop.hidden = true; return; }
      // Escape in a field leaves the field; a second Escape leaves the
      // selection. Deselecting under a half-typed draft would be rude even
      // though the draft itself survives by construction.
      if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) { t.blur(); return; }
      deselect();
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

  // A disclosure control on a section with nothing under it is a control that
  // does nothing. Hidden rather than removed, so the rail's alignment holds.
  function hideChildlessTwists() {
    var ts = document.querySelectorAll('.twist');
    for (var i = 0; i < ts.length; i++) {
      var top = ts[i].getAttribute('data-twist');
      if (!document.querySelector('.outline a[data-under="' + top + '"]')) {
        ts[i].style.visibility = 'hidden';
      }
    }
  }

  function boot() {
    hideChildlessTwists();
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
    backBtn = document.getElementById('back');
    headSaveEl = document.getElementById('headsave');
    if (backBtn) backBtn.addEventListener('click', goBack);
    liveEl = document.getElementById('live');
    liveTextEl = document.getElementById('live-text');
    metaEl = document.getElementById('toolbar-meta');
    outlineEl = document.getElementById('outline');
    pathEl = document.getElementById('doc-path');
    hueWheel = document.getElementById('hue-wheel');
    agentEl = document.getElementById('agent-status');
    tickerEl = document.getElementById('ticker');

    S.ms = window.MS || {};
    S.blocks = S.ms.blocks || {};
    S.chats = S.ms.chats || {};
    // Keyed by the document, not the page: one directory can serve several
    // documents (blob "main"/"docs"), and a draft typed on the appendix must
    // not surface on the paper.
    S.docKey = String(S.ms.main || S.ms.title || window.location.pathname || 'doc');

    // The document switcher. Hidden when there is nothing to choose; changing
    // it navigates, because a different document is a different page.
    var docSwitch = document.getElementById('doc-switch');
    if (docSwitch) {
      // Each value is a document's path relative to the served root
      // (e.g. "latex/main.tex" or, at the root, just "main.tex"). Grouped by
      // folder so the switcher shows the tree the project holds; a flat
      // manuscript directory has one group and reads exactly as it did before.
      var docs = S.ms.docs || [];
      if (docs.length > 1) {
        var groups = {}, order = [];
        for (var di = 0; di < docs.length; di++) {
          var val = docs[di];
          var cut = val.lastIndexOf('/');
          var folder = cut < 0 ? '' : val.slice(0, cut);
          if (!(folder in groups)) { groups[folder] = []; order.push(folder); }
          groups[folder].push(val);
        }
        var manyGroups = order.length > 1;
        for (var gi = 0; gi < order.length; gi++) {
          var folder = order[gi];
          var parent = docSwitch;
          if (manyGroups || folder) {
            var og = document.createElement('optgroup');
            og.label = folder || '(root)';
            docSwitch.appendChild(og);
            parent = og;
          }
          var vals = groups[folder];
          for (var vi = 0; vi < vals.length; vi++) {
            var opt = document.createElement('option');
            opt.value = vals[vi];
            var base = vals[vi].slice(vals[vi].lastIndexOf('/') + 1);
            opt.textContent = base.replace(/\.tex$/, '');
            if (vals[vi] === S.ms.main) opt.selected = true;
            parent.appendChild(opt);
          }
        }
        docSwitch.hidden = false;
        docSwitch.addEventListener('change', function () {
          window.location.search = '?main=' + encodeURIComponent(docSwitch.value);
        });
      }
    }
    // Seeded from the blob so a page opened while a session is halfway through
    // the third comment does not open on "idle" and an empty ticker.
    S.queue = S.ms.queue || [];
    S.ticker = (S.ms.ticker || []).slice(0, TICKER_KEEP);

    restoreDrafts();
    hydrate();
    renderHome();
    updateMeta();
    renderAgent();
    // "how long it has been in that state" has to keep being true. Ages only:
    // renderTicker rebuilds nothing when the entries themselves have not moved.
    window.setInterval(renderAgent, 15000);

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
    if (savedHue) applyHue(savedHue);

    /* Whether the rail is on screen is a function of the window as well as the
       preference, so the button is re-read on a resize: crossing the width at
       which the rail stops being a column changes what the same button does. */
    /* The divide the reader set, if they set one. Re-clamped on every resize,
       because a width chosen on a large display would otherwise leave a narrow
       window with no manuscript at all. */
    wireSplit();
    var savedSplit = parseInt(load(MS_PREF_PREFIX + 'insp') || '', 10);
    if (savedSplit > 0) applySplit(savedSplit);
    window.addEventListener('resize', function () {
      if (appEl.getAttribute('data-split') === 'user') applySplit(currentSplit());
    }, { passive: true });

    var savedRail = load(MS_PREF_PREFIX + 'rail');
    applyRail(savedRail === 'on' || savedRail === 'off' ? savedRail : null);
    window.addEventListener('resize', function () {
      if (railSync) return;
      railSync = true;
      window.requestAnimationFrame(function () { railSync = false; syncRailButton(); });
    }, { passive: true });

    /* The hue picker is the page's own popover. The native <input type=color>
       panel anchored wherever the platform pleased (seen opening over the
       source editor, nowhere near the swatch) and offered a preset grid the
       design rejected: one wheel, no presets, no second surface. Angle is
       hue, radius is saturation, lightness is fixed; the pick still round-
       trips through applyHue so the saved preference and the clamp behave
       identically to a typed hex. */
    var huePop = document.getElementById('hue-pop');
    var hueDisc = document.getElementById('hue-disc');
    if (hueWheel && huePop && hueDisc) {
      hueWheel.addEventListener('click', function () {
        huePop.hidden = !huePop.hidden;
        hueWheel.setAttribute('aria-expanded', huePop.hidden ? 'false' : 'true');
      });
      document.addEventListener('pointerdown', function (e) {
        if (!huePop.hidden && !e.target.closest('.hues')) {
          huePop.hidden = true;
          hueWheel.setAttribute('aria-expanded', 'false');
        }
      });
      var picking = false;
      var pickFrom = function (e) {
        var r = hueDisc.getBoundingClientRect();
        var dx = e.clientX - r.left - r.width / 2;
        var dy = e.clientY - r.top - r.height / 2;
        var hue = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
        var sat = Math.min(1, Math.sqrt(dx * dx + dy * dy) / (r.width / 2));
        applyHue(hslToHex(hue, Math.max(0.08, sat), 0.55));
      };
      hueDisc.addEventListener('pointerdown', function (e) {
        picking = true;
        try { hueDisc.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
        pickFrom(e);
      });
      hueDisc.addEventListener('pointermove', function (e) { if (picking) pickFrom(e); });
      hueDisc.addEventListener('pointerup', function () { picking = false; });
    }

    renderTodos(S.ms.todos || []);
    showRepair(S.ms.missing_fulltexts);
    wireSkillMenu('checks-menu');
    wireSkillMenu('produce-menu');
    var todosBox = document.getElementById('todos');
    if (todosBox) {
      todosBox.addEventListener('change', function (e) {
        var cb = e.target.closest('input[data-todo]');
        if (cb) send({ type: 'todo_toggle', id: cb.getAttribute('data-todo'), done: cb.checked });
      });
    }
    var todoAdd = document.getElementById('todo-add');
    if (todoAdd) {
      todoAdd.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        var text = todoAdd.value.trim();
        if (!text) return;
        if (send({ type: 'todo', text: text })) todoAdd.value = '';
      });
    }

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

  /* The standing state, readable by a host that is not a websocket: the
     standalone shell, or a check driving the page. Both need what the header is
     showing without re-deriving it from frames they never saw. Copies, so a
     reader cannot quietly become a writer. */
  api.agentState = function () {
    return { queue: S.queue.slice(), ticker: S.ticker.slice() };
  };

  /* ------------------------------------------------------------ extensions

     Four features want to add a panel, a toolbar action or a frame handler,
     and four of them editing this file would be four sets of conflicting
     edits to one 1400-line module. An extension registers instead, in its own
     script, and is handed exactly what it needs.

       MSViewer.extend({
         name: 'compile',
         act: {'compile:pdf': fn},          // data-act handlers
         open: {'review': fn},              // data-open panels -> view object
         frame: {'compiled': fn},           // inbound websocket frames
         tab: function (id, block) {...}    // an extra inspector tab, or null
       })

     The viewer calls these; extensions never reach into S. Anything an
     extension needs that is not passed to it is a gap in this contract, and
     the fix is to widen the contract rather than to reach around it. */
  var EXT = [];

  api.extend = function (ext) {
    if (!ext || !ext.name) return;
    EXT.push(ext);
    if (S.sel) renderInspector();
  };

  api.ext = {
    send: send,
    escape: esc,
    card: card,
    block: function (id) { return S.blocks[normId(id)] || null; },
    name: function (b) { return blockName(b); },
    selection: function () { return S.sel ? { kind: S.sel.kind, key: S.sel.key, blockId: S.sel.blockId } : null; },
    ms: function () { return S.ms; },
    notify: function (text) { pushTicker({ text: String(text), when: new Date().toISOString() }); },
    refresh: function () { if (S.sel) renderInspector(); }
  };

  api._extensions = EXT;

  return api;
});
