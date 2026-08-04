# manuscriptor

obsidian: `/Users/bbdaniels/Documents/Obsidian/Manuscriptor`

Read `Manuscriptor/Tasks.md` at session start. The design is `Manuscriptor/plans/2026-07-22 - Phase 1 Design.md`; verified findings and decision rationale are in `Manuscriptor/Technical Notes.md`. Read the design before implementing a milestone, because most of the non-obvious constraints are recorded there rather than in the code.

## What this is

A live manuscript editor. LaTeX renders to a page where every block is addressable back to its source bytes, so a margin comment or a direct edit resolves to a real byte range rather than to a page number and a highlighted phrase.

`manuscriptor serve <dir>` works end to end today. The drain (`proc` and the wake job) is the piece still missing, so chats land in `comments.jsonl` and nothing reads them.

## Invariants

**Context wide, unit narrow.** A worker may read as widely as it needs and may only ever write one block. This is what makes running edits live acceptable. Do not add a code path that writes more than one block per operation.

**The server has zero knowledge of Claude, and Claude never talks to the server.** They communicate through the `.tex` tree and `comments.jsonl`, and nothing else. Do not add an LLM call inside `manuscriptor/server/`, and do not add an HTTP client to the drain.

**`comments.jsonl` is append only.** Two processes write it. A rewrite introduces conflicts that appending cannot have. State changes are new records, never edits to old ones.

**Block identity is content derived, never positional.** Line numbers move the moment anything above them changes.

**Never write to a generated ARTIFACT -- and a block is only one kind of artifact.** A file written by analysis code must refuse the write and name its producer instead. This invariant was widened deliberately on 2026-08-04, from blocks alone, when the same failure turned up one file over. The original case: `tables/*.tex` produced by an R or Stata script, where editing hardcodes a result and violates the standing rule against typing numbers into LaTeX. The case that widened it: covet-india's `sample.bib` is exported from Zotero by `manuscript/make-bib.py`, and `plan_citation` appended an entry to it. The appended entry dies at the next `make bib`; the `\citep{...}` spliced into `main.tex` survives -- a citation with no bibliography entry, a broken build the author did not cause and could not see coming. Worse, the insert minted its own cite key while the generator gates on Zotero's `citationKey`, so the regeneration would have failed its resolve-to-exactly-one check on a key nothing in the library carries. The refusal is a BLOCKING check naming the producer and the remedy the file's own header states, never a silent skip: over-claiming a hand-maintained `.bib` would refuse every legitimate insertion, so the author has to be able to see which file, which producer, and what to run instead.

**Provenance is decided in `server/producers.py` and nowhere else, whatever the suffix.** One function, `provenance()`, answers "is this written by something other than the author" for a `.tex` block and a `.bib` alike; callers ask it and never re-derive an answer. A file's own `GENERATED FILE` / `DO NOT EDIT` header is a second SIGNAL feeding that one decision -- a producer scan can only see scripts it can reach, and a Makefile rule or a generator in another repo is invisible to it -- and it must never become a second implementation living somewhere else. Never infer "generated" from a path or a directory name. That guess once marked 74% of the reference manuscript uneditable, because its hand-written prose appendices are also not the root file. `segment()` decides only whether a block can be spliced as one byte range; that is a different question and the two must not be folded together again.

**Ids are content-derived, so an edit renames its own block.** Anything comparing block ids across two builds must go through `rematch` and carry the rename onward, or drafts and chats keyed to the old id are silently orphaned. This bug has already been introduced once, in the server's patch diff.

**LaTeX table-structure repair -- column-type normalization AND header-row identification -- is decided in `render/tables.py` and nowhere else, in this repo or any other.** This invariant was widened deliberately on 2026-08-03, from column types alone, when the second repair was added. Both answer the same question -- what does this LaTeX table actually say -- and both can only be answered while the LaTeX still says it, so they get one home rather than two.

The header half: pandoc's LaTeX reader promotes at most ONE row into `<thead>`, so a table whose header is two rows deep comes back as body rows only, `theadCount` 0, and every sticky-header and header-rule style switched off -- 31 of the 44 tables actually reachable from the served documents. (A `.tex`-file count says 72 of 243; most of those files are never reached by a render, and the rendered count is the one that matters. Pandoc also emits no `<thead>` for a SINGLE-row header with no closing rule, which is why the rendered number is not simply the multi-row count.) `mark_header_rows` marks those rows before pandoc reads them, using the full-width rules the author wrote, and `render/postprocess.py` may only CARRY that marking into `<thead>`. It must never infer which rows are headers: covet-india's Table 1 puts `\multicolumn{6}{l}{\textit{Patna}}` in the middle of its body as a panel label, indistinguishable from a header row by content and not one. In the LaTeX the rules say which is which; in the HTML they are gone. The mark goes INSIDE a leading `\multicolumn`'s content group, because a cell that begins with `\multicolumn` is a spanning cell to pandoc and anything in front of it costs the span and the words inside it, at exit 0.

The column-type half: stripping a `\newcolumntype` and rewriting the specs that used it are one operation, and half of it is worse than none: strip alone and a longtable degrades into prose at exit 0, or estonia-ecm's redefined `r` -- ragged-*right*, so left-aligned -- renders right-aligned, backwards and silently, with the table intact so no structural guard fires. That is why a partial re-implementation is the hazard rather than a missing one. There were two copies on 2026-07-29, this one and a standalone written that afternoon in the Word submission skills; diffed over 3,477 `.tex` files they disagreed on eight, and the wrong copy was this one, losing estonia-ecm's balance table to a `\multicolumn` broken across a line. The second copy existed for about two hours. That is the argument, not a footnote to it: two implementations of this operation did not need months to diverge on real manuscripts, they needed an afternoon. The skills now import this module and fail loudly when it is absent, because a skipped normalization ships degraded tables into the `.docx` a journal submission is built from. A guard in `tests/test_render_tables.py` fails if anything re-implements EITHER half, and it scans the whole of `~/.claude/skills/` as well as this package -- the copy that existed lived under `submission/`, but the next one has no reason to.

**One observer per directory, and `server/watch.py` is what enforces it.** Watchdog's fsevents backend keys its stream registrations in a PROCESS-GLOBAL table on `(path, is_recursive)`, so two `Observer()` objects on one directory are not independent -- they are one registration plus a `RuntimeError` thrown inside the emitter thread, where nothing catches it. The second watcher then exists, answers `stop()`, and never delivers an event. That is how `serve` spent weeks holding a dead watch on `.manuscriptor/agent`: the feed and the ledger are two files in one directory and both legitimately need watching, so both call sites were correct and the defect was a level below them. The live history push was simply off, and the traceback at boot read as cosmetic. Callers ask for a file or a tree; `watch.py` shares the underlying observer, fans out to every handler, and refcounts the teardown so the first leaver cannot unwatch the others.

**Everything on disk goes through `server/paths.py`, and nowhere else names it.** The layout was once fourteen literal spellings of `root / "build" / "manuscriptor"` across five modules, which made it unchangeable in practice. One module answers "where does Manuscriptor keep its files"; a guard in `tests/test_paths.py` fails if anything else spells it.

**The hidden directory writes its own `.gitignore`, and the rule hides itself.** `.manuscriptor/` sits inside the manuscript directory, which is nearly always a git working tree the author cares about. Serving a paper must never make `git status` grow, so the ignore file covers everything including itself. `comments.jsonl` is the single exception, because a coauthor needs the review record.

**Three tiers, and only one of them is disposable.** `cache/` is regenerable by rebuilding and is the ONLY thing `manuscriptor clean` may remove. `drafts.json` and `agent/` are durable but private. `comments.jsonl` is durable and tracked. Do not move anything durable under `cache/`: `clean` was once `rmtree` on the directory holding `drafts.json`, so running it destroyed unsaved text no rebuild can reconstruct.

**Compiling writes only into the cache, except the deliverable.** The `.aux`, `.log`, `.bbl` and `.blg` stay hidden; the reference manuscript has several of those COMMITTED, so writing them beside the source rewrites tracked files. The finished PDF is copied out beside the `.tex` on success and ONLY on success, since replacing a good PDF with the output of a compile that died in pass 2 leaves a file that still opens and is quietly wrong. A read-only serve withholds even that copy.

**`tidy` reports and does not sweep.** It runs against real manuscripts. Whether a file is regenerable is decided by its suffix; whether removing it is safe is decided by asking git. Never conflate the two: dsp-bias tracks `main.bbl` and `supplement.bbl`, so a suffix-based sweep would delete two committed files.

## Never drive the editor against a real manuscript

`serve` is read-write. Automated interaction against a live manuscript WILL
eventually write to it: on 2026-07-22 a browser-driven verification pass left
`\footnote{}\footnote{}` at the start of a paragraph in estonia-ecm's main.tex.
It was recoverable only because that file happened to be clean in git.

Copy the manuscript to a scratch directory and point `serve` at the copy for any
test that clicks, types, or dispatches events. The real manuscript may be served
for reading and screenshots, and nothing else.

## Testing

Watch every guard fail before trusting it. A test that has never failed proves nothing, and a skipped test is not a pass. The tests that matter most: flatten resolves nested includes with exact offsets; block ids survive an edit above them; sentinels round-trip through pandoc into the correct position; a splice to block N changes only block N's bytes; a comment on block 40 still resolves after block 3 is rewritten.

**A test may not write a websocket frame.** For a long time none did, and the whole live push path was untested while the boot path was asserted everywhere -- so the server rebuilt correctly and the open page stayed wrong, in four places at once, under a suite that passed. `tests/pagedriver.py` loads the page the server renders, runs the real `viewer.js` in it, and hands it frames the server built (`_diff`, or whatever `broadcast` was handed); assert on what the page holds afterwards. It needs node and jsdom -- run `npm install` in `tests/js` -- and `tests/test_live_frames.py` SKIPS without them, so check that it ran.

## Verifying rendered output

Arc is effectively always running, and launching a second instance is prohibited. Probe `curl -s 127.0.0.1:9222/json/version` and connect to the live session via the chrome-devtools MCP. Point a tab at the running server rather than at a file, and call `resize_page` to 1440x900 before screenshotting, since retina defaults exceed the 2000px image limit.

## Test manuscripts

`~/Projects/estonia-ecm/latex` is the reference case: 84KB, `AEA.cls`, tables via `\include`, appendices via `\input`, figures as PNG, 59 `\ref`. Render fidelity has been verified only against it. esttab regression tables and custom preamble macros are untested and either could invalidate M2.
