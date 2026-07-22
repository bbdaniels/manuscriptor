/* Computed values: what the number IS, not only who wrote it.
 *
 * The References row and the value provenance panel both read `description`,
 * which the server now derives from the code that writes each fragment. This
 * extension adds the rest of the record, which does not fit on one line and is
 * the part the author actually needs when a referee asks where 0.096 came from:
 *
 *   * the estimate ROW, not the single number. A p-value is read beside its
 *     coefficient, its standard error, its N and its clusters, and the script
 *     that wrote one wrote all five in the same loop, so they are on disk.
 *   * the MODEL: the formula, what it clusters on, what it absorbs.
 *   * the HISTORY: what the value was last week and which commit changed it.
 *     A fragment file holds only what the value is now.
 *   * a way to CORRECT a description that is wrong, which goes through the
 *     comment log like every other request, because the page never writes files.
 *
 * The manifest is fetched rather than carried in the page: it is derived from
 * the analysis code, not from the manuscript, so it does not belong in a blob
 * that is rebuilt on every keystroke pause. It is served out of the build
 * directory, and a page that cannot reach it (a static export opened from
 * disk) still shows every description, because those ride in the block record.
 */
(function () {
  'use strict';

  if (typeof MSViewer === 'undefined' || !MSViewer.extend) return;

  var MANIFEST = null;      // key -> the derived record
  var STATE = 'loading';    // loading | ready | absent

  /* The built-in value panel already looks for `ms().values[key]`, and nothing
     has ever filled it. Filling it is what puts the producing code under a
     violet number in the page, with no change to the viewer. */
  function adopt(ctx, data) {
    var ms = ctx.ms();
    if (!ms) return;
    ms.values = ms.values || {};
    for (var key in data) {
      if (Object.prototype.hasOwnProperty.call(data, key)) ms.values[key] = data[key];
    }
  }

  function load(ctx) {
    if (typeof fetch !== 'function') { STATE = 'absent'; return; }
    fetch('values.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (doc) {
        MANIFEST = (doc && doc.values) || null;
        STATE = MANIFEST ? 'ready' : 'absent';
        if (MANIFEST) { adopt(ctx, MANIFEST); ctx.refresh(); }
      })
      .catch(function () { STATE = 'absent'; });
  }

  function record(key) {
    return (MANIFEST && MANIFEST[key]) || null;
  }

  // ------------------------------------------------------------- rendering

  function line(ctx, dt, dd, mono) {
    if (!dd) return '';
    return '<dt>' + ctx.escape(dt) + '</dt><dd' + (mono ? '' : ' style="font-family:inherit"') +
      '>' + ctx.escape(dd) + '</dd>';
  }

  function estimateRow(ctx, rec) {
    var rows = [];
    if (rec.value) rows.push({ statistic: rec.statistic || 'this value', value: rec.value, key: rec.key });
    rows = rows.concat(rec.siblings || []);
    if (rows.length < 2) return '';
    var html = '<dl class="stat">';
    for (var i = 0; i < rows.length; i++) {
      html += '<dt>' + ctx.escape(rows[i].statistic) + '</dt><dd>' + ctx.escape(rows[i].value) +
        '<span style="opacity:.55"> · ' + ctx.escape(rows[i].key) + '</span></dd>';
    }
    return ctx.card('The estimate this belongs to', String(rows.length), html + '</dl>' +
      '<p class="meta" style="margin-top:.5rem">Read off the sibling fragments the same loop wrote. ' +
      'These are the files on disk, not a recomputation.</p>');
  }

  function modelCard(ctx, rec) {
    var m = rec.model;
    if (!m) {
      return ctx.card('The model', '', '<p class="meta">The script fits more than one specification and ' +
        'nothing in it ties this fragment to a particular one, so no equation is claimed here.</p>');
    }
    return ctx.card('The model', m.call || '',
      '<dl class="stat">' +
      line(ctx, 'Equation', m.formula, true) +
      line(ctx, 'Clustered on', m.cluster, true) +
      line(ctx, 'Fixed effects', (m.fe || []).join(', '), true) +
      '</dl>');
  }

  function historyCard(ctx, rec) {
    var hist = rec.history || [];
    if (!hist.length) {
      return ctx.card('What it was before', '',
        '<p class="meta">' + ctx.escape(rec.history_note ||
          'No earlier value is recorded for this fragment.') + '</p>');
    }
    var html = '';
    for (var i = 0; i < hist.length; i++) {
      html += '<div class="hit" style="cursor:default"><b>' + ctx.escape(hist[i].value) + '</b>' +
        '<span>' + ctx.escape(hist[i].when) + (hist[i].why ? ' · ' + ctx.escape(hist[i].why) : '') +
        '</span></div>';
    }
    return ctx.card('What it was before', String(hist.length), html +
      '<p class="meta" style="margin-top:.5rem">Read out of the commits that touched this fragment. ' +
      'The newest line is what the manuscript is printing now.</p>');
  }

  function correction(ctx, rec) {
    var snippet = '{\n  "values": {\n    "' + rec.key + '": "…what it really is…"\n  }\n}';
    return ctx.card('If this is wrong', '',
      '<p class="meta">The description is derived from the code, and the code does not always say ' +
      'enough. A correction goes in <b>values.json</b> beside the manuscript, which nothing here ' +
      'ever writes, so it survives every rebuild.</p>' +
      '<pre style="margin-top:.5rem">' + ctx.escape(snippet) + '</pre>' +
      '<div class="row" style="margin-top:.6rem">' +
      '<button class="btn" type="button" data-values-ask="' + ctx.escape(rec.key) +
      '">Ask for it to be corrected</button></div>');
  }

  function one(ctx, v) {
    var rec = record(v.key);
    var head = '<div class="hit" style="cursor:default"><b>' + ctx.escape(v.key) +
      (rec && rec.value ? ' = ' + ctx.escape(rec.value) : '') + '</b>' +
      '<span>' + ctx.escape(v.description || (rec && rec.description) ||
        'No description could be derived for this fragment.') + '</span>' +
      '<span class="ok">' + ctx.escape(
        (rec && rec.producer)
          ? rec.producer + (rec.producer_line ? ':' + rec.producer_line : '')
          : (v.producer || (rec && rec.reason) || 'producer unknown')) + '</span></div>';

    if (!rec) {
      return ctx.card(v.key, '', head +
        '<p class="meta" style="margin-top:.5rem">' + ctx.escape(
          STATE === 'ready'
            ? 'This fragment is not in the manifest, so nothing more is known about it.'
            : 'The manifest has not loaded, so only the description carried in the page is shown.') +
        '</p>');
    }

    var facts = '<dl class="stat">' +
      line(ctx, 'Fragment', rec.path, true) +
      line(ctx, 'Statistic', rec.statistic) +
      line(ctx, 'Of', rec.subject) +
      line(ctx, 'Family', rec.family) +
      line(ctx, 'Units', rec.units) +
      line(ctx, 'Derived from', rec.base) +
      line(ctx, 'Source', rec.source === 'hand' ? 'your own correction' :
        rec.source === 'derived' ? 'derived from the producing code' : 'the producer only') +
      '</dl>' +
      (rec.reason ? '<p class="meta" style="margin-top:.5rem">' + ctx.escape(rec.reason) + '</p>' : '');

    return ctx.card(v.key, rec.statistic || '', head + facts) +
      estimateRow(ctx, rec) +
      modelCard(ctx, rec) +
      historyCard(ctx, rec) +
      (rec.code ? ctx.card('The code that writes it', rec.lines || '',
        '<pre>' + ctx.escape(rec.code) + '</pre>', true) : '') +
      correction(ctx, rec);
  }

  function body(ctx, block) {
    var values = (block && block.values) || [];
    if (!values.length) return '<p class="empty">No computed values in this block.</p>';
    var html = '';
    for (var i = 0; i < values.length; i++) html += one(ctx, values[i]);
    return html +
      '<div class="row" style="margin-top:.6rem"><button class="btn" type="button" ' +
      'data-open="values">Every computed value in the paper</button></div>';
  }

  // ------------------------------------------------------------- the hooks

  MSViewer.extend({
    name: 'values',

    tab: function (blockId, block, ctx) {
      // Late arrivals: a page that opened before the fetch landed, or a fetch
      // that failed while the server was mid-rebuild.
      if (MANIFEST === null && STATE === 'loading') load(ctx);
      var values = (block && block.values) || [];
      if (!values.length) return null;
      return { name: 'Values', n: values.length, body: body(ctx, block) };
    },

    open: {
      // Every described value in the manuscript, in one list.
      'values': function (ctx) {
        var ms = ctx.ms() || {};
        var all = ms.values || {};
        var keys = Object.keys(all).sort();
        var described = 0, html = '';
        for (var i = 0; i < keys.length; i++) {
          var rec = all[keys[i]];
          if (rec.description) described++;
          html += '<button class="hit" type="button" data-open="value:' + ctx.escape(keys[i]) + '">' +
            '<b>' + ctx.escape(keys[i]) + (rec.value ? ' = ' + ctx.escape(rec.value) : '') + '</b>' +
            '<span>' + ctx.escape(rec.description || 'No description could be derived.') + '</span>' +
            '<span class="' + (rec.description ? 'ok' : 'warn') + '">' +
            ctx.escape(rec.producer || 'producer unknown') + '</span></button>';
        }
        return {
          eyebrow: 'Provenance', chip: ['c', 'computed'],
          title: 'Computed values',
          sub: described + ' of ' + keys.length + ' described from the code that writes them',
          tabs: [{ name: 'All', n: keys.length, body: html || '<p class="empty">No manifest loaded.</p>' }]
        };
      }
    },

    /* `act` is registered so the contract is used where it fits, and the
       correction button does NOT go through it: an act handler is handed `ctx`
       and not the element that was clicked, so a button carrying a value key
       has nowhere to put the key. That is a gap in the contract rather than
       something to work around quietly, and the note is in the report. */
    act: {
      'values:reload': function (ctx) { STATE = 'loading'; load(ctx); }
    }
  });

  /* The page never writes a file. A correction is a request, and it goes down
     the one channel that exists for requests: the chat on the block the value
     is reported in, which the drain reads. */
  function ask(ctx, key) {
    var sel = ctx.selection();
    if (!sel || !sel.blockId) { ctx.notify('Open a paragraph first'); return false; }
    var sent = ctx.send({
      type: 'chat',
      block: sel.blockId,
      body: 'The description for the computed value ' + key + ' is wrong. Read the code that ' +
            'writes it, then put the right one-line description in values.json beside the ' +
            'manuscript, under "values". Do not touch the fragment itself.'
    });
    ctx.notify('asked for ' + key + ' to be described');
    return sent !== false;
  }

  /* Fetched at load rather than on the first block that has a value: the
     whole-paper list and the value panel both want it, and neither goes
     through a block. Under node there is no fetch and this settles to
     `absent`, which is the same state a static export lands in. */
  if (MSViewer.ext) load(MSViewer.ext);

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest && e.target.closest('[data-values-ask]');
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      ask(MSViewer.ext, btn.getAttribute('data-values-ask'));
    }, true);
  }

  // Exported for tests, which run this file under node with no document.
  if (typeof module === 'object' && module.exports) {
    module.exports = {
      _adopt: adopt,
      _one: one,
      _body: body,
      _ask: ask,
      _set: function (data, state) { MANIFEST = data; STATE = state || 'ready'; },
      _state: function () { return STATE; }
    };
  }
})();
