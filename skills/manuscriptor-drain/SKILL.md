---
name: manuscriptor-drain
description: Use when Manuscriptor comments need addressing — the user says "proc the comments", "process my comments", "drain", or asks you to act on notes left in the Manuscriptor editor. Also the procedure a backgrounded watcher follows when it wakes.
---

# Draining Manuscriptor comments

The author leaves comments in the Manuscriptor editor. They land in
`comments.jsonl` beside the manuscript. Nothing reads them until you do.

You are not talking to the server. You share a filesystem with it: you read the
log, edit `.tex` files with your ordinary tools, and append state records. The
server's watcher notices your edit and redraws the author's page on its own.

## The procedure

**0. Load the project's context, read-only.**

The manuscript belongs to a research project whose live state is in the
Obsidian vault, not in the repo. Resolve which project owns this directory the
way the vault's own contract does: read the frontmatter of
`~/Documents/Obsidian/*/Tasks.md` and find the project whose `cwds:` globs
match the manuscript directory, longest match winning. Never match by name;
`cwds:` ownership is the only sanctioned mapping, and a shared `reads:` path
never attributes.

When a project owns the directory, read its `Dashboard.md`, the `## Active`
section of its `Tasks.md`, and its `Technical Notes.md` before working the
queue. That is where the deadlines, the framing decisions, and the "we decided
against X" notes live, and an edit that contradicts them is wrong even when it
answers the comment. Carry the relevant constraints into the items you hand to
subagents; workers do not re-read the vault. When no project owns the
directory, proceed without.

The vault is read-only here. The drain session's job is the manuscript;
session logging belongs to the ingest hooks, not to you.

**1. Read the queue.**

```bash
manuscriptor proc <manuscript-dir>
```

Each item gives you the comment, the block it sits on, its section, its
neighbours on either side, its citations and computed values, and the file and
line range. Items are marked either `EDIT` or `READ ONLY`.

**They arrive oldest first, and that is the order to work them.** The author's
header reads the same list: `3 queued · 1 working`. Working the newest first
means the comment he left ten minutes ago is the last one answered, and the
counter he is watching barely moves.

**2. One subagent per comment.**

Dispatch them together. Each subagent gets exactly one item: its comment, its
one writable block, and permission to read whatever it needs. Nothing about the
queue is sequential — see the concurrency note below — so a queue of five is
five parallel workers, not five turns.

Hand each subagent the item verbatim rather than a summary of it. The context
in the item is the whole point: a worker that has the section, the neighbours
and the producing script can answer a comment, and one that has a paraphrase
cannot.

**3. Mark it before you start.**

```bash
manuscriptor state <manuscript-dir> c-0007 working
```

The author sees that state in three places at once: the margin pin, the header
count, and the ticker. It is not bookkeeping, it is the only thing telling him
his prose is being edited while he reads it. Skipping it means edits appear
under him with no warning, which is worse than batching.

**4. Read as widely as you need. Change exactly one block.**

This is the constraint the whole design rests on. Read the section, the
neighbouring paragraphs, the bibliography, the table being referenced, the
script behind a number: whatever the comment requires. Then write **one block**,
the one named under `EDIT`.

A worker that can read everything and change one paragraph cannot silently
wreck a paper. Do not fix a second thing you noticed. Leave a comment of your
own if it matters, or tell the author when you report back.

**5. Edit the file at the given range**, using Edit as usual. You are editing
LaTeX source, so `\input{...}` directives in the block are the author's results
pulled in from analysis code. **Never resolve one to its value.** That would
hardcode a result, which is the single thing this tool exists to prevent.

**6. Mark it done, and say what happened when words are needed.**

```bash
manuscriptor state <manuscript-dir> c-0007 done
manuscriptor reply <manuscript-dir> c-0007 "Moved the caveat into the first sentence rather than cutting it; the JPubE referee asked for it explicitly."
```

The ticker shows the edit landing, not the claim that it would: a `done` with no
change behind it reads differently from one with a patch after it. Do not mark
work done that did not happen.

A reply joins the comment's own chat and appears on the author's page. Use one
whenever the edit is not self-explanatory: you chose between two readings of
the comment, you declined ("this number comes from a script; the fix belongs
there"), or the comment asked a question rather than for a change. A question
answered only by a `done` state looks ignored.

## Concurrency: do not serialize

**Two workers editing two paragraphs of the same file at the same time is safe.**
`splice` holds a per-file lock across read-locate-write plus an advisory
`flock`, so the second writer re-reads after the first has written and finds its
own block by content. This was measured: before the lock existed, eight
concurrent splices left two survivors. It is fixed, and the fix deliberately
kept the parallelism — the critical section is one read and one write, in
microseconds, so workers think concurrently and only their writes take turns.

So do not add a queue of your own, do not take turns by file, and do not wait
for one comment to finish before starting the next.

**The one thing that lock cannot cover is an editor that does not take it.**
Claude's ordinary `Edit` tool writes the file directly and holds nothing. So a
subagent editing `.tex` by hand should:

* **re-read the block immediately before writing it**, because another worker
  may have rewritten a paragraph above it and moved everything below;
* **keep the edit small and to one block**, matching a distinctive string rather
  than a line range, so a shifted file cannot make it land in the wrong place;
* **never rewrite a whole file** to change one paragraph. That is the operation
  that loses another worker's edit, and it is the only one that can.

## Things that will come up

**A check** is a document-level item whose CHECK line names a skill
(`consistency-check`, `review-manuscript`, `revision-audit`, `validate-bib`,
or a Produce skill like `declaude`, `talk`, `docx-package`). Invoke that
skill on this manuscript with the vault context you already loaded. For a
review-shaped skill, land every finding as its own comment:

```bash
manuscriptor comment <dir> --review --author proofreader --check consistency-check \
  --doc main.tex --quote "the exact sentence the finding concerns" \
  "What is wrong, and what would fix it."
```

The quote is what anchors the finding to its paragraph, so quote the
manuscript's own words exactly. Findings are `review` state: pinned for the
author, never presented back to you as work, so you cannot end up working
your own review. Duplicates against open findings are skipped automatically;
do not pre-filter. When the skill produces an artifact instead (a deck, a
submission package), put it under `build/manuscriptor/` and reply with the
path. Either way: reply with a one-line summary and mark the check's own
comment done.

**A document-level comment** has no block: the author typed it into the
manuscript panel rather than onto a paragraph, and its item says so. This is
orchestration work. Decompose it into per-block subagent tasks yourself; the
one-block-per-write constraint binds every worker exactly as before, and the
comment is yours to mark `working` when the first worker starts and `done`
when the last lands. Reply with what was changed and where, because there is
no single margin pin for the author to watch. A document comment that is a
question rather than an instruction gets a reply and a `done`, no edits.

**`READ ONLY`** means the file is written by analysis code. Do not edit it. The
fix belongs in the script named beside it; make that change instead, or explain
why you cannot.

**`re-anchored`** in a NOTE means the block was edited after the comment was
written, so its id changed and it was found again by its text. Check the comment
still makes sense against the paragraph you were handed.

**"the block this was written on is gone"** means the paragraph was deleted or
rewritten past recognition. Do not guess at a replacement. Tell the author, and
mark it `orphaned` if they agree.

**Prose belongs to the author.** Read `~/.claude/style/author-style.md` before
rewriting any sentence, and apply its §9 self-check. A comment asking you to
tighten a paragraph is asking for the author's voice, not a generic edit.

## Running live: the standing session

`manuscriptor serve` runs ONE persistent session beside the server by default
(from the CLI and from the app alike), and you are most often that session.
The protocol, which is also in your launch prompt:

1. Boot once: this skill, the vault context.
2. `manuscriptor proc <dir> --json`; for each pending item mark it `working`
   IMMEDIATELY, before reading anything else — the author is watching the
   pin, and the reading can happen after it moves.
3. Work the items per this skill, then **park**: start
   `manuscriptor proc <dir> --wait` as a BACKGROUND task and end your turn.
   The task finishing means new comments are on disk and wakes you.
4. After roughly 20 wakes, or when your context has grown long, exit cleanly
   instead of parking; the outer loop restarts you fresh.

Your working directory is the manuscript, and the git repository root rides
along as an added directory, so producing scripts beside the manuscript
(`analysis/`, `code/`) are editable: a figure comment is usually about one of
them. Never stop the loop because one comment failed; reply with why and
continue. The session dies with the server. `--no-agent` opts out;
`--read-only` implies out.

## Ask, when more than one path is open

A comment often admits several answers, and picking one silently is the failure
the author notices last. On 2026-07-26 a request for "every panel has all three
channels, one interesting case per demographic" was answered by choosing the most
interesting outcome per demographic, which left five of six panels missing a
channel, and by writing a caption that claimed all three anyway. The instruction
was satisfiable for four panels and genuinely impossible for two, and that is
worth a sentence to the author rather than a quiet decision on their behalf.

So: when the paths diverge in a way the author would care about, say so with
`manuscriptor reply` and leave the comment open rather than guessing. A reply is
cheap, arrives in the chat beside the paragraph, and the answer comes back as a
new comment. What deserves the question:

* a constraint that cannot be met for every case ("four of six can, two cannot,
  here is why");
* a trade-off with no dominant side (comparability against interest, coverage
  against significance);
* anything where you are about to write a caption or a sentence that is true of
  most of the exhibit rather than all of it.

Offer the alternatives with the numbers attached, in a small table, and name the
one you would pick. Do not ask about things the comment already decided, and do
not ask twice.

## Exhibits: the conventions this manuscript keeps

**A float goes immediately after the paragraph that first cites it.** HTML has no
floats, so where the float sits in the source is where the reader meets it. A
figure left at the end of a subsection reads as unrelated to the sentence that
introduced it, fifty lines up.

**A file name matches the number the exhibit carries.** `fig3_heatmap.pdf` as
Figure 2 is a trap for whoever reads the producing script next. Renumbering an
exhibit means renaming its output, its `\includegraphics`, and the line in the
script that writes it, in one pass.

**A name says what the exhibit IS, not what it once was.** The six-panel figure
stopped being a heatmap when it was rebuilt; it is `fig2_channels.pdf` now.

**Notes belong in the caption**, where the renderer keeps them with the exhibit.
A note written as a separate paragraph after `\end{tabular}` is detached from its
table the moment anything moves.

**Never hand-edit a generated exhibit.** Edit the script that writes it and
regenerate; the file itself refuses edits for the reason the standing rule gives.
Say which script you ran.

**Report magnitude and inference separately.** This manuscript de-emphasises
thresholds: print the number (`q = 0.015`, `R² = 0.32`) rather than a star, and
define the adjustment once in the caption.
