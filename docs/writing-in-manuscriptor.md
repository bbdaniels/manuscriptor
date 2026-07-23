# Writing in Manuscriptor

A guide for the person writing the paper, not the person maintaining the tool.

## Starting

```bash
manuscriptor serve ~/Projects/estonia-ecm/latex
```

That renders the manuscript, opens it, **and runs the agent**: a Claude Code
session beside the server that answers comments as you leave them. That is the
whole workflow, so it is the default, from the CLI and from the app alike.
Editing is live: a change you type is written to the `.tex` file about a second
after you stop typing. There is no save button and no save keystroke.

Two variants matter.

```bash
manuscriptor serve <dir> --read-only     # render and read, nothing can write
manuscriptor serve <dir> --no-agent      # serve without the agent; comments queue
```

`--read-only` is the one to reach for when you want to look at a paper rather
than work on it. Nothing reaches the filesystem in that mode, not the `.tex` and
not the comment log, so it is safe by construction rather than by remembering.
It implies `--no-agent`. With `--no-agent`, comments queue until any Claude
session in the repo runs "proc the comments". On a machine without the
`claude` CLI, serve degrades to that mode with a warning.

`~/Projects/manuscriptor-demo` is a copy of estonia-ecm kept as its own git
repository. Break it however you like and put it back with
`git -C ~/Projects/manuscriptor-demo checkout .`. Use it to try anything you
have not tried before.

## Several documents in one directory

A directory often holds more than one document: the paper, an online appendix,
a response to reviewers, a highlights page. Any `.tex` that declares its own
`\documentclass` counts, and when there is more than one, a switcher appears at
the left of the toolbar. Pick a document and the page rebuilds around it,
Overleaf-style; the directory, the comment log, and the git history stay
shared.

Comments know which document they were left on. A note you leave on the
response queues against the response, and a session draining the paper never
sees it; `manuscriptor proc --main response.tex` drains that document's queue.

With no `main.tex` and several documents, `serve` asks you to pick with
`--main` rather than guessing. Double-clicking a document in Finder is never
ambiguous: a file that declares a documentclass opens as itself.

## The page

Three columns, each scrolling on its own. The window itself never scrolls.

**Left** is the outline, folded to top-level sections. Click a section to jump.
Below it, the files, citations, computed values and exhibits the manuscript
holds, and the activity ticker when an agent is working.

**Centre** is the manuscript. Click any paragraph, table, figure or heading to
select it.

**Right** is the inspector, and it is pinned rather than following: scrolling the
manuscript never clears what you have open, so you can hold a table's code in
view while reading what the introduction claims about it. When the block you
have open scrolls out of sight, a control appears offering the way back.

## Reading

The text carries two signals worth knowing before you read a page.

**A citation's underline is its evidence status.** Green means the supporting
quote is verbatim in the cited paper. Amber means a passage was reported but
could not be matched. Red means nothing was found, or the paper is not in your
library. Scanning a page tells you where your support is thin before you read a
word.

**Run evidence…** in the toolbar is what colours them. It streams its stages to
the ticker and recolours the underlines when it lands; the first run on a
manuscript pays for extraction on every citation pair, and every run after
only pays for what changed. It reads your Zotero library and never writes it.
When a run cannot find fulltext for some pairs, a **Fetch missing PDFs…**
button appears beside it. That click, and only that click, downloads PDFs into
your Zotero library, then re-runs the pass to upgrade the underlines. It is a
separate button on purpose: a routine build never touches your library as a
side effect.

**A violet number came from your analysis code**, through `\input` of a file some
script wrote. An unmarked number in prose is one you typed by hand. The page
shows you that without being asked, which is the no-hardcoded-results rule made
visible rather than remembered.

Click either one to see what is behind it.

## Editing

Click a paragraph and the Source tab shows **its real LaTeX**, not the rendered
text. Edit it there.

It shows the unflattened source, so a paragraph containing `p=\input{exhibits/pval}`
shows you exactly that rather than `p=0.096`. This matters: editing the resolved
value would bake a result into the manuscript, which is the one thing the tool
exists to prevent, and it cannot happen because you are never holding the value.

Saving is continuous but not reckless. It writes on a pause of about a second
and on blur, and only when the block parses as balanced LaTeX. Type `\citep{`
and it holds, tells you why, and keeps your text. The state line sits under the
title.

**Nothing you type is ever discarded.** A draft belongs to its block, not to the
panel showing it, so clicking away, scrolling off, or an agent editing something
else all leave it intact. It survives a reload. The paragraph carries a mark
until you save or discard.

A block whose file is written by analysis code refuses the edit and names the
script instead.

## Comments, and having them answered

Open a paragraph's **Chat** tab and type what you want changed. That is the
whole gesture; there is no separate markup step and nothing to extract
afterwards.

The inspector's resting panel, before you select anything, is a chat about the
**whole manuscript**. A note typed there is a comment with no paragraph: "check
the tenses across the results section", or a question about the paper. The
session decomposes it into per-paragraph work itself, and each of those writes
still touches exactly one block.

The agent answers in words as well as edits. A reply lands in the same chat the
comment was typed into, so a question gets an answer and an edit that needed a
judgment call comes with its reasoning. The session also reads the manuscript's
project notes in the Obsidian vault before working, so an edit respects the
decisions recorded there, not just the paragraph in front of it.

Comments queue. The header carries the standing state, `3 queued · 1 working`,
and the ticker names what was touched by its section rather than an id.

They are answered **automatically by default**: one standing agent session
runs beside the server, parks on the comment log, and wakes when a comment
lands; anything already queued is worked when the server starts. The pin
turns blue within seconds of your comment (the session marks it working
before it reads anything), the paragraph updates underneath you, and the pin
turns green. The first comment after a launch pays the session's boot, about
a minute; after that each one is a single wake. With `--no-agent`, the same queue waits instead, and any Claude
Code session in that repo drains it when you say "proc the comments".

Know what is running. That session edits inside the manuscript directory
without asking each time; that is the point, and it is why the header and
ticker exist: you can always glance up. Everything it does goes through git,
so commit before a long session and `git diff` shows exactly what changed.

The safety property behind all of this: a worker may read as widely as it needs
and may write **one block**. It can read the section, the neighbouring
paragraphs, the bibliography and the script behind a number, and change one
paragraph. That constraint is what makes running it live acceptable.

## Checks, and the rest of the skill suite

Two menus in the toolbar reach the skills you already use from the terminal.

**Checks…** runs a review: the preflight (consistency-check's seven passes),
the full manuscript review, the revision audit (across the paper, appendix,
and response sharing this directory), or the bibliography. Picking one queues
a single comment; the session runs the skill and every finding comes back as
a comment **pinned to the paragraph it concerns**, anchored by the sentence
the finding quotes, so a preflight reads exactly like an imported referee
report. Findings arrive in a `review` state: the header counts them
separately ("2 queued · 12 to review") and the session never treats them as
instructions, so an agent cannot end up working its own review. Each finding
carries its triage: **Ask to fix** files an ordinary comment the queue works
under the usual one-block rule; **Dismiss** closes it. Re-running a check
skips findings already open; a dismissed finding that a later run raises
again is the check telling you it still thinks so.

**Produce…** is the generative half: the declaude rewrite (decomposed
paragraph by paragraph, one block per write as always), drafting a section,
the seminar deck, and the submission packages. Artifacts land in
`build/manuscriptor/` and the reply tells you where.

The whole exchange is records in `comments.jsonl`, so the review, your
decisions, and the fixes are one audit trail beside the paper.

## Inserting

Click a paragraph, open **Source**, and the bar under the editor offers
Citation, Number and Footnote at your cursor, and Exhibit or a new paragraph
after the paragraph.

Every insertion writes more than one file, and it shows you which before it
writes anything.

**A citation** searches your Zotero library first, then Crossref and OpenAlex.
It writes the `\citep` at your cursor, the entry in your `.bib`, and the Zotero
record if it is new. A key that fails your identity gate, meaning a canonical
DOI with Crossref and OpenAlex agreeing, is not inserted, and the page says
which check failed.

**A number has no field that accepts a literal.** The value comes from a
quantity your code already computes, or the insertion writes the code that
computes it. It writes the fragment file, the line in the producing script, and
the `\input` at your cursor. You re-run the script.

**An exhibit** adds the caption, the label, and the line in your runfile. That
last one matters most: an exhibit missing from the runfile goes stale on your
next rebuild and nobody notices until a referee does.

## Compiling

**Compile PDF** and **Compile Word** are in the toolbar.

PDF runs three passes around a bibtex, and reports each step as it finishes, so
you can see it working rather than watching a button go quiet. Roughly thirty
seconds for a full paper. When it fails you get the LaTeX error that actually
stopped it, with its source line, rather than the first warning in the log.

Word goes through your `pandoc-docx` skill, so it arrives with its table rules,
its cross-references resolved, and its figures embedded, and it is not handed
over until `textutil` confirms Word can open it.

Both write into `build/manuscriptor/` inside the manuscript, which ignores
itself, so compiling never grows your `git status`.

## Bringing in a reviewer

**Import comments…** takes a marked-up PDF or a `.docx` with tracked changes.

Annotations are anchored by **the text the reviewer highlighted**, not the page
number, so they survive you rewriting everything around them. That is the whole
design: a page number is meaningless the moment you touch the paper, and the
highlighted sentence is findable.

Anything that cannot be placed confidently goes to a tray rather than onto a
guessed paragraph, and you place it by hand. A reviewer's comment attached to
the wrong sentence is worse than one you have to place yourself.

Imported comments become ordinary comments. They queue, they show in the margin,
and a session drains them exactly like your own.

## The app

```bash
cd ~/Projects/manuscriptor/shell && ./build.sh
```

Produces `shell/build/Manuscriptor.app`. Move it to `/Applications` if you want
it in Spotlight; it runs from where it is built either way.

Right click any `.tex` and choose Manuscriptor. It walks up to the manuscript
root rather than serving the fragment, then jumps the page to the file you
opened. The app owns its server and kills it on quit.

## What it will not do

Some of these are deliberate and some are simply not built. Both are worth
knowing before you are surprised.

It **will not let you type a number**. There is no field for it anywhere.

It **will not edit a file your analysis code writes**. It names the script
instead.

It **will not resolve an `\input` to its value** when you edit a paragraph.

It **cannot describe every computed value.** It reads your producing code to say
what a fragment is, and how well that works depends on how your code names its
outputs. On qutub-india it describes 96 of 128 fragments, 66 of them saying more
than where the number came from. On manuscripts whose scripts build filenames by
concatenation it says "producer unknown" rather than guessing, because a wrong
description of a coefficient is worse than none.

It **will not render a manuscript that does not compile.** qutub-india currently
fails on genuinely unbalanced braces in
`docs/overleaf-mumbai/tab/t-learning.tex` and `t-convenience.tex`, where row
labels are written `{Samples}}`. It fails for LaTeX too.

Sixteen blocks in estonia-ecm cannot be anchored, mostly display equations glued
into a sentence. They render; you cannot click them.

## When something looks wrong

The terminal prints diagnostics on every build: unanchored blocks, unresolved
references, missing includes. If a paragraph will not select, it is probably in
that list.

Everything the tool writes goes through git. Your manuscript is a repository, so
`git diff` shows exactly what changed and `git checkout` undoes it. That is the
real safety net, and it is worth committing before a long session with
`--with-agent`.
