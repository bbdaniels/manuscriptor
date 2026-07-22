/* Compile PDF and Compile Word.

   Both buttons existed and did nothing, which is worse than not offering them:
   a control that lies sits in the toolbar beside controls that work and there
   is no way to tell them apart until you have waited.

   A compile takes tens of seconds, so the whole design here is about the wait
   being visible. Three surfaces, each answering a different question:

     the button      am I compiling, and how far in    ("PDF · pass 2 of 3 12s")
     the chip        what happened last time            ("PDF ready · 31s")
     the panel       what exactly, and where is it      (steps, path, the error)

   The panel opens by itself when a compile starts, because a progress report
   nobody opened is not a progress report. It stays afterwards, so the result
   and the way to reveal it in the Finder survive being scrolled past.

   The server does the work over `POST /compile` and reports back through the
   websocket that already exists, as `{type:'compile'}` frames. */

(function () {
  'use strict';
  if (typeof MSViewer === 'undefined' || !MSViewer.extend) return;

  var S = {
    kind: null,        // 'pdf' | 'docx' while running, null when idle
    label: '',
    steps: [],
    result: null,
    started: 0,
    tick: null,
    ctx: null
  };

  var LABEL = { pdf: 'PDF', docx: 'Word' };

  function esc(s) { return (S.ctx ? S.ctx.escape(String(s == null ? '' : s)) : String(s)); }
  function secs(n) { return (Math.round(Number(n || 0) * 10) / 10) + 's'; }
  function elapsed() { return Math.max(0, Math.round((Date.now() - S.started) / 1000)); }

  /* ------------------------------------------------------------- the toolbar */

  function button(kind) {
    return document.querySelector('[data-act="compile:' + kind + '"]');
  }

  /* A place for the standing state, and the way back into the panel once it has
     been scrolled away from. Injected rather than added to the template, because
     three other agents are working in this tree and the template is not mine. */
  function chip() {
    var el = document.getElementById('ms-compile-chip');
    if (el) return el;
    var bar = document.querySelector('.toolbar');
    if (!bar) return null;
    el = document.createElement('button');
    el.id = 'ms-compile-chip';
    el.type = 'button';
    el.className = 'tb ms-compile-chip';
    el.setAttribute('data-open', 'compile');
    el.hidden = true;
    var anchor = button('docx');
    if (anchor && anchor.parentNode) anchor.parentNode.appendChild(el);
    else bar.appendChild(el);
    return el;
  }

  function paintToolbar() {
    ['pdf', 'docx'].forEach(function (k) {
      var b = button(k);
      if (!b) return;
      if (!b.getAttribute('data-label')) b.setAttribute('data-label', b.textContent);
      var base = b.getAttribute('data-label');
      if (S.kind === k) {
        var last = S.steps.length ? S.steps[S.steps.length - 1].step : 'starting';
        b.textContent = LABEL[k] + ' · ' + last + ' ' + elapsed() + 's';
        b.classList.add('is-compiling');
      } else {
        b.textContent = base;
        b.classList.remove('is-compiling');
      }
      b.disabled = !!S.kind;
    });

    // The panel is rebuilt on frames, and a LaTeX pass is nine seconds long, so
    // between frames its clock would sit still while the toolbar's ticked --
    // the one surface with room to show what is happening looking like the one
    // that had stalled. Only the number moves, which is all that has changed.
    var now = document.querySelector('.ms-compile-step.is-now .t');
    if (now) now.textContent = elapsed() + 's';

    var c = chip();
    if (!c) return;
    if (S.kind) {
      c.hidden = false;
      c.className = 'tb ms-compile-chip is-running';
      c.textContent = 'compiling…';
    } else if (S.result) {
      c.hidden = false;
      c.className = 'tb ms-compile-chip ' + (S.result.ok ? 'is-ok' : 'is-bad');
      c.textContent = LABEL[S.result.kind] + (S.result.ok ? ' ready · ' : ' failed · ') + secs(S.result.seconds);
    }
  }

  function startTicking() {
    stopTicking();
    S.tick = window.setInterval(paintToolbar, 1000);
  }

  function stopTicking() {
    if (S.tick) { window.clearInterval(S.tick); S.tick = null; }
  }

  /* --------------------------------------------------------------- the panel */

  /* Opened the same way a click opens it, because the extension contract has no
     way for an extension to open its own panel. Reported rather than reached
     around: `ctx` has send, escape, card, block, selection, ms, notify and
     refresh, and nothing that says "show me". */
  function openPanel() {
    var c = chip();
    if (c) { c.hidden = false; c.click(); }
  }

  function stepRow(s) {
    var mark = s.ok ? '·' : '!';
    return '<div class="ms-compile-step' + (s.ok ? '' : ' is-bad') + '">' +
      '<span class="m">' + mark + '</span>' +
      '<span class="n">' + esc(s.step) + '</span>' +
      '<span class="t">' + esc(secs(s.seconds)) + '</span>' +
      (s.detail ? '<span class="d">' + esc(s.detail) + '</span>' : '') +
      '</div>';
  }

  function progressBody(ctx) {
    var rows = S.steps.map(stepRow).join('');
    if (S.kind) {
      rows += '<div class="ms-compile-step is-now"><span class="m">→</span>' +
        '<span class="n">working</span><span class="t">' + elapsed() + 's</span></div>';
    }
    if (!rows) {
      rows = '<p class="empty">Nothing compiled yet. Compile PDF runs three LaTeX passes ' +
        'around a bibtex; Compile Word goes through the pandoc-docx skill.</p>';
    }

    var html = ctx.card(S.kind ? 'Running' : 'Steps', S.kind ? LABEL[S.kind] : '', rows);

    var r = S.result;
    if (r && !S.kind) {
      var inner;
      if (r.ok) {
        inner = '<div class="ms-compile-path">' + esc(r.output) + '</div>' +
          '<div class="row ms-compile-row">' +
          '<button class="btn pri" data-act="compile:reveal">Reveal in Finder</button>' +
          (r.url ? '<a class="btn" href="' + esc(r.url) + '" target="_blank" rel="noopener">Open it</a>' : '') +
          '<span class="hintline">' + esc(secs(r.seconds)) + '</span></div>';
      } else {
        inner = '<div class="ms-compile-error">' + esc(r.error || 'it failed and said nothing') + '</div>' +
          (r.log ? '<div class="hintline">the full transcript is at ' + esc(r.log) + '</div>' : '');
      }
      if (r.notes && r.notes.length) {
        inner += '<ul class="ms-compile-notes">' +
          r.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
      }
      html = ctx.card(r.ok ? 'Done' : 'Failed', LABEL[r.kind] || '', inner) + html;
    }
    return html;
  }

  function view(ctx) {
    S.ctx = ctx;
    var r = S.result;
    var sub;
    // No number here: this line is only rebuilt when a frame arrives, and a
    // stale count of seconds is worse than none.
    if (S.kind) sub = 'this takes tens of seconds; every step appears as it finishes';
    else if (r) sub = (r.ok ? 'finished in ' : 'failed after ') + secs(r.seconds);
    else sub = 'nothing compiled in this session yet';
    return {
      eyebrow: 'Compile',
      chip: S.kind ? ['c', 'running'] : (r ? [r.ok ? 'v' : 'm', r.ok ? 'ready' : 'failed'] : null),
      title: S.kind ? ('Compiling ' + LABEL[S.kind]) : (r ? LABEL[r.kind] : 'Compile'),
      sub: sub,
      tabs: [{ name: 'Progress', body: progressBody(ctx) }]
    };
  }

  /* -------------------------------------------------------------- the actions */

  function post(payload, done) {
    fetch('/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        done(resp.status, body);
      });
    }).catch(function (err) {
      done(0, { error: String(err) });
    });
  }

  function start(kind, ctx) {
    S.ctx = ctx;
    if (S.kind) { openPanel(); return; }
    S.kind = kind;
    S.label = LABEL[kind];
    S.steps = [];
    S.result = null;
    S.started = Date.now();
    paintToolbar();
    startTicking();
    openPanel();
    post({ action: kind }, function (status, body) {
      if (status === 202) return;
      // Nothing will arrive over the socket, so the wait has to end here.
      S.kind = null;
      stopTicking();
      S.result = {
        kind: kind, ok: false, seconds: 0, output: null, url: null,
        error: (body && body.error) || ('the server refused the compile (' + status + ')'),
        notes: [], steps: []
      };
      paintToolbar();
      ctx.refresh();
    });
  }

  function reveal(ctx) {
    if (!S.result || !S.result.output) return;
    post({ action: 'reveal', path: S.result.output }, function (status, body) {
      if (status !== 200 && S.result) {
        S.result.error = (body && body.error) || 'the file could not be revealed';
        ctx.refresh();
      }
    });
  }

  /* --------------------------------------------------------------- the frames */

  function onFrame(msg, ctx) {
    S.ctx = ctx;
    if (msg.phase === 'start') {
      S.kind = msg.kind;
      S.label = msg.label || LABEL[msg.kind];
      S.steps = [];
      S.result = null;
      if (!S.started) S.started = Date.now();
      startTicking();
    } else if (msg.phase === 'step') {
      S.steps.push({ step: msg.step, ok: msg.ok !== false, seconds: msg.seconds, detail: msg.detail });
    } else if (msg.phase === 'done') {
      S.kind = null;
      stopTicking();
      S.result = {
        kind: msg.kind, ok: !!msg.ok, seconds: msg.seconds, output: msg.output,
        url: msg.url, error: msg.error, log: msg.log,
        notes: msg.notes || [], steps: msg.steps || []
      };
      if (msg.steps && msg.steps.length) {
        S.steps = msg.steps.map(function (s) {
          return { step: s.name, ok: s.ok, seconds: s.seconds, detail: s.detail };
        });
      }
      S.started = 0;
    }
    paintToolbar();
    ctx.refresh();
  }

  /* ----------------------------------------------------------------- the skin */

  function style() {
    if (document.getElementById('ms-compile-style')) return;
    var el = document.createElement('style');
    el.id = 'ms-compile-style';
    el.textContent = [
      '.tb.is-compiling { border-color: var(--accent); color: var(--accent); }',
      '.ms-compile-chip.is-running { border-color: var(--accent); color: var(--accent); }',
      '.ms-compile-chip.is-ok { border-color: var(--verbatim); color: var(--verbatim); }',
      '.ms-compile-chip.is-bad { border-color: var(--missing); color: var(--missing); }',
      '.ms-compile-step { display: grid; grid-template-columns: 1rem 1fr auto; gap: .1rem .5rem;',
      '  align-items: baseline; font-family: var(--mono); font-size: .72rem; padding: .18rem 0; }',
      '.ms-compile-step .m { color: var(--faint); }',
      '.ms-compile-step .t { color: var(--muted); }',
      '.ms-compile-step .d { grid-column: 2 / -1; color: var(--missing); font-size: .68rem; }',
      '.ms-compile-step.is-bad .n { color: var(--missing); }',
      '.ms-compile-step.is-now .n { color: var(--accent); }',
      '.ms-compile-path { font-family: var(--mono); font-size: .72rem; word-break: break-all;',
      '  color: var(--ink); margin-bottom: .5rem; }',
      '.ms-compile-row { display: flex; gap: .4rem; align-items: center; flex-wrap: wrap; }',
      '.ms-compile-row .btn { text-decoration: none; }',
      '.ms-compile-error { font-family: var(--mono); font-size: .72rem; color: var(--missing);',
      '  white-space: pre-wrap; word-break: break-word; }',
      '.ms-compile-notes { margin: .5rem 0 0 1rem; font-size: .72rem; color: var(--muted); }'
    ].join('\n');
    document.head.appendChild(el);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { style(); chip(); });
  } else {
    style();
    chip();
  }

  MSViewer.extend({
    name: 'compile',
    act: {
      'compile:pdf': function (ctx) { start('pdf', ctx); },
      'compile:docx': function (ctx) { start('docx', ctx); },
      'compile:reveal': function (ctx) { reveal(ctx); }
    },
    open: {
      'compile': function (ctx) { return view(ctx); }
    },
    frame: {
      'compile': function (msg, ctx) { onFrame(msg, ctx); }
    }
  });
})();
