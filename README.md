# manuscriptor

A live manuscript editor. LaTeX renders to a page where every block knows which bytes of which `.tex` file it came from, so you can click a paragraph, read its real source, edit it yourself or leave an instruction, and watch the change land without a rebuild step.

Replaces the loop of compiling to PDF, marking up the PDF, and typing the markup back into the source.

**[Interface demo](https://bbdaniels.github.io/manuscriptor/demo/)**, no install needed. A synthetic paper, rendered by the real renderer, carrying a real agent session: three comments, two landed edits and an answer about a citation. Every word in it is invented.

## What it does

The loop closes. Serve a manuscript, click a paragraph, read its real LaTeX, edit it, and watch the change land on disk and redraw the page. Verified in a browser against estonia-ecm, at 368 anchored blocks.

| | |
|---|---|
| `manuscriptor serve` | the live editor |
| `manuscriptor build` | a static anchored page |
| `manuscriptor blocks` | the block table |
| `manuscriptor evidence` | the citation evidence pipeline |
| `manuscriptor repair` / `clean` | fetch the PDFs evidence could not find, and empty the cache |
| `manuscriptor proc` | the comment queue a drain reads |
| `manuscriptor compile` | `--pdf`, and `--docx` if the two external pieces below are installed |
| `manuscriptor import` | reviewer PDFs and tracked changes |
| `manuscriptor state` | records what happened |
| `Manuscriptor.app` | a standalone shell, built by `shell/build.sh` |

```bash
manuscriptor serve examples/demo-paper                # the example in this repo, edit freely
manuscriptor serve /path/to/your/paper                # your own, read-write
manuscriptor serve /path/to/your/paper --read-only    # your own, nothing written
manuscriptor serve <dir> --with-agent                 # and a session draining comments
```

`--with-agent` starts a background Claude Code session that answers comments as
you leave them. It runs with edits accepted inside the manuscript directory,
which is the point of the flag and worth knowing before typing it: the header
carries `2 queued · 1 working` and a ticker names what was touched, so the work
is visible rather than silent. It refuses to combine with `--read-only`, and the
session dies with the server.

`serve` is read-write: an edit in the page is written to the `.tex` file on a
typing pause. `--read-only` renders and browses without any path reaching the
filesystem, not the manuscript and not the comment log, so pointing it at real
work is safe by construction rather than by remembering.

`examples/demo-paper` is a short invented paper, kept in this repository so there
is something safe to try the editor on. It has a table, a cross-reference and
three references, and it compiles. Break it however you like and `git checkout
examples/demo-paper` puts it back.

The honest limits, all documented in the guide: value descriptions depend on how
your analysis code names its
outputs (96 of 128 fragments on one test manuscript, 66 of them saying more than
where the number came from); sixteen blocks in the reference manuscript cannot
be anchored; and one test manuscript does not render at all, because it does not
compile.

The thing no amount of testing substitutes for is an afternoon of real writing
in it.

**If you are here to write a paper, read
[docs/writing-in-manuscriptor.md](docs/writing-in-manuscriptor.md).** This file
is about the code.

## Install

```bash
git clone https://github.com/bbdaniels/manuscriptor.git
cd manuscriptor
pip install -e .
```

Requires `pandoc` and `pdftotext` (poppler) on PATH, the `claude` CLI for the default LLM backend, and a running local Zotero on port 23119 for the evidence pipeline.

`manuscriptor compile --pdf` needs a TeX installation and nothing else. `--docx` needs two more things, and neither of them ships with this package:

- **The pandoc-docx conversion scripts.** Pandoc's own LaTeX to Word output is rejected by Word, so the conversion goes through HTML and is then repaired at the OOXML level by scripts this tool shells out to. Set `MANUSCRIPTOR_PANDOC_DOCX_DIR` to the directory holding them; without it the tool looks in `~/.claude/skills/pandoc-docx`.
- **A CSL style file**, which decides how the bibliography reads. A `.csl` beside the manuscript is used first; otherwise set `MANUSCRIPTOR_CSL` to one, or put one at `~/.csl/econ.csl`.

If either is missing, `--docx` refuses and names what it could not find. `--pdf` is unaffected.

## How it fits together

Two processes, sharing a filesystem, with an interface of exactly two things: the `.tex` tree and `comments.jsonl`.

**The server** renders, serves, watches, applies direct human edits as byte-range splices, and appends to the comment log. It has zero knowledge of Claude and carries no LLM dependency, which is what keeps it testable.

**A Claude Code session** reads new records from `comments.jsonl`, edits `.tex` files with ordinary tools, and appends state records. The server's watcher notices and pushes the redraw. Because it is just a Claude Code session, it inherits CLAUDE.md, the style file, and every existing skill for free.

The safety property is **context wide, unit narrow**: a worker may read the section, the neighboring paragraphs, the bibliography, and the table being referenced, but may only ever write one block. A process that can read everything and write one paragraph cannot silently wreck a paper.

## Layout

```
manuscriptor/
  cli.py         entry point
  source/        flatten, segment, anchor, splice   (the byte-exact mapping)
  render/        pandoc, cross-references, postprocess
  server/        http, websocket, watcher, comment log
  evidence/      the absorbed cite-evidence pipeline
  templates/     page template, styles, viewer
shell/           standalone Manuscriptor.app        (a wrapper, not needed to run it)
```

Client features register through `MSViewer.extend` from their own file under
`templates/static/ext/`, so adding one is a new file rather than an edit to
`viewer.js`. `templates/ext.py` picks them up automatically.

`source/` is the load-bearing package. Everything else depends on its mapping being exact, so keep it small and test it hard.

`server/producers.py` answers one question: is this `.tex` file written by
analysis code? A producer scan is definitive and names the owning script; a
content test covers the files no scan can claim, because manuscripts name their
outputs inconsistently. Nothing else may decide it from a path; the reasoning
is recorded in `CLAUDE.md`.

## Relationship to cite-evidence

The `cite-evidence` tool is absorbed here rather than depended on, because the flattening pass with a source map is strictly better than its parse stage and both tools want it. Two repos maintaining two LaTeX parsers that must agree is a split that rots quietly and bites mid-revision.

Its `cite-evidence` console script is deliberately not claimed here, so an existing install of that tool keeps working beside this one.

## License and citation

MIT. To cite the software, use the metadata in [CITATION.cff](CITATION.cff); a versioned DOI accompanies each release.
