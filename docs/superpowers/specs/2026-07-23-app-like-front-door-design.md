# Manuscriptor: the app-like front door — design

Date: 2026-07-23
Status: proposed (awaiting review)

## Problem

Manuscriptor already has real Mac-app scaffolding (an `NSApplication`, a window,
a menu with Open… on Cmd-O, Finder open-handling, last-manuscript restore, the
server hidden as a child process). Yet it still *feels* like a terminal tool,
because the front door is a terminal command. The `.app` is built only in
`shell/build/`, never installed; opening it with no document drops a raw
`NSOpenPanel`; and it leans on the pip-installed CLI. The user launches from the
terminal (often via `claude`) out of habit, and so never touches the app
affordances that already exist.

Measured facts that shape the design:

- Server cold start is fast: `manuscriptor --help` is 0.05s, `serve` reaches
  ready in ~0.2s on the demo. Launch latency is **not** the problem, so no
  persistent-daemon workaround is warranted.
- `Cmd-O` → `NSOpenPanel(canChooseDirectories: true)` → `open()` already swaps
  the window to a new manuscript. Project switching is **already built**; it is
  just undiscoverable because the user is not in the app.

## Goal

Make Manuscriptor feel like an app you open: an installed, menubar-resident
launcher that opens your manuscripts by project name (from the Obsidian vault),
by recent, or by folder, with the server invisible.

## Non-goals (explicitly deferred to a later "distribution" project)

- Bundling the Python runtime into the `.app` (py2app/PyInstaller).
- Developer ID signing, notarization, auto-updater.
- Live agent/compile status glyph in the menubar.
- Multi-window (each manuscript in its own window). Stay single-window-swap.

These are the ClaudePrism packaging harvest and are only needed to hand the app
to another person. They are cleanly separable and not required to stop reaching
for the terminal.

## Components

All five share one project/recents list and one `open(path:)` entry point.

### 1. Install to /Applications
`shell/install.sh` (and a `make install` alias): build release, copy
`Manuscriptor.app` into `/Applications`. This is the single change that makes
the icon the front door and puts it in Spotlight. No code change to the app.

### 2. Menubar status item + dynamic lifecycle
- Add an `NSStatusItem` with a **quill icon** (on-brand for Manuscriptor). SF
  Symbols has no true quill, so ship a custom monochrome quill asset in
  `Contents/Resources` and set `image.isTemplate = true` so it auto-adapts to
  the light/dark menu bar; ~18pt. **Click model:** left-click focuses (brings the
  manuscript window forward, or opens the home if none is open, so a click always
  does something); right-click or control-click pops the menu — the shared list
  (Recent + Projects) plus Open Folder… (Cmd-O), Show Window, Quit. The menu is
  popped manually, not bound to the status item, so the two clicks stay distinct.
  Crib the `NSStatusItem` setup pattern from the user's existing ClaudeHUD code.
- **Lifecycle change.** Today: closing the window quits the app, which stops the
  server. New: the app persists in the menubar with no window; closing a
  manuscript window stops its server but does *not* quit. This honors the
  existing invariant — a *server* never holds a *manuscript* with nothing on
  screen — because the server still dies with the window. Only the app's
  quitting decouples. `applicationShouldTerminateAfterLastWindowClosed`
  returns `false`; `applicationWillTerminate` still calls `server.stop()`.
- **Dynamic activation policy.** `.regular` while a manuscript window is open
  (full menu bar so the load-bearing Edit menu key equivalents — Cmd-V into the
  source editor — work); drop to `.accessory` when no window is open so nothing
  lingers in the Dock or Cmd-Tab. Set on window open / last-window close. The
  user does not use the Dock, so the transient Dock icon while editing is
  irrelevant; the requirement is a working menu bar while editing and no clutter
  while idle.

### 3. Home screen on cold open
Replace the cold-open `NSOpenPanel` with a branded `home.html` loaded into the
existing `WKWebView` before any server starts. Lists recent manuscripts and
projects with New / Open Folder. A `WKScriptMessageHandler` bridges clicks back
to Swift (`open(path:)`, `showOpenPanel`, `newManuscript`). Bundled in
`Contents/Resources`; styled to match the viewer so the app has one visual
language. The server stays out of the launch path entirely.

### 4. Recents store → Open Recent
Generalize the existing single `LastManuscript` UserDefaults key into an ordered
recents list (bounded, most-recent-first, delta on each successful `open()`).
Surface it three ways from the one source: a File > Open Recent submenu, the home
screen, and the menubar menu.

### 5. Open Project (vault-sourced)
- New CLI subcommand `manuscriptor projects [--vault PATH]`: scan the vault's
  canonical project model (each `<Project>/Tasks.md` frontmatter `cwds:`),
  resolve each project's manuscript root(s) with the existing `ManuscriptRoot`
  rule, and print JSON: `[{"name": "...", "root": "...", "main": "..."}]`. A
  project whose `cwds` hold two manuscript roots lists both under its name.
- Vault path defaults to `~/Documents/Obsidian`, overridable via `--vault` /
  env / a UserDefaults key. **Soft source:** no vault → empty project list;
  Recent and Open Folder still work. The Swift shell never learns what Obsidian
  is; it shells `manuscriptor projects`, parses JSON, and renders it in the same
  three surfaces as recents.
- Rationale note: hooking the vault personalizes the app to the user's setup
  (the same coupling that makes such a tool non-sellable as-is). That is the
  right trade for a personal tool; keeping it a pluggable, optional source keeps
  the core honest.

## Data flow

Cold open → Swift shells `manuscriptor projects` (~0.2s) + reads recents from
UserDefaults → renders `home.html` in the WebView (no server yet). User picks a
project/recent/folder → Swift `open(path:)` resolves root, spawns
`manuscriptor serve`, loads its localhost URL, prepends to recents. Menubar menu
and File > Open Recent/Open Project render the same two lists on demand.

## Error handling

- `manuscriptor` binary missing: keep the existing located-binary check; the
  home screen shows an inline install/repair affordance instead of the current
  `pip install` code snippet (still no bundling).
- `manuscriptor projects` fails or returns nothing: Open Project section is
  simply absent/empty; the rest of the home works. Never block launch on it.
- A project’s manuscript root no longer exists: skip it in the JSON (validated
  server-side) and prune it from recents on a failed `open()`.

## Testing

- `manuscriptor projects` unit tests: a fixture vault with 0, 1, and 2-manuscript
  projects; a project with no `cwds`; a dangling `cwds` path. Assert JSON shape
  and root resolution. Pure Python, no GUI.
- Recents store: add/dedupe/bound/prune ordering (can be exercised through the
  `--resolve-root`-style headless flag pattern already used in `tests/`).
- Lifecycle: manual check that closing the window leaves the menubar item,
  reopening from it works, and quitting stops the server (assert no orphaned
  `manuscriptor serve` process).
- Existing shell resolution tests (`tests/test_shell.py`) must stay green.

## Build sequence

1. `install.sh` (immediate value, no code risk).
2. `manuscriptor projects` command + tests (pure Python, unblocks the list).
3. Recents store + File > Open Recent (small Swift, reuses `open()`).
4. Home screen (`home.html` + script-message bridge) reading recents + projects.
5. Menubar item + dynamic activation policy + lifecycle flip (the one invariant
   change; do last, with the manual lifecycle check).
