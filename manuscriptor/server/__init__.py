"""The local server: HTTP, websocket, file watching, comment log.

Two invariants define this package, and both are load-bearing.

The server has zero knowledge of Claude. It renders, serves, watches the tree,
applies direct human edits as byte-range splices, and appends to the comment
log. Nothing agentic happens here, so it stays testable and carries no LLM
dependency.

Claude never talks to the server. A Claude Code session shares the filesystem,
reads new records from `comments.jsonl`, edits `.tex` files with its ordinary
tools, and appends state records. The watcher notices and pushes the redraw.

The two communicate through exactly two things: the `.tex` tree and
`comments.jsonl`. That is the whole interface.
"""
