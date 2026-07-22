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

**6. Mark it done.**

```bash
manuscriptor state <manuscript-dir> c-0007 done
```

The ticker shows the edit landing, not the claim that it would: a `done` with no
change behind it reads differently from one with a patch after it. Do not mark
work done that did not happen.

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

## Running live instead of on demand

To have comments addressed as they are written rather than when asked, run the
watcher as a background job:

```bash
manuscriptor proc <manuscript-dir> --wait
```

It blocks until a comment hits disk and then exits, and that exit is what wakes
your session. When you wake, run the procedure above and start another watcher.
The session doing this should be doing nothing else, so it does not compete with
other work.

Each wake costs a turn, so a comment resolves in about a minute. Live here means
the author never has to ask, not that it happens while they watch the cursor.

The author can start both halves at once:

```bash
manuscriptor serve <manuscript-dir> --with-agent
```

which serves the page and runs that loop beside it. It refuses to combine with
`--read-only`, and the session dies with the server.
