/* Insertion: name every file, show every byte, then write.
 *
 * The insert bar under the source editor already said what an insertion would
 * touch and then handed the request to chat. This makes it write, and the whole
 * design of the flow follows from two constraints.
 *
 *   THE PREVIEW IS THE CONFIRMATION. Nothing is written until the author has
 *   seen the exact text of every file change. A plan comes back from the server
 *   with a token; the token is what the "write it" button sends. The plan itself
 *   never travels back, so this form cannot become the field that accepts a
 *   literal number.
 *
 *   THE CARET IS CAPTURED BEFORE THE PANEL MOVES. Opening the citation form
 *   switches the inspector to References, which destroys the source textarea and
 *   the selection inside it. So the caret and the exact text it was measured
 *   against are read in the CAPTURE phase of the click, before the viewer's own
 *   handler runs. Without that, every insertion lands at the end of the block.
 *
 * The overlay exists because `ctx` offers no way to draw inside the inspector
 * body; a card added from an extension would have to reach into the viewer's
 * DOM. Three or four file diffs also want the room.
 */
(function () {
  'use strict';
  if (typeof MSViewer === 'undefined') return;

  var KINDS = {
    'insert:cite': 'citation',
    'insert:value': 'value',
    'insert:exhibit': 'exhibit'
  };

  var TITLES = {
    citation: ['Insert a citation',
      'Your bibliography is searched first: a source the paper already cites goes in with no lookup and no gate, '
      + 'named by its cite key, its DOI, an author and year, or part of its title. Anything new is looked up in '
      + 'your library, then Crossref and OpenAlex, and is not inserted if the identity checks fail.'],
    value: ['Insert a number',
      'There is no field for a value. Name the quantity and the expression that computes it, and the code that writes it is written for you.'],
    exhibit: ['Insert an exhibit',
      'A float after this paragraph, the code that builds it, and the runfile line without which it goes stale on the next rebuild.']
  };

  /* Where the caret was, and the text it was measured against. Both are needed:
     an offset into a draft that has not reached disk points at the wrong place
     in the file, and the server refuses rather than guessing. */
  var caret = null;
  var pendingKind = null;
  /* The citation the author clicked, when the form was opened from one. A
     named citation is a target that needs no caret and cannot be measured
     wrong, which is the whole point of offering it. */
  var pendingBeside = null;
  var host = null;

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function srcBox() {
    return document.querySelector('textarea.src[data-role="src"]');
  }

  /* A textarea keeps its selection after it loses focus, so the offset read at
     button-click time is right even though the button has taken the focus by
     then. What cannot be read that way is whether the author ever put a caret
     there at all: an untouched box reports 0, which is indistinguishable from a
     caret placed deliberately at the start, and inserting on that reading
     silently prepends to the paragraph.

     So "was it touched" is remembered from interactions inside the box, and the
     OFFSET is re-read live. Testing `document.activeElement` at click time
     instead looks equivalent and is not: focus moves on mousedown, so it is
     already the button, and every insertion lands at the end of the block. That
     is what it did until a browser run showed the citation in the wrong place. */
  var touched = {};

  function remember(interactive) {
    var el = srcBox();
    if (!el) return;
    var id = el.getAttribute('data-block');
    if (interactive) touched[id] = true;
    caret = { block: id, at: el.selectionStart, base: el.value, known: !!touched[id] };
  }

  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('[data-open],[data-act]') : null;
    if (!t) { if (e.target && e.target.matches && e.target.matches('textarea.src')) remember(true); return; }
    var key = t.getAttribute('data-open') || t.getAttribute('data-act') || '';
    if (key.indexOf('insert:') !== 0 && key !== 'ins:go') return;
    remember(false);
    /* Which form is open is the viewer's state, not ours, and reading it back
       off rendered headings would be a second copy of it that drifts. The click
       that opens the form is the one moment it is unambiguous, so it is read
       here and nowhere else. */
    if (key.indexOf('insert:cite:beside:') === 0) {
      pendingKind = 'citation';
      pendingBeside = key.slice('insert:cite:beside:'.length);
    } else if (key.indexOf('insert:') === 0) {
      pendingKind = KINDS[key] || key.split(':')[1];
      pendingBeside = null;
    }
  }, true);
  document.addEventListener('keyup', function (e) {
    if (e.target && e.target.matches && e.target.matches('textarea.src')) remember(true);
  }, true);
  document.addEventListener('select', function (e) {
    if (e.target && e.target.matches && e.target.matches('textarea.src')) remember(true);
  }, true);

  // ------------------------------------------------------------------ the wire

  function post(body) {
    return fetch('/insert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  // ----------------------------------------------------------------- the shell

  function styles() {
    if (document.getElementById('ms-insert-css')) return;
    var s = document.createElement('style');
    s.id = 'ms-insert-css';
    s.textContent = [
      '.msins-wrap{position:fixed;inset:0;z-index:60;display:flex;align-items:center;',
      'justify-content:center;background:rgba(0,0,0,.42);padding:2rem 1rem;}',
      '.msins{background:var(--surface);color:var(--ink);border:1px solid var(--rule);',
      'border-radius:8px;width:min(760px,100%);max-height:100%;display:flex;flex-direction:column;',
      'box-shadow:0 18px 60px rgba(0,0,0,.28);font-family:var(--ui);}',
      '.msins header{padding:.8rem 1rem;border-bottom:1px solid var(--rule);background:var(--sunken);',
      'border-radius:8px 8px 0 0;}',
      '.msins header b{display:block;font-size:.95rem;}',
      '.msins header span{font-size:.74rem;color:var(--muted);line-height:1.5;display:block;margin-top:.2rem;}',
      '.msins .body{padding:.9rem 1rem;overflow:auto;display:flex;flex-direction:column;gap:.7rem;}',
      '.msins footer{padding:.7rem 1rem;border-top:1px solid var(--rule);display:flex;gap:.45rem;',
      'align-items:center;flex-wrap:wrap;}',
      '.msins label{display:block;font-size:.7rem;color:var(--muted);margin-bottom:.2rem;}',
      '.msins input,.msins select,.msins textarea{width:100%;font:inherit;font-size:.8rem;',
      'padding:.4rem .55rem;border:1px solid var(--rule);border-radius:5px;background:var(--surface);',
      'color:var(--ink);}',
      '.msins textarea{font-family:var(--mono);min-height:3.2rem;resize:vertical;}',
      '.msins .grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;}',
      '.msins .note{font-size:.72rem;color:var(--muted);margin:0;line-height:1.5;}',
      '.msins .chk{display:flex;gap:.5rem;align-items:baseline;font-size:.75rem;padding:.28rem 0;}',
      '.msins .chk b{font-family:var(--mono);font-size:.68rem;min-width:5.4rem;}',
      '.msins .chk span{color:var(--muted);}',
      '.msins .yes{color:var(--verbatim);} .msins .no{color:var(--missing);}',
      '.msins .adv{color:var(--paraphrase);}',
      '.msins .w{border:1px solid var(--rule);border-radius:6px;overflow:hidden;}',
      '.msins .w h4{margin:0;padding:.4rem .6rem;background:var(--sunken);font-size:.72rem;',
      'font-weight:600;display:flex;justify-content:space-between;gap:.6rem;}',
      '.msins .w h4 em{font-style:normal;color:var(--muted);font-family:var(--mono);font-size:.66rem;}',
      '.msins pre{margin:0;padding:.55rem .6rem;font-family:var(--mono);font-size:.7rem;',
      'line-height:1.5;white-space:pre-wrap;word-break:break-word;overflow-x:auto;}',
      '.msins .blocked{border:1px solid var(--missing);border-radius:6px;padding:.55rem .6rem;',
      'font-size:.76rem;line-height:1.5;}',
      '.msins .done{border:1px solid var(--verbatim);border-radius:6px;padding:.55rem .6rem;font-size:.76rem;}'
    ].join('');
    document.head.appendChild(s);
  }

  function close() {
    if (host && host.parentNode) host.parentNode.removeChild(host);
    host = null;
  }

  function open(kind, ctx, seed) {
    styles();
    close();
    host = document.createElement('div');
    host.className = 'msins-wrap';
    host.innerHTML = '<div class="msins" role="dialog" aria-modal="true"><header><b>' +
      esc(TITLES[kind][0]) + '</b><span>' + esc(TITLES[kind][1]) + '</span></header>' +
      '<div class="body" data-role="msins-body"></div>' +
      '<footer data-role="msins-foot"></footer></div>';
    host.addEventListener('click', function (e) { if (e.target === host) close(); });
    document.addEventListener('keydown', onEsc);
    document.body.appendChild(host);
    form(kind, ctx, seed);
  }

  function onEsc(e) { if (e.key === 'Escape' && host) { close(); document.removeEventListener('keydown', onEsc); } }

  function body() { return host.querySelector('[data-role="msins-body"]'); }
  function foot() { return host.querySelector('[data-role="msins-foot"]'); }
  function val(name) {
    var el = host.querySelector('[name="' + name + '"]');
    return el ? String(el.value).trim() : '';
  }

  // ------------------------------------------------------------------ the form

  function form(kind, ctx, seed) {
    var b = ctx.block(caret && caret.block);
    var where = b ? (b.parent_heading || b.file || 'this paragraph') : 'this paragraph';
    var html = '<p class="note">Into <b>' + esc(where) + '</b>' +
      (kind === 'exhibit' ? ', as a new float after it.'
        : (kind === 'citation' && pendingBeside)
          ? ', beside <b>' + esc(pendingBeside) + '</b>. No cursor needed.'
          : ', at your cursor.') + '</p>';

    if (kind === 'citation') {
      html += field('query', 'A title, an author and year, or a DOI',
        seed || '', 'persistence of provider behaviour after subsidy withdrawal');
      html += '<p class="note">Three checks have to pass before anything is written: a canonical ' +
        'DOI, Crossref and OpenAlex agreeing on what it is, and a record in your Zotero library. ' +
        'A key that fails any of them is not inserted, and you are told which one failed.</p>';
    } else if (kind === 'value') {
      html += '<div class="grid">' + field('key', 'Fragment name', slug(seed), 'statin_control_mean') +
        scriptField() + '</div>';
      html += field('description', 'What this number is', seed || '',
        'the control-group mean of statin prescribing in the year before enrolment');
      html += area('expression', 'The expression that computes it, in the script’s own language',
        '', 'round(mean(dta[treat == 0]$statin), 3)');
      html += '<p class="note">There is deliberately no box for a value. The number will exist ' +
        'only once you run the script; until then the manuscript shows <b>??</b> where it goes, ' +
        'which is honest about what has not been computed yet.</p>';
    } else {
      html += '<div class="grid">' + field('key', 'Exhibit name', slug(seed), 'table2_cross_sex') +
        scriptField() + '</div>';
      html += '<div class="grid">' + field('caption', 'Caption', seed || '',
        'Effects on utilisation, by patient sex') +
        select('float', 'Float', [['table', 'table'], ['figure', 'figure']]) + '</div>';
      html += area('expression', 'The expression that writes the exhibit body', '', 'tex_output');
    }

    body().innerHTML = html;
    foot().innerHTML = '<button class="btn pri" data-role="check">Show me what it will write</button>' +
      '<button class="btn" data-role="cancel">Cancel</button>' +
      '<span class="note" data-role="say"></span>';
    foot().querySelector('[data-role="cancel"]').onclick = close;
    foot().querySelector('[data-role="check"]').onclick = function () { check(kind, ctx); };
    var first = body().querySelector('input,textarea');
    if (first) first.focus();

    if (kind !== 'citation') loadScripts(ctx);
  }

  function field(name, label, value, placeholder) {
    return '<div><label for="msins-' + name + '">' + esc(label) + '</label>' +
      '<input id="msins-' + name + '" name="' + name + '" value="' + esc(value) +
      '" placeholder="' + esc(placeholder) + '"></div>';
  }

  function area(name, label, value, placeholder) {
    return '<div><label for="msins-' + name + '">' + esc(label) + '</label>' +
      '<textarea id="msins-' + name + '" name="' + name + '" placeholder="' + esc(placeholder) +
      '">' + esc(value) + '</textarea></div>';
  }

  function select(name, label, options) {
    return '<div><label for="msins-' + name + '">' + esc(label) + '</label>' +
      '<select id="msins-' + name + '" name="' + name + '">' +
      options.map(function (o) {
        return '<option value="' + esc(o[0]) + '">' + esc(o[1]) + '</option>';
      }).join('') + '</select></div>';
  }

  function scriptField() {
    return '<div><label for="msins-script">The script that will compute it</label>' +
      '<select id="msins-script" name="script"><option value="">loading…</option></select></div>';
  }

  function slug(text) {
    return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '').slice(0, 40);
  }

  function loadScripts(ctx) {
    post({ stage: 'context', block: caret && caret.block }).then(function (out) {
      var sel = host && host.querySelector('[name="script"]');
      if (!sel) return;
      var usable = (out.scripts || []).filter(function (s) { return s.recipe; });
      if (!usable.length) {
        sel.innerHTML = '<option value="">no analysis scripts found</option>';
        return;
      }
      /* Ranked by the server: a script that already writes something this
         paragraph reads comes first, because that is nearly always the one. */
      sel.innerHTML = usable.map(function (s) {
        return '<option value="' + esc(s.path) + '">' + esc(s.name) +
          (s.outputs.length ? ' — writes ' + esc(s.outputs.slice(0, 2).join(', ')) : '') +
          '</option>';
      }).join('');
    /* Never silent. A swallowed rejection here left the box reading "loading…"
       for ever with nothing anywhere saying why, and the cause was a one-word
       typo. A control that fails must say so where the reader is looking. */
    }).catch(function (err) {
      var sel = host && host.querySelector('[name="script"]');
      if (sel) sel.innerHTML = '<option value="">could not list scripts: ' + esc(err) + '</option>';
      say('could not list your analysis scripts: ' + err);
    });
  }

  // ----------------------------------------------------------------- the plan

  /* The block to write into, and the caret inside it.
   *
   * A caret is only trustworthy when it belongs to the paragraph that is
   * actually open and the box it was measured in had focus. A textarea that was
   * never focused reports 0, which is indistinguishable from a caret placed
   * deliberately at the start, and inserting on that reading silently prepends
   * to the paragraph. With no caret of its own, the end of the block is the only
   * honest guess. */
  function target(ctx) {
    var sel = ctx.selection();
    var id = sel && sel.blockId;
    if (caret && caret.block && (!id || caret.block === id)) return caret;
    if (!id) return null;
    var b = ctx.block(id);
    if (!b) return null;
    return { block: id, at: String(b.source || '').length, base: String(b.source || ''), known: false };
  }

  function check(kind, ctx) {
    var at = target(ctx);
    if (!at) {
      say('Open a paragraph first: an insertion needs somewhere to land.');
      return;
    }
    caret = at;
    var payload = {
      stage: 'plan', kind: kind, block: at.block,
      caret: at.known ? at.at : at.base.length,
      base: at.base
    };
    if (kind === 'citation') {
      payload.query = val('query');
      if (pendingBeside) payload.beside = pendingBeside;
    }
    else {
      payload.key = val('key');
      payload.expression = val('expression');
      payload.script = val('script');
      if (kind === 'value') payload.description = val('description');
      else { payload.caption = val('caption'); payload.float = val('float'); }
    }
    say('checking…');
    post(payload).then(function (out) { render(out, kind, ctx); })
      .catch(function (err) { fallback(String(err), kind, ctx); });
  }

  function say(text) {
    var el = host && host.querySelector('[data-role="say"]');
    if (el) el.textContent = text || '';
  }

  function render(out, kind, ctx) {
    if (!host) return;
    var plan = out.plan || {};
    var html = '';

    if (plan.summary) html += '<p class="note">' + esc(plan.summary) + '</p>';

    if ((plan.checks || []).length) {
      html += '<div class="w"><h4>Checks</h4><div style="padding:.35rem .6rem">' +
        plan.checks.map(function (c) {
          var cls = c.ok ? 'yes' : (c.blocking ? 'no' : 'adv');
          return '<div class="chk"><b class="' + cls + '">' + (c.ok ? '✓' : (c.blocking ? '✗' : '!')) +
            ' ' + esc(c.name) + '</b><span>' + esc(c.detail) +
            (!c.blocking ? ' <em>(does not block)</em>' : '') + '</span></div>';
        }).join('') + '</div></div>';
    }

    if (!out.ok) {
      html += '<div class="blocked"><b>Nothing will be written.</b> ' +
        esc(plan.blocked || out.error || 'a check failed') + '</div>';
    } else {
      html += '<p class="note">' + plan.writes.length + ' file' +
        (plan.writes.length === 1 ? '' : 's') + ' will change. This is exactly what will be added.</p>';
      html += plan.writes.map(function (w) {
        return '<div class="w"><h4>' + esc(w.label) + '<em>' + esc(w.path) + '</em></h4>' +
          '<pre>' + esc(w.preview) + '</pre>' +
          (w.kind === 'runfile' && w.context ? '<pre style="border-top:1px solid var(--rule);opacity:.7">' +
            esc(w.context) + '</pre>' : '') + '</div>';
      }).join('');
      if (plan.library_add) {
        html += '<p class="note">Zotero has no record of this DOI, so one is imported first. If any ' +
          'later step fails, that record is removed again.</p>';
      }
      if (plan.rerun) {
        html += '<p class="note">Your analysis code is <b>not</b> run: it can take hours and touch data ' +
          'this process has no business opening. Run <b>' + esc(plan.rerun) + '</b> yourself and the ' +
          'value appears.</p>';
      }
    }

    body().innerHTML = html;
    foot().innerHTML = (out.ok
      ? '<button class="btn pri" data-role="write">Write ' + plan.writes.length + ' change' +
        (plan.writes.length === 1 ? '' : 's') + '</button>'
      : '') +
      '<button class="btn" data-role="back">Back</button>' +
      '<button class="btn" data-role="cancel">Cancel</button>' +
      '<span class="note" data-role="say"></span>';
    foot().querySelector('[data-role="cancel"]').onclick = close;
    foot().querySelector('[data-role="back"]').onclick = function () { form(kind, ctx, ''); };
    var go = foot().querySelector('[data-role="write"]');
    if (go) go.onclick = function () {
      go.disabled = true;
      say('writing…');
      post({ stage: 'apply', token: out.token })
        .then(function (done) { landed(done, ctx); })
        .catch(function (err) { say(String(err)); go.disabled = false; });
    };
  }

  function landed(done, ctx) {
    if (!host) return;
    if (!done.ok) {
      body().innerHTML = '<div class="blocked"><b>Nothing was written.</b> ' +
        esc(done.error || 'the write failed') +
        ' Every step before the failure has been rolled back.</div>';
      foot().innerHTML = '<button class="btn" data-role="cancel">Close</button>';
      foot().querySelector('[data-role="cancel"]').onclick = close;
      return;
    }
    body().innerHTML = '<div class="done"><b>Written.</b><br>' +
      (done.wrote || []).map(function (w) { return esc(w); }).join('<br>') + '</div>' +
      (done.rerun ? '<p class="note">Now run <b>' + esc(done.rerun) +
        '</b> to fill the fragment in. The page redraws itself when it changes.</p>' : '');
    foot().innerHTML = '<button class="btn pri" data-role="cancel">Done</button>';
    foot().querySelector('[data-role="cancel"]').onclick = function () { close(); closeInline(); };
    ctx.notify('inserted: ' + (done.wrote || []).join('; '));
  }

  /* The server may be old, or the route may be gone. Say so and offer the thing
     that used to happen, rather than losing what was typed. */
  function fallback(reason, kind, ctx) {
    if (!host) return;
    body().innerHTML = '<div class="blocked"><b>The server did not answer.</b> ' + esc(reason) +
      '</div><p class="note">Nothing was written. You can leave this as a comment instead, ' +
      'and the drain will pick it up.</p>';
    foot().innerHTML = '<button class="btn" data-role="chat">Leave it as a comment</button>' +
      '<button class="btn" data-role="cancel">Cancel</button>';
    foot().querySelector('[data-role="cancel"]').onclick = close;
    foot().querySelector('[data-role="chat"]').onclick = function () {
      ctx.send({ type: 'chat', block: caret.block, body: 'Insert a ' + kind + ' here: ' + val('query') });
      close();
    };
  }

  // -------------------------------------------------------------- registration

  MSViewer.extend({
    name: 'insert',

    act: {
      /* Claiming this replaces the old behaviour, which routed every insertion
         to chat because the protocol only carried `edit` and an edit writes one
         block. A citation is three files.

         A new paragraph still goes to chat, and deliberately: it mints a block
         out of nothing, which is a drafting job rather than a coordinated
         write, and claiming it here would break a control that works. */
      'ins:go': function (ctx) {
        var seedBox = document.querySelector('textarea[data-role="insert"]');
        var seed = seedBox ? seedBox.value.trim() : '';
        var kind = pendingKind;
        if (kind === 'citation' || kind === 'value' || kind === 'exhibit') {
          open(kind, ctx, seed);
          return;
        }
        if (!seed) return;
        var sel = ctx.selection();
        if (!sel || !sel.blockId) return;
        ctx.send({ type: 'chat', block: sel.blockId,
                   body: 'Insert a ' + (kind || 'block') + ' here: ' + seed });
        ctx.notify('queued: insert a ' + (kind || 'block'));
        closeInline();
      }
    },

    frame: {},

    tab: function () { return null; }
  });

  /* Closing the inline form is the viewer's business, so it is asked to do it
     through the control it already owns rather than by reaching into S. */
  function closeInline() {
    var x = document.querySelector('[data-act="ins:close"]');
    if (x) x.click();
  }

})();
