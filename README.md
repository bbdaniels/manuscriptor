# manuscriptor

A live manuscript editor. LaTeX renders to a page where every block knows which bytes of which `.tex` file it came from, so you can click a paragraph, read its real source, edit it yourself or leave an instruction, and watch the change land without a rebuild step.

Replaces the tex → PDF → markup → extraction loop, and eventually Overleaf.

## Status

The loop closes. Serve a manuscript, click a paragraph, read its real LaTeX, edit it, and watch the change land on disk and redraw the page. Verified in a browser against estonia-ecm: 368 anchored blocks, 268 tests.

| | |
|---|---|
| `manuscriptor serve` | **works** |
| `manuscriptor build` | **works**, static anchored page |
| `manuscriptor blocks` | **works**, the block table |
| `manuscriptor evidence` | **works** (absorbed from cite-evidence) |
| `manuscriptor repair` / `clean` | **works** |
| `manuscriptor proc` | M5, the drain, not yet built |

```bash
manuscriptor serve ~/Projects/estonia-ecm/latex
```

Not yet built: the drain (`proc` and the wake job), so chats land in
`comments.jsonl` and nothing reads them; descriptions for computed values; the
insert flows beyond a footnote; reading in coauthor markup; the native shell.

The design lives in the Obsidian vault at `Manuscriptor/plans/2026-07-22 - Phase 1 Design.md`, with verified findings and decision rationale in `Manuscriptor/Technical Notes.md`. Read those before implementing a milestone.

## Install

```bash
cd ~/Projects/manuscriptor && pip install -e .
```

Requires `pandoc` and `pdftotext` (poppler) on PATH, the `claude` CLI for the default LLM backend, and a running local Zotero on port 23119 for the evidence pipeline.

## How it fits together

Two processes, sharing a filesystem, with an interface of exactly two things: the `.tex` tree and `comments.jsonl`.

**The server** renders, serves, watches, applies direct human edits as byte-range splices, and appends to the comment log. It has zero knowledge of Claude and carries no LLM dependency, which is what keeps it testable.

**A Claude Code session** reads new records from `comments.jsonl`, edits `.tex` files with ordinary tools, and appends state records. The server's watcher notices and pushes the redraw. Because it is just a Claude Code session, it inherits CLAUDE.md, the style file, and every existing skill for free.

The safety property is **context wide, unit narrow**: a worker may read the section, the neighbouring paragraphs, the bibliography, and the table being referenced, but may only ever write one block. A process that can read everything and write one paragraph cannot silently wreck a paper.

## Layout

```
manuscriptor/
  cli.py         entry point
  source/        flatten, segment, anchor, splice   (the byte-exact mapping)
  render/        pandoc, cross-references, postprocess
  server/        http, websocket, watcher, comment log
  evidence/      the absorbed cite-evidence pipeline
  templates/     page template, styles, viewer
shell/           standalone Manuscriptor.app        (late, off the critical path)
```

`source/` is the load-bearing package. Everything else depends on its mapping being exact, so keep it small and test it hard.

`server/producers.py` answers one question: is this `.tex` file written by
analysis code? A producer scan is definitive and names the owning script; a
content test covers the files no scan can claim, because manuscripts name their
outputs inconsistently (estonia-ecm by basename, qutub-india by concatenation).
Nothing else may decide it from a path. Guessing "generated means not the root
file" once marked 74% of the reference manuscript uneditable.

## Relationship to cite-evidence

`~/Projects/cite-evidence` is absorbed rather than depended on, because the flattening pass with a source map is strictly better than its parse stage and both tools want it. Two repos maintaining two LaTeX parsers that must agree is a split that rots quietly and bites mid-revision.

That repo stays in place and installed until M6 lands, so nothing that works today stops working. Its `cite-evidence` console script is deliberately not claimed here.
