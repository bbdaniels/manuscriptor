# Evidence panel: identity, and two ways out of it

**Date:** 2026-08-04
**Status:** approved, not yet implemented

## The problem

The Evidence tab shows a resolved title, a cite key, a status chip, and the
extracted quotes. It shows no author, no journal, no year, and no identifier of
any kind. So the panel can tell the author that a quote was found, and cannot
tell him *what it was found in* — the one question the panel exists to answer —
and gives him no way to go and look.

The data has existed the whole time. `evidence/resolve.py:111-127` writes
`authors`, `year`, `journal`, `doi` and `zotero_key` into `citations.json`, and
`server/build.py:440-447` drops all five when it builds the page payload. For
covet-india that is 62 DOIs across 64 entries and a Zotero item key for 64 of
64, already on disk, never crossing into the browser.

Two corrections to the record, both of which changed this design:

- Tasks.md says covet-india's `sample.bib` has "45 entries and zero `doi`
  fields". It now has 64 entries and 62 DOIs; the bib was re-exported. Separately
  `resolve.py:113` already prefers Zotero's DOI over the bib's, which backfills
  several the bib still lacks. A DOI link is viable for nearly the whole library,
  which it would not have been when that note was written.
- The 11 entries in `missing.json` — the ones with no full text — carry 10 DOIs
  and 11 Zotero keys between them. The identifier is available *precisely where
  the PDF is not*, so this feature is most useful in the case that currently
  renders as a dead end.

## A live bug this feature inherits

`shell/Sources/Manuscriptor/DocumentWindow.swift` conforms to
`WKNavigationDelegate` and implements only `didFinish` and
`didFailProvisionalNavigation`. There is no `decidePolicyFor navigationAction`,
no `WKUIDelegate`, and no `NSWorkspace.open` anywhere in the shell.

Pandoc already emits 64 anchors into covet-india's rendered bibliography, 62 of
them `https://doi.org/…`. `viewer.js`'s click handler falls through to the
block-select branch at `viewer.js:2774`, which returns without preventing
default. **So clicking a DOI in the bibliography today navigates the app's only
window away from the manuscript**, recoverable only with Cmd-R. That is live,
today, with no feature added.

A `zotero://` URL fails differently and worse: nothing intercepts the scheme,
WebKit drops it inside the content process, and the author sees a flicker and no
Zotero. The obvious implementation of "open in Zotero" is therefore the one that
silently does nothing.

This is fixed as part of this work, because the feature inherits it either way.

## Design

### The panel

Beneath the quotes, each evidence entry gains a metadata block and an action
row.

The metadata block renders through the existing `<dl class="stat">` rows
(`viewer.js:1712-1717`): authors, journal, year, and the DOI as a **visible URL
string**, not hidden behind friendly text. That last choice is deliberate and
cheap — the manuscript body two inches to the left already renders every
bibliography DOI with its href equal to its visible text, so a panel that hid
them would be visibly inconsistent with the page it annotates. Absent fields are
omitted rather than rendered empty.

The action row offers **both** ways out, because they are different moments:

- **Open PDF** — primary. The verification loop: is this quote actually in the
  source.
- **Open in Zotero** — the item record, with its notes, tags and attachments.

An entry with no attachment shows the DOI and Open in Zotero, and does not
render an Open PDF button that would do nothing.

### Both opens go through the server

`compile.py:679` already runs `["open", "-R", path]` server-side for Reveal in
Finder, gated to targets inside the build directory. It is the one mechanism in
this app that reliably opens an external thing, and this follows it rather than
inventing a second one. `ext/compile.js:154`'s `target="_blank"` is the
counter-example: it needs `WKUIDelegate.createWebViewWith`, which does not
exist, so it is dead in the app and works only in a browser tab.

- `POST /evidence/open-pdf {cite_key}` → resolve the attachment path → `open <path>`
- `POST /evidence/open-zotero {cite_key}` → `open zotero://select/library/items/<KEY>`

Neither involves webview navigation, so both behave identically in a browser tab
and in the app.

**The path gate is the one security-shaped thing here.** The resolved PDF path
must lie inside `~/Zotero/storage`, checked after resolution, on the same
argument as `compile.py:666`. A cite key is author-controlled input arriving over
HTTP, and `open` on an arbitrary path is an arbitrary-file-open.

### Path resolution happens on click, not at build

The Zotero storage directory is keyed by the **attachment** key, not the parent
item key, so a path needs a `children` call. `fetch.py:205-225` already makes one
and globs `~/Zotero/storage/<ATTACHMENT_KEY>/*.pdf`; that becomes a small named
resolver both the endpoint and `fetch.py` call.

Resolving on click rather than at build time is deliberate: a path baked into a
page payload goes stale the moment the author moves or re-attaches a file, and
the page can outlive that by hours.

### Cleanup that belongs in this change

`ZoteroItem.attachment_paths` (`zotero.py:63`, populated at `zotero.py:310-316`)
reads `data["path"]` off each attachment. Against the live local API that field
is `None` for every attachment checked, so the list is always empty — and
nothing anywhere consumes it. It is a second, broken answer to "where is this
PDF" sitting beside the working one.

It is deleted, or made to return the storage-glob path. Not left beside the new
resolver: two implementations of one operation is this repo's most common defect
shape, and one of these two is already wrong.

### The navigation policy

`decidePolicyFor navigationAction` is added to `DocumentWindow.swift`:

- Local navigations (the served `127.0.0.1` origin) proceed.
- `http`/`https` are cancelled and handed to `NSWorkspace.shared.open`, so they
  open in the default browser.
- Everything else is cancelled.

This repairs the 62 existing bibliography links as a side effect, which is the
larger half of its value.

## Testing

- **Payload:** the five fields survive `build.py` into `window.MS`. Watch it fail
  against current HEAD, where they do not.
- **Panel:** authors/journal/year/DOI render; absent fields are omitted, not
  blank; an entry with no attachment renders no Open PDF button.
- **Endpoints:** with a fake Zotero client and a temp storage dir — the happy
  path resolves and issues the right command; a cite key with no attachment
  returns a clean refusal rather than a 500; an unknown cite key 404s.
- **The gate:** a resolved path outside `~/Zotero/storage` is refused. This one
  gets an explicit hostile case, including a traversal attempt, and is watched
  failing with the gate removed.
- The `open` invocation is asserted as a command, never executed.
- **Navigation policy is Swift and this suite does not reach it.** It is verified
  by hand in the running app — a bibliography DOI opens the browser and leaves
  the manuscript on screen; an Open in Zotero action reaches Zotero.app. This is
  stated as manual verification and must not be reported as covered.

## Out of scope

- Landing the PDF at the quote rather than at page 1. `char_offset` indexes the
  concatenated fulltext string, not a PDF page, and `location_hint` is an LLM
  guess. There is no page number anywhere in the pipeline, so this would be new
  machinery, not a refinement.
- Fixing DOI coverage in the bibliographies that lack it (estonia-ecm 48 of 136,
  under a DOI-dropping `aea.bst`; teech 0 of 17). That is the separate
  bibliography-style work already recorded in Submission Preflight.
- Any write to Zotero. This feature is read-only against the library.
