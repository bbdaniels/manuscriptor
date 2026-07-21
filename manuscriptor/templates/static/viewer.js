(function () {
  const data = window.CITE_EVIDENCE || {};
  const panel = document.getElementById('evidence-panel');
  const manuscript = document.getElementById('manuscript');
  const filterInput = document.getElementById('filter-input');
  if (!panel || !manuscript) return;

  const claimsByKey = new Map();
  for (const claim of data.claims || []) {
    claimsByKey.set(claim.claim_id, claim);
  }

  manuscript.addEventListener('click', (e) => {
    const cite = e.target.closest('.citation');
    if (!cite) return;
    e.preventDefault();
    activateCite(cite);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') clearActive();
  });

  function activateCite(cite) {
    clearActive();
    cite.classList.add('is-active');
    const claimId = cite.dataset.claimId;
    const claim = claimsByKey.get(claimId);
    if (!claim) {
      panel.innerHTML = '<div class="panel-placeholder"><p>No claim record found for this citation.</p></div>';
      return;
    }
    const html = renderPanel(claim);
    panel.innerHTML = html;
    panel.scrollTop = 0;
  }

  function clearActive() {
    for (const node of manuscript.querySelectorAll('.citation.is-active')) {
      node.classList.remove('is-active');
    }
  }

  function renderPanel(claim) {
    const sectionLabel = claim.section ? humanizeSection(claim.section) : '';
    const parts = [];
    parts.push('<div class="panel-claim">');
    parts.push('<span class="label">claim</span>');
    parts.push(escapeHtml(claim.sentence || ''));
    if (sectionLabel) parts.push(`<div class="section">${escapeHtml(sectionLabel)}</div>`);
    parts.push('</div>');
    for (const key of claim.cite_keys || []) {
      parts.push(renderCard(claim.claim_id, key));
    }
    return parts.join('');
  }

  function renderCard(claimId, citeKey) {
    const cit = (data.citations_by_key || {})[citeKey] || {};
    const evidence = ((data.evidence_by_pair || {})[claimId] || {})[citeKey];
    const status = cardStatus(evidence);
    const title = cit.title || '(no title)';
    const authors = (cit.authors || []).slice(0, 4).join(', ');
    const yr = cit.year ? ` (${cit.year})` : '';
    const journal = cit.journal ? ` · ${cit.journal}` : '';
    const doi = cit.doi ? `<a href="https://doi.org/${escapeAttr(cit.doi)}" target="_blank" rel="noopener noreferrer">doi:${escapeHtml(cit.doi)}</a>` : '';
    const zot = cit.zotero_key ? `<a href="zotero://select/library/items/${escapeAttr(cit.zotero_key)}" title="Open in Zotero">open in Zotero</a>` : '';
    const sourceLabel = cit.fulltext_source ? `<span title="fulltext source">${escapeHtml(cit.fulltext_source)}</span>` : '';

    const head = `
      <div class="cite-head">
        <span class="cite-key">${escapeHtml(citeKey)}</span>
        <p class="cite-title">${escapeHtml(title)}</p>
        <p class="cite-meta">${escapeHtml(authors)}${escapeHtml(yr)}${escapeHtml(journal)}</p>
        <p class="cite-meta">${doi}${doi && zot ? ' · ' : ''}${zot}</p>
      </div>
    `;

    let body;
    if (!evidence) {
      const reason = cit.has_fulltext === false
        ? `No indexed fulltext available for this paper${cit.zotero_key ? ' in Zotero.' : '; not in your Zotero library.'}`
        : 'No evidence record yet — has the extract stage been run?';
      body = `<div class="quote-empty">${escapeHtml('No supporting passage found.')}<span class="reason">${escapeHtml(reason)}</span></div>`;
    } else if (!evidence.quotes || evidence.quotes.length === 0) {
      const reason = evidence.reasoning || 'Model returned no supporting passage.';
      body = `<div class="quote-empty">${escapeHtml('No supporting passage found.')}<span class="reason">${escapeHtml(reason)}</span></div>`;
    } else {
      const qs = evidence.quotes.map((q) => {
        const cls = q.status === 'paraphrase' ? ' is-paraphrase' : '';
        const statusCls = q.status === 'verbatim' ? 'is-verbatim' : 'is-paraphrase';
        const hint = q.location_hint ? `<span class="quote-loc">${escapeHtml(q.location_hint)}</span>` : '<span></span>';
        return `<div class="quote">
          <p class="quote-text${cls}">${escapeHtml(q.text)}</p>
          <div class="quote-foot">
            <span class="quote-status ${statusCls}">${escapeHtml(q.status)}</span>${hint}
          </div>
        </div>`;
      }).join('');
      body = `<div class="quote-list">${qs}</div>`;
    }

    const foot = `<div class="cite-foot"><span>status: ${escapeHtml(status)}</span>${sourceLabel}</div>`;

    return `<div class="cite-card status-${escapeAttr(status)}">${head}${body}${foot}</div>`;
  }

  function cardStatus(evidence) {
    if (!evidence || !evidence.quotes || evidence.quotes.length === 0) return 'missing';
    if (evidence.quotes.some((q) => q.status === 'verbatim')) return 'verbatim';
    return 'paraphrase';
  }

  function humanizeSection(s) {
    const idx = s.indexOf(':');
    if (idx === -1) return s;
    return s.slice(idx + 1);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  function escapeAttr(s) {
    return escapeHtml(s);
  }

  if (filterInput) {
    filterInput.addEventListener('input', () => {
      const q = filterInput.value.trim().toLowerCase();
      const cites = manuscript.querySelectorAll('.citation');
      if (!q) {
        cites.forEach((c) => c.classList.remove('is-faded'));
        return;
      }
      cites.forEach((c) => {
        const claimId = c.dataset.claimId;
        const claim = claimsByKey.get(claimId);
        const keys = (claim ? claim.cite_keys : []).join(' ').toLowerCase();
        let hay = keys;
        if (claim) {
          for (const k of claim.cite_keys || []) {
            const cit = (data.citations_by_key || {})[k] || {};
            hay += ' ' + (cit.title || '').toLowerCase();
            hay += ' ' + (cit.authors || []).join(' ').toLowerCase();
          }
        }
        const match = hay.includes(q);
        c.classList.toggle('is-faded', !match);
      });
    });
  }
})();
