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

**1. Read what is pending.**

```bash
manuscriptor proc <manuscript-dir>
```

Each item gives you the comment, the block it sits on, its section, its
neighbours on either side, its citations and computed values, and the file and
line range. Items are marked either `EDIT` or `READ ONLY`.

**2. Take one at a time, and mark it before you start.**

```bash
manuscriptor state <manuscript-dir> c-0007 working
```

The author sees that state in the margin. Skipping it means edits appear under
them with no warning, which is worse than batching.

**3. Read as widely as you need. Change exactly one block.**

This is the constraint the whole design rests on. Read the section, the
neighbouring paragraphs, the bibliography, the table being referenced, the
script behind a number: whatever the comment requires. Then write **one block**,
the one named under `EDIT`.

A worker that can read everything and change one paragraph cannot silently
wreck a paper. Do not fix a second thing you noticed. Leave a comment of your
own if it matters, or tell the author when you report back.

**4. Edit the file at the given range**, using Edit as usual. You are editing
LaTeX source, so `\input{...}` directives in the block are the author's results
pulled in from analysis code. **Never resolve one to its value.** That would
hardcode a result, which is the single thing this tool exists to prevent.

**5. Mark it done.**

```bash
manuscriptor state <manuscript-dir> c-0007 done
```

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
