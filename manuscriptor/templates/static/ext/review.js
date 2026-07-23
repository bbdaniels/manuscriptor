/* Import comments: a referee's marked-up PDF, a coauthor's tracked changes.

   The button existed and did nothing, which is the half of the author's problem
   that stayed open: he could comment TO the tool and a referee still could not
   comment IN.

   Two surfaces, and the second one is the important one.

   READ A FILE takes a .pdf or a .docx and reports what happened to it: how many
   marks anchored, how many are waiting, and from which file. Nothing here
   decides where anything goes -- the server matches on the text that was
   marked, never on the page number, so the anchors survive the rewrite that
   happens between a referee reading a draft and the author acting on it.

   THE TRAY is where anything that could not be placed confidently waits. This
   is not a failure list, it is the point: a referee comment attached to the
   wrong sentence is worse than one the author has to place by hand, because he
   will read it as being about that sentence. So each waiting mark says why it
   is waiting, quotes what was marked, and offers the paragraphs it nearly
   matched with a button on each. Placing one is a click, not a hunt.

   A placed mark becomes an ordinary comment in `comments.jsonl`, attributed to
   whoever wrote it, so it queues and drains exactly like one of his own. This
   extension never learns that; it posts to `/import` and the margin fills in
   through the same `chat` and `queue` frames a typed comment produces. */

(function () {
  'use strict';
  if (typeof MSViewer === 'undefined' || !MSViewer.extend) return;

  var S = {
    tray: [],          // marks waiting to be placed
    marks: {},         // block id -> the markup anchored on it
    report: null,      // what the last read produced
    error: '',
    reading: '',       // filename, while a read is in flight
    loaded: false,
    stale: false,      // the log or the blocks moved; re-read before showing
    busy: false,
    ctx: null
  };

  /* This extension's own markup, styled by this extension. `styles.css` is
     shared with three other features and with both skins, so a handful of
     classes only these panels use have no business in it. Every colour is a
     token the skins already define, or the panel would stop belonging to its
     page the moment the hue changed. */
  (function styles() {
    var css =
      '.mark-q{margin:.45rem 0;padding:.4rem .6rem;border-left:2px solid var(--accent);' +
      'background:var(--sunken);font-family:var(--serif);' +
      'font-size:.86rem;line-height:1.45;opacity:.92}' +
      '.mark-note{margin:.35rem 0 0;font-size:.86rem;line-height:1.45}' +
      '.mark-row{padding:.18rem 0;font-size:.82rem}' +
      '.mark-actions{display:flex;flex-direction:column;gap:.3rem;margin-top:.6rem}' +
      '.mark-actions .btn{text-align:left;justify-content:flex-start}' +
      '.mark-actions .btn .meta{margin-left:.4rem;opacity:.7}';
    var el = document.createElement('style');
    el.setAttribute('data-ext', 'review');
    el.textContent = css;
    (document.head || document.documentElement).appendChild(el);
  })();

  var KIND = {
    highlight: 'highlighted', strikeout: 'struck out', underline: 'underlined',
    squiggly: 'marked', insertion: 'inserted', deletion: 'deleted',
    note: 'left a note', comment: 'commented'
  };

  function esc(s) { return S.ctx ? S.ctx.escape(String(s == null ? '' : s)) : String(s); }
  function n(x) { return Number(x || 0); }

  /* ------------------------------------------------------------- the server */

  function post(payload, done) {
    var opts = (payload instanceof FormData)
      ? { method: 'POST', body: payload }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload) };
    fetch('/import', opts).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, body: j }; },
                           function () { return { ok: false, body: { error: 'the server said nothing' } }; });
    }).then(done, function () {
      done({ ok: false, body: { error: 'could not reach the server' } });
    });
  }

  function absorb(body) {
    if (body.tray) S.tray = body.tray;
    if (body.marks) S.marks = body.marks;
    S.loaded = true;
    S.stale = false;
  }

  /* Fetched on load and re-read when the log or the blocks move. Called from
     render, so it has to be idempotent: a fetch that triggered a render that
     triggered a fetch would spin the page for as long as the panel was open.
     `ctx` is optional, because the first read happens before anything is open:
     the count on the toolbar is the only reason the tray gets emptied, and a
     count that appears once you go looking is not a count. */
  function load(ctx) {
    if (S.busy) return;
    S.busy = true;
    S.stale = false;
    post({ action: 'state' }, function (res) {
      S.busy = false;
      if (res.ok) { absorb(res.body); paintButton(); if (ctx) ctx.refresh(); }
      else { S.loaded = true; S.error = res.body.error || ''; }
    });
  }

  function fresh(ctx) { if (!S.loaded || S.stale) load(ctx); }

  function upload(file, ctx) {
    if (S.busy) return;
    S.busy = true;
    S.error = '';
    S.reading = file.name;
    ctx.refresh();
    var fd = new FormData();
    fd.append('file', file, file.name);
    post(fd, function (res) {
      S.busy = false;
      S.reading = '';
      if (!res.ok) {
        S.error = res.body.error || 'that file could not be read';
      } else {
        S.report = res.body;
        S.error = '';
        absorb(res.body);
        ctx.notify(n(res.body.anchored) + ' anchored, ' + n(res.body.unplaced) +
                   ' waiting · ' + res.body.file);
      }
      paintButton();
      ctx.refresh();
    });
  }

  function place(importId, blockId, ctx) {
    if (S.busy) return;
    S.busy = true;
    post({ action: 'place', 'import': importId, block: blockId }, function (res) {
      S.busy = false;
      if (!res.ok) { S.error = res.body.error || 'that could not be placed'; }
      else {
        S.error = '';
        absorb(res.body);
        var b = ctx.block(blockId);
        ctx.notify('placed ' + (res.body.author || 'a comment') + ' on ' +
                   ((b && b.parent_heading) || (b && b.file) || 'the paragraph'));
      }
      paintButton();
      ctx.refresh();
    });
  }

  /* ------------------------------------------------------------- the toolbar
     A tray nobody opened is a tray nobody empties, so the count rides on the
     button that opens it. Text only: the button belongs to the page, and an
     extension repainting someone else's markup should leave as small a mark as
     it can. */

  function paintButton() {
    var el = document.querySelector('[data-open="import:reviewer"]');
    if (!el) return;
    var base = 'Import comments…';
    el.textContent = S.tray.length ? base + '  ' + S.tray.length : base;
    el.setAttribute('title', S.tray.length
      ? S.tray.length + ' imported ' + (S.tray.length === 1 ? 'mark is' : 'marks are') +
        ' waiting to be placed'
      : 'Read a marked-up PDF or a .docx of tracked changes into the margin');
  }

  /* ---------------------------------------------------------------- the view */

  function quote(text) {
    if (!text) return '';
    return '<blockquote class="mark-q">' + esc(text) + '</blockquote>';
  }

  function whereLine(item) {
    var bits = [];
    if (item.author) bits.push(item.author);
    if (item.kind && KIND[item.kind]) bits.push(KIND[item.kind]);
    if (item.where) bits.push(item.where);
    if (item.source) bits.push(item.source);
    return bits.join(' · ');
  }

  function readCard() {
    var body =
      '<p class="meta">A referee\'s marked-up PDF, or a coauthor\'s .docx with tracked ' +
      'changes and comments. Highlights, strikeouts, sticky notes, insertions and ' +
      'deletions all come in.</p>' +
      '<div class="row" style="margin-top:.6rem" data-review-drop>' +
      '<button class="btn pri" data-act="review:pick"' + (S.busy ? ' disabled' : '') + '>' +
      (S.reading ? 'Reading ' + esc(S.reading) + '…' : 'Choose a file…') + '</button>' +
      '<span class="hintline">or drop one here</span></div>';
    return S.ctx.card('Read a file', '.pdf · .docx', body);
  }

  function gateCard() {
    return S.ctx.card('', '', '<div class="locked"><span>⊘</span><div>' +
      'Every mark is matched back to the paragraph it sits on using the text that was ' +
      'marked, not the page number, so the anchors survive your rewrites. Anything that ' +
      'cannot be placed confidently waits in the tray rather than attaching itself to the ' +
      'wrong sentence.</div></div>');
  }

  function reportCard() {
    if (S.error) {
      return S.ctx.card('That did not read', '', '<p class="empty">' + esc(S.error) + '</p>');
    }
    if (!S.report) return '';
    var r = S.report;
    var rows = '<dl class="stat">' +
      '<dt>from</dt><dd>' + esc(r.file) + '</dd>' +
      '<dt>anchored</dt><dd>' + n(r.anchored) + ' of ' + n(r.marks) + '</dd>' +
      '<dt>in the tray</dt><dd>' + n(r.unplaced) + '</dd>' +
      (n(r.already) ? '<dt>already read in</dt><dd>' + n(r.already) + '</dd>' : '') +
      '</dl>';
    var placed = (r.items || []).filter(function (it) { return it.block; });
    if (placed.length) {
      rows += '<div class="meta" style="margin-top:.6rem">Where they landed</div>' +
        placed.map(function (it) {
          var b = S.ctx.block(it.block);
          var name = (b && b.parent_heading) || 'the manuscript';
          var line = b ? (b.file + ':' + b.line_start) : it.block;
          return '<div class="mark-row"><a href="#" data-goto="' + esc(it.block) + '">' +
            esc(name) + '</a> <span class="meta">' + esc(line) + ' · ' +
            esc(whereLine(it)) + '</span></div>';
        }).join('');
    }
    return S.ctx.card('What happened', String(n(r.marks)) + ' marks', rows);
  }

  /* One waiting mark, with somewhere to put it. The candidates are the
     paragraphs it nearly matched, re-scored against the manuscript as it is
     now rather than as it was when the file was read. */
  function trayCard(item, ctx) {
    var sel = ctx.selection();
    var body = '<p class="meta">' + esc(item.reason || 'waiting to be placed') + '</p>' +
      quote(item.marked) +
      (item.note ? '<p class="mark-note">' + esc(item.note) + '</p>' : '');

    var buttons = (item.candidates || []).map(function (c) {
      var b = ctx.block(c.block);
      if (!b) return '';
      var name = b.parent_heading || 'the paragraph';
      return button(item.id, c.block,
        'Place on ' + name, b.file + ':' + b.line_start + ' · ' + Math.round(c.score * 100) + '%');
    }).filter(Boolean);

    var open = sel && sel.blockId ? ctx.block(sel.blockId) : null;
    var already = (item.candidates || []).some(function (c) { return sel && c.block === sel.blockId; });
    if (open && !already) {
      buttons.push(button(item.id, sel.blockId, 'Place on the paragraph I have open',
        open.file + ':' + open.line_start));
    }

    body += buttons.length
      ? '<div class="mark-actions">' + buttons.join('') + '</div>'
      : '<p class="empty">Nothing here resembled it. Select a paragraph in the ' +
        'manuscript and come back, and it can go there.</p>';

    return ctx.card(item.author || 'a reviewer', item.where || '', body);
  }

  /* An act handler is given the context and nothing else, so a button that
     needs arguments has to carry them in its NAME and register a handler under
     it. Regenerated on every render and swept first, so the map cannot grow
     without bound as the tray is worked through. */
  function button(importId, blockId, label, sub) {
    var act = 'review:place:' + importId + ':' + blockId;
    EXT.act[act] = function (ctx) { place(importId, blockId, ctx); };
    return '<button class="btn" data-act="' + esc(act) + '"' + (S.busy ? ' disabled' : '') +
      '>' + esc(label) + (sub ? ' <span class="meta">' + esc(sub) + '</span>' : '') + '</button>';
  }

  function sweep() {
    for (var k in EXT.act) {
      if (k.indexOf('review:place:') === 0) delete EXT.act[k];
    }
  }

  function view(ctx) {
    S.ctx = ctx;
    fresh(ctx);
    sweep();

    var trayBody = S.tray.length
      ? S.tray.map(function (it) { return trayCard(it, ctx); }).join('')
      : '<p class="empty">Nothing waiting. Marks that could not be placed ' +
        'confidently land here instead of on a guessed paragraph.</p>';

    var sub = S.report
      ? S.report.file + ' · ' + n(S.report.anchored) + ' anchored · ' + n(S.report.unplaced) + ' waiting'
      : 'a referee PDF, or a coauthor\'s tracked changes';

    return {
      eyebrow: 'Import comments',
      chip: S.tray.length ? ['c', S.tray.length + ' waiting'] : null,
      title: 'Bring outside markup in',
      sub: sub,
      tabs: [
        { name: 'Read a file', body: readCard() + reportCard() + gateCard() },
        { name: 'Tray', n: S.tray.length, body: trayBody }
      ]
    };
  }

  /* The paragraph's own markup. The comment already shows in the margin, but it
     carries the referee's WORDS, not what he marked, and the two are different
     things: "this overstates it" is unreadable without the sentence under it. */
  function blockTab(blockId, block, ctx) {
    S.ctx = ctx;
    if (!S.loaded || S.stale) { load(ctx); return null; }
    var mine = S.marks[blockId] || [];
    if (!mine.length) return null;
    return {
      name: 'Markup',
      n: mine.length,
      body: mine.map(function (m) {
        return ctx.card(m.author || 'a reviewer', m.where || '',
          '<p class="meta">' + esc(whereLine(m)) + '</p>' + quote(m.marked) +
          (m.note ? '<p class="mark-note">' + esc(m.note) + '</p>' : ''));
      }).join('')
    };
  }

  /* ---------------------------------------------------------------- wiring */

  function pick(ctx) {
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = '.pdf,.docx';
    input.addEventListener('change', function () {
      var f = input.files && input.files[0];
      if (f) upload(f, ctx);
    });
    input.click();
  }

  /* Dropping a referee report on the panel is the natural gesture, and the
     extension contract has no hook for wiring a rendered element. A listener on
     the document costs nothing and does not reach into the viewer: it acts only
     when the drop landed inside a zone this extension drew. */
  function dropzone(e) { return e.target && e.target.closest && e.target.closest('[data-review-drop]'); }

  document.addEventListener('dragover', function (e) {
    if (dropzone(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
  });
  document.addEventListener('drop', function (e) {
    if (!dropzone(e)) return;
    e.preventDefault();
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f && S.ctx) upload(f, S.ctx);
  });

  var EXT = {
    name: 'review',
    act: {
      'review:pick': pick,
      'review:refresh': function (ctx) { S.loaded = false; load(ctx); }
    },
    open: { 'import:reviewer': view },
    /* Two different reasons to go stale, and they want different urgency.
       A `queue` frame means the log moved -- an import landed from a session or
       from another window on the same port -- and the toolbar count has to move
       with it, so that one re-reads at once. A `patch` means an edit renamed
       blocks, so the marks map is keyed to ids the page no longer has; that is
       only visible once a panel is drawn, and re-scoring every waiting mark
       against every paragraph on each keystroke pause would be work nobody
       asked for. So it is marked stale and re-read when something looks. */
    frame: {
      queue: function (_msg, ctx) { load(ctx); },
      patch: function () { S.stale = true; }
    },
    tab: blockTab
  };

  MSViewer.extend(EXT);

  /* Read once on boot, so the count on the toolbar is right before anyone opens
     anything. Verified in a browser: without it a page loaded with two marks
     waiting showed a bare "Import comments…" until the panel was opened, which
     is the one moment the count would have been worth having. */
  function boot() { paintButton(); load(null); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
