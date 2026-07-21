# manuscriptor

obsidian: `/Users/bbdaniels/Documents/Obsidian/Manuscriptor`

Read `Manuscriptor/Tasks.md` at session start. The design is `Manuscriptor/plans/2026-07-22 - Phase 1 Design.md`; verified findings and decision rationale are in `Manuscriptor/Technical Notes.md`. Read the design before implementing a milestone, because most of the non-obvious constraints are recorded there rather than in the code.

## What this is

A live manuscript editor. LaTeX renders to a page where every block is addressable back to its source bytes, so a margin comment or a direct edit resolves to a real byte range rather than to a page number and a highlighted phrase.

## Invariants

**Context wide, unit narrow.** A worker may read as widely as it needs and may only ever write one block. This is what makes running edits live acceptable. Do not add a code path that writes more than one block per operation.

**The server has zero knowledge of Claude, and Claude never talks to the server.** They communicate through the `.tex` tree and `comments.jsonl`, and nothing else. Do not add an LLM call inside `manuscriptor/server/`, and do not add an HTTP client to the drain.

**`comments.jsonl` is append only.** Two processes write it. A rewrite introduces conflicts that appending cannot have. State changes are new records, never edits to old ones.

**Block identity is content derived, never positional.** Line numbers move the moment anything above them changes.

**Never hand-edit a generated block.** A file written by analysis code (`tables/*.tex` produced by an R or Stata script) must refuse edits and name its producer instead. Editing it would hardcode a result, which violates the standing rule against typing numbers into LaTeX.

## Testing

Watch every guard fail before trusting it. A test that has never failed proves nothing, and a skipped test is not a pass. The tests that matter most: flatten resolves nested includes with exact offsets; block ids survive an edit above them; sentinels round-trip through pandoc into the correct position; a splice to block N changes only block N's bytes; a comment on block 40 still resolves after block 3 is rewritten.

## Verifying rendered output

Arc is effectively always running, and launching a second instance is prohibited. Probe `curl -s 127.0.0.1:9222/json/version` and connect to the live session via the chrome-devtools MCP. Point a tab at the running server rather than at a file, and call `resize_page` to 1440x900 before screenshotting, since retina defaults exceed the 2000px image limit.

## Test manuscripts

`~/Projects/estonia-ecm/latex` is the reference case: 84KB, `AEA.cls`, tables via `\include`, appendices via `\input`, figures as PNG, 59 `\ref`. Render fidelity has been verified only against it. esttab regression tables and custom preamble macros are untested and either could invalidate M2.
