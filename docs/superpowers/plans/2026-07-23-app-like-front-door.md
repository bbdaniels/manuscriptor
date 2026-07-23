# App-Like Front Door Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Manuscriptor open like a Mac app — an installed, menubar-resident (quill) launcher that opens manuscripts by project name (from the Obsidian vault), by recent, or by folder, with the server invisible.

**Architecture:** Keep the Swift-shell-over-Python-server model. Add one CLI subcommand (`manuscriptor projects`) that emits the vault-sourced project list as JSON; the Swift shell shells out to it exactly as it already does for `serve`. A recents list in `UserDefaults` and the vault projects feed three surfaces (a bundled `home.html` in the WebView, a File menu, a menubar `NSStatusItem`), all routed through the existing `open(path:)`. The app persists in the menubar with no window; the server still dies with its window.

**Tech Stack:** Python 3.10+ (argparse CLI, stdlib only for the new command), Swift/AppKit/WebKit shell, pytest.

## Global Constraints

- Python floor: 3.10 (matches `pyproject.toml`). New command uses **stdlib only** — no new dependency for frontmatter parsing.
- The root rule is single-source in `manuscriptor/source/root.py` (`find_root`, `has_documentclass`, `MAIN_NAME`). Reuse it; never re-implement root detection.
- Vault path default: `~/Documents/Obsidian`, overridable via `--vault` and env `MANUSCRIPTOR_VAULT`. Soft source: no vault → empty list, exit 0, never an error.
- Swift shell invariants are tested by grepping the source in `tests/test_shell.py` (the app is not built in CI). Every new Swift invariant gets a grep assertion there.
- The server must never outlive its window (existing invariant, preserved). Only the app's quitting decouples from window close.
- Single-window-swap only. No multi-window, no Python bundling, no signing/notarization/updater, no live status glyph (all deferred).

---

### Task 1: `shell/install.sh` — install the app to /Applications

**Files:**
- Create: `shell/install.sh`
- Test: `tests/test_shell.py` (add `test_install_script_present`)

**Interfaces:**
- Consumes: `shell/build.sh` (existing release build producing `shell/build/Manuscriptor.app`).
- Produces: nothing code-facing; an executable script.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shell.py  (add near BUILD_SH definitions)
INSTALL_SH = SHELL / "install.sh"

def test_install_script_present_and_executable():
    assert INSTALL_SH.exists(), "shell/install.sh must exist"
    assert os.access(INSTALL_SH, os.X_OK), "shell/install.sh must be executable"
    body = INSTALL_SH.read_text()
    assert "build.sh" in body, "install must build first"
    assert "/Applications" in body, "install must copy into /Applications"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shell.py::test_install_script_present_and_executable -v`
Expected: FAIL (file does not exist).

- [ ] **Step 3: Write `shell/install.sh`**

```bash
#!/usr/bin/env bash
# Build a release Manuscriptor.app and install it into /Applications, so the
# app is launchable from Spotlight and the menubar instead of a terminal.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/build.sh" --release
APP="$HERE/build/Manuscriptor.app"
[ -d "$APP" ] || { echo "build did not produce $APP" >&2; exit 1; }
DEST="/Applications/Manuscriptor.app"
rm -rf "$DEST"
cp -R "$APP" "$DEST"
echo "installed -> $DEST"
open -R "$DEST"
```

Then: `chmod +x shell/install.sh`. If `build.sh` does not accept `--release`, drop the flag (check `shell/build.sh` first and match its actual interface).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_shell.py::test_install_script_present_and_executable -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shell/install.sh tests/test_shell.py
git commit -m "feat(shell): install.sh to put Manuscriptor.app in /Applications"
```

---

### Task 2: `manuscriptor projects` — vault-sourced project list as JSON

**Files:**
- Create: `manuscriptor/source/projects.py`
- Modify: `manuscriptor/cli.py` (add `cmd_projects` + `sub.add_parser("projects", ...)` + dispatch)
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `manuscriptor.source.root.find_root(start: Path) -> tuple[Path, str]`, `has_documentclass(path: Path) -> bool`.
- Produces: `list_projects(vault: Path) -> list[dict]` where each dict is `{"name": str, "root": str, "main": str}` (root is an absolute posix path, main is the root's main .tex name or ""). `cmd_projects` prints `json.dumps(list_projects(...))` and returns 0.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_projects.py
from __future__ import annotations
import json
from pathlib import Path
from manuscriptor.source.projects import list_projects, _cwds_from_frontmatter, _manuscript_roots

DOC = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"

def _mk_project(vault: Path, name: str, cwds: list[str]):
    d = vault / name
    d.mkdir(parents=True)
    body = "---\ncwds:\n" + "".join(f"  - {c}\n" for c in cwds) + "---\n# Tasks\n"
    (d / "Tasks.md").write_text(body)

def test_frontmatter_cwds_parses_block_list():
    fm = "---\ncwds:\n  - /a/b\n  - /a/b/**\nreads:\n  - /x\n---\nbody"
    assert _cwds_from_frontmatter(fm) == ["/a/b", "/a/b/**"]

def test_no_frontmatter_returns_empty():
    assert _cwds_from_frontmatter("# Tasks\nno frontmatter") == []

def test_manuscript_roots_finds_documentclass_in_subdir(tmp_path: Path):
    proj = tmp_path / "proj"; (proj / "manuscript").mkdir(parents=True)
    (proj / "manuscript" / "main.tex").write_text(DOC)
    roots = _manuscript_roots(proj)
    assert roots == [proj / "manuscript"]

def test_list_projects_maps_name_to_root(tmp_path: Path):
    vault = tmp_path / "vault"; vault.mkdir()
    code = tmp_path / "estonia-ecm" / "latex"; code.mkdir(parents=True)
    (code / "main.tex").write_text(DOC)
    _mk_project(vault, "Estonia ECM", [str(tmp_path / "estonia-ecm"), str(tmp_path / "estonia-ecm") + "/**"])
    got = list_projects(vault)
    assert got == [{"name": "Estonia ECM", "root": str(code), "main": "main.tex"}]

def test_missing_vault_is_empty_not_error(tmp_path: Path):
    assert list_projects(tmp_path / "nope") == []

def test_project_without_manuscript_is_skipped(tmp_path: Path):
    vault = tmp_path / "v"; vault.mkdir()
    empty = tmp_path / "empty"; empty.mkdir()
    _mk_project(vault, "No Paper", [str(empty)])
    assert list_projects(vault) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_projects.py -v`
Expected: FAIL (`No module named 'manuscriptor.source.projects'`).

- [ ] **Step 3: Write `manuscriptor/source/projects.py`**

```python
"""Vault-sourced project list for the app's "Open Project" surface.

Reads the Obsidian vault's canonical project model: each <Project>/Tasks.md
frontmatter declares `cwds:` (the working directories the project owns). For
each project we find its manuscript root(s) with the shared root rule and emit
{name, root, main}. Stdlib only. A missing vault yields [] — never an error —
so the app degrades to Recent + Open Folder.
"""
from __future__ import annotations

import os
from pathlib import Path

from manuscriptor.source.root import find_root, has_documentclass

# Directories never worth walking when hunting for a manuscript root.
_SKIP = {".git", ".build", "node_modules", "__pycache__", ".venv", "venv",
         "build", "dist", ".obsidian"}
_MAX_DEPTH = 4  # a paper root sits near the top of a repo, not buried deep


def _cwds_from_frontmatter(text: str) -> list[str]:
    """Parse the `cwds:` block-sequence out of YAML frontmatter, stdlib only."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    fm = text[3:end].splitlines()
    out: list[str] = []
    in_cwds = False
    for raw in fm:
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            in_cwds = line.strip() == "cwds:"
            continue
        if in_cwds and line.lstrip().startswith("- "):
            out.append(line.lstrip()[2:].strip())
        elif in_cwds and not line.startswith(" "):
            in_cwds = False
    return out


def _base_dir(cwd: str) -> Path:
    """A cwds entry may be a glob (`/a/b/**`); take the concrete base."""
    stripped = cwd.split("*", 1)[0].rstrip("/")
    return Path(os.path.expanduser(stripped)).resolve()


def _manuscript_roots(base: Path) -> list[Path]:
    """Dirs under `base` (incl. base) holding a .tex with \\documentclass."""
    roots: list[Path] = []
    seen: set[Path] = set()
    if not base.is_dir():
        return roots
    base = base.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        depth = len(d.relative_to(base).parts)
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [x for x in dirnames if x not in _SKIP and not x.startswith(".")]
        for f in filenames:
            if f.endswith(".tex") and has_documentclass(d / f):
                if d not in seen:
                    seen.add(d)
                    roots.append(d)
                break
    return roots


def list_projects(vault: Path) -> list[dict]:
    vault = Path(os.path.expanduser(str(vault)))
    if not vault.is_dir():
        return []
    out: list[dict] = []
    seen_roots: set[Path] = set()
    for tasks in sorted(vault.glob("*/Tasks.md")):
        name = tasks.parent.name
        try:
            cwds = _cwds_from_frontmatter(tasks.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        bases: list[Path] = []
        for c in cwds:
            b = _base_dir(c)
            if b not in bases:
                bases.append(b)
        for b in bases:
            for root in _manuscript_roots(b):
                if root in seen_roots:
                    continue
                seen_roots.add(root)
                _, main = find_root(root)
                out.append({"name": name, "root": root.as_posix(), "main": main})
    return out
```

- [ ] **Step 4: Wire the subcommand in `manuscriptor/cli.py`**

Add the handler (near the other `cmd_*` functions):

```python
def cmd_projects(args: argparse.Namespace) -> int:
    import json
    from manuscriptor.source.projects import list_projects
    vault = args.vault or os.environ.get("MANUSCRIPTOR_VAULT") or "~/Documents/Obsidian"
    print(json.dumps(list_projects(Path(vault))))
    return 0
```

Register the parser (in `main()`, beside the other `sub.add_parser(...)` calls):

```python
    p_projects = sub.add_parser("projects", help="List vault manuscripts as JSON {name, root, main}.")
    p_projects.add_argument("--vault", default=None, help="Vault path (default ~/Documents/Obsidian or $MANUSCRIPTOR_VAULT).")
    p_projects.set_defaults(func=cmd_projects)
```

Confirm `main()` dispatches via `args.func(args)`; if it dispatches on `args.command` with an if/elif chain instead, add `elif args.command == "projects": return cmd_projects(args)` following the file's existing pattern. Ensure `os` and `Path` are imported at module top (they are used elsewhere in the file — verify).

- [ ] **Step 5: Run tests + smoke the command**

Run: `pytest tests/test_projects.py -v`
Expected: PASS (all six).
Run: `manuscriptor projects --vault ~/Documents/Obsidian | python3 -m json.tool | head`
Expected: a JSON array including your real manuscript projects (e.g. Estonia ECM/QBS).

- [ ] **Step 6: Commit**

```bash
git add manuscriptor/source/projects.py manuscriptor/cli.py tests/test_projects.py
git commit -m "feat(cli): manuscriptor projects — vault-sourced manuscript list as JSON"
```

---

### Task 3: Recents store + File > Open Recent (Swift)

**Files:**
- Modify: `shell/Sources/Manuscriptor/AppDelegate.swift` (recents store; Open Recent submenu; record on open)
- Test: `tests/test_shell.py` (add `test_recents_invariants`)

**Interfaces:**
- Consumes: existing `open(path:)`, existing `UserDefaults` key `LastManuscript` (generalized).
- Produces: `recents() -> [String]`, `pushRecent(_ path: String)` (bounded, dedup, most-recent-first), a `File > Open Recent` submenu rebuilt from `recents()`. Later tasks (home, menubar) call `recents()`.

- [ ] **Step 1: Write the failing test (source-invariant grep, matching this file's style)**

```python
# tests/test_shell.py
def test_recents_invariants():
    src = (SOURCES / "AppDelegate.swift").read_text()
    assert "RecentManuscripts" in src, "recents must use a dedicated UserDefaults key"
    assert "func pushRecent" in src and "func recents" in src
    assert "Open Recent" in src, "File menu must offer Open Recent"
    # bounded so the list cannot grow without limit
    assert "prefix(" in src, "recents must be bounded"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_shell.py::test_recents_invariants -v`
Expected: FAIL.

- [ ] **Step 3: Implement recents in AppDelegate.swift**

Add a store (place near the `lastKey` static):

```swift
    private static let recentsKey = "RecentManuscripts"
    private static let recentsMax = 12

    func recents() -> [String] {
        let raw = UserDefaults.standard.stringArray(forKey: AppDelegate.recentsKey) ?? []
        return raw.filter { FileManager.default.fileExists(atPath: $0) }
    }

    func pushRecent(_ path: String) {
        var list = UserDefaults.standard.stringArray(forKey: AppDelegate.recentsKey) ?? []
        list.removeAll { $0 == path }
        list.insert(path, at: 0)
        list = Array(list.prefix(AppDelegate.recentsMax))
        UserDefaults.standard.set(list, forKey: AppDelegate.recentsKey)
    }
```

In `open(path:)`, after the successful `UserDefaults.standard.set(resolved.root.path, forKey: AppDelegate.lastKey)` line, add:

```swift
        pushRecent(resolved.root.path)
        rebuildOpenRecentMenu()
```

Add the submenu builder and a store for the item, and call it from `buildMenu()`:

```swift
    private var openRecentItem: NSMenuItem?

    private func rebuildOpenRecentMenu() {
        let submenu = NSMenu(title: "Open Recent")
        for path in recents() {
            let title = (path as NSString).lastPathComponent
            let item = NSMenuItem(title: title, action: #selector(openRecentPath(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = path
            submenu.addItem(item)
        }
        openRecentItem?.submenu = submenu
        openRecentItem?.isEnabled = !recents().isEmpty
    }

    @objc private func openRecentPath(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        openHandled = true
        open(path: path)
    }
```

In `buildMenu()`, after the "Open…" item in the File menu:

```swift
        let recent = NSMenuItem(title: "Open Recent", action: nil, keyEquivalent: "")
        fileMenu.addItem(recent)
        openRecentItem = recent
        rebuildOpenRecentMenu()
```

- [ ] **Step 4: Run the invariant test + build**

Run: `pytest tests/test_shell.py::test_recents_invariants -v` → PASS.
Run: `cd shell && ./build.sh` → builds without error.

- [ ] **Step 5: Commit**

```bash
git add shell/Sources/Manuscriptor/AppDelegate.swift tests/test_shell.py
git commit -m "feat(shell): recents store + File > Open Recent"
```

---

### Task 4: Home screen on cold open (home.html + JS↔Swift bridge)

**Files:**
- Create: `shell/Resources/home.html`
- Modify: `shell/Sources/Manuscriptor/AppDelegate.swift` (load home instead of `NSOpenPanel` on cold open; add `WKScriptMessageHandler`; feed recents+projects in)
- Modify: `shell/Package.swift` and/or `shell/build.sh` if resources need declaring (verify how `Info.plist` and existing resources are copied into the bundle)
- Test: `tests/test_shell.py` (`test_home_screen_invariants`)

**Interfaces:**
- Consumes: `recents() -> [String]`; shells `manuscriptor projects` for `[{name,root,main}]`.
- Produces: a cold-open home surface; message handlers `open` (path), `openPanel`, and `openProject` (root) routed to `open(path:)` / `showOpenPanel`.

- [ ] **Step 1: Write the failing test**

```python
def test_home_screen_invariants():
    src = (SOURCES / "AppDelegate.swift").read_text()
    home = SHELL / "Resources" / "home.html"
    assert home.exists(), "bundled home.html must exist"
    assert "WKScriptMessageHandler" in src
    assert "manuscriptor" in home.read_text() or "ms.open" in home.read_text()
    # cold open loads the home, not a bare NSOpenPanel
    assert "loadHome" in src, "cold open must present the home surface"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_shell.py::test_home_screen_invariants -v` → FAIL.

- [ ] **Step 3: Create `shell/Resources/home.html`**

A self-contained page (no external assets, matching the viewer's neutral look). It receives its data via `window.__ms_data__` injected by Swift, and posts actions back over `window.webkit.messageHandlers.ms`.

```html
<!doctype html><html><head><meta charset="utf-8"><style>
  :root{color-scheme:light dark}
  body{font:14px -apple-system,system-ui,sans-serif;margin:0;padding:32px;
       background:#faf9f7;color:#1c1917}
  @media (prefers-color-scheme:dark){body{background:#1c1917;color:#faf9f7}}
  h1{font:600 22px Georgia,serif;margin:0 0 4px}
  .sub{color:#78716c;margin:0 0 24px}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#78716c;margin:24px 0 8px}
  .row{padding:8px 10px;border-radius:8px;cursor:pointer;display:flex;justify-content:space-between}
  .row:hover{background:rgba(120,113,108,.15)}
  .row .path{color:#a8a29e;font-size:12px}
  .actions{margin-top:24px}
  button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #d6d3d1;
         background:transparent;color:inherit;cursor:pointer;margin-right:8px}
</style></head><body>
  <h1>Manuscriptor</h1><p class="sub">Open a manuscript</p>
  <div id="projects"></div>
  <div id="recents"></div>
  <div class="actions"><button onclick="ms('openPanel')">Open Folder…</button></div>
<script>
  function ms(action, arg){ window.webkit.messageHandlers.ms.postMessage({action:action, arg:arg||""}); }
  function section(elId, title, items, key){
    var host=document.getElementById(elId); if(!items.length) return;
    var h=document.createElement('h2'); h.textContent=title; host.appendChild(h);
    items.forEach(function(it){
      var row=document.createElement('div'); row.className='row';
      var label=key==='project'? it.name : it.replace(/^.*\//,'');
      var path = key==='project'? it.root : it;
      row.innerHTML='<span>'+label+'</span><span class="path">'+path+'</span>';
      row.onclick=function(){ ms('open', path); };
      host.appendChild(row);
    });
  }
  var d = window.__ms_data__ || {projects:[],recents:[]};
  section('projects','Projects', d.projects, 'project');
  section('recents','Recent', d.recents, 'recent');
</script></body></html>
```

- [ ] **Step 4: Load the home + bridge in AppDelegate.swift**

Register the handler in `buildWindow()` (the `WKWebViewConfiguration` block), before creating the WebView:

```swift
        config.userContentController.add(self, name: "ms")
```

Add conformance `WKScriptMessageHandler` to the class declaration and implement:

```swift
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "ms", let body = message.body as? [String: Any],
              let action = body["action"] as? String else { return }
        let arg = body["arg"] as? String ?? ""
        switch action {
        case "open" where !arg.isEmpty: openHandled = true; open(path: arg)
        case "openPanel": showOpenPanel(nil)
        default: break
        }
    }
```

Add the home loader:

```swift
    private func loadHome() {
        guard let url = Bundle.main.url(forResource: "home", withExtension: "html") else {
            showOpenPanel(nil); return   // fallback keeps the app usable
        }
        let projectsJSON = ServerProcess.projectsJSON()          // see below, "[]" on failure
        let recentsJSON = (try? JSONSerialization.data(withJSONObject: recents()))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        let inject = "window.__ms_data__={projects:\(projectsJSON),recents:\(recentsJSON)};"
        let js = WKUserScript(source: inject, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        webView.configuration.userContentController.addUserScript(js)
        webView.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
    }
```

Replace the cold-open `self.showOpenPanel(nil)` branch in `applicationDidFinishLaunching` with `self.loadHome()`.

Add a synchronous helper to `ServerProcess.swift` that shells the projects command (reusing `locateBinary()`):

```swift
    /// `manuscriptor projects` as a JSON string; "[]" on any failure so the
    /// home always renders.
    static func projectsJSON() -> String {
        guard let bin = locateBinary() else { return "[]" }
        let p = Process(); p.executableURL = bin; p.arguments = ["projects"]
        p.environment = childEnvironment()
        let out = Pipe(); p.standardOutput = out; p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return "[]" }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let s = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return (p.terminationStatus == 0 && s.hasPrefix("[")) ? s : "[]"
    }
```

`childEnvironment()` and `locateBinary()` already live on `ServerProcess`; `projectsJSON()` is added to the same type in the same file, so it can call them even though `childEnvironment()` is `private` (Swift `private` allows same-type, same-file access). No visibility change needed.

- [ ] **Step 5: Verify resources ship in the bundle**

Check `shell/build.sh` / `shell/Package.swift`: `home.html` under `shell/Resources/` must be copied into `Manuscriptor.app/Contents/Resources/`. Follow however `Info.plist` gets there. Add a copy line if the build copies resources explicitly.

- [ ] **Step 6: Run tests + manual check**

Run: `pytest tests/test_shell.py::test_home_screen_invariants -v` → PASS.
Run: `cd shell && ./build.sh && open build/Manuscriptor.app` → launching with no last-manuscript shows the home listing your projects and recents; clicking one opens it.

- [ ] **Step 7: Commit**

```bash
git add shell/Resources/home.html shell/Sources/Manuscriptor/AppDelegate.swift shell/Sources/Manuscriptor/ServerProcess.swift tests/test_shell.py
git commit -m "feat(shell): home screen on cold open with vault projects + recents"
```

---

### Task 5: Menubar quill + dynamic activation policy + lifecycle flip

**Files:**
- Modify: `shell/Sources/Manuscriptor/AppDelegate.swift` (NSStatusItem, menu, policy transitions, terminate-on-close flip)
- Create: `shell/Resources/quill.pdf` (monochrome template image; a simple vector quill)
- Test: `tests/test_shell.py` (`test_menubar_and_lifecycle_invariants`)

**Interfaces:**
- Consumes: `recents()`, `ServerProcess.projectsJSON()`, `open(path:)`, `showOpenPanel`.
- Produces: an always-present menubar item; window-open → `.regular`, last-window-close → `.accessory` + app stays alive.

- [ ] **Step 1: Write the failing test**

```python
def test_menubar_and_lifecycle_invariants():
    src = (SOURCES / "AppDelegate.swift").read_text()
    assert "NSStatusItem" in src, "must add a menubar status item"
    assert "quill" in src, "menubar uses the quill template image"
    assert "isTemplate = true" in src, "menubar image must be a template image"
    # left-click focuses, right/control-click pops the menu (menu not bound to item)
    assert "sendAction(on:" in src or "rightMouseUp" in src, "clicks must be differentiated"
    assert "popUp" in src, "right-click must pop the project/recents menu"
    assert "setActivationPolicy(.accessory)" in src and "setActivationPolicy(.regular)" in src, \
        "activation policy must switch regular<->accessory"
    # the app no longer quits when the last window closes
    assert "applicationShouldTerminateAfterLastWindowClosed" in src
    assert "return false" in src.split("applicationShouldTerminateAfterLastWindowClosed", 1)[1][:120], \
        "must persist in the menubar after the window closes"
    # the server invariant is preserved: still stopped on window close
    assert "windowWillClose" in src or "server.stop()" in src
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_shell.py::test_menubar_and_lifecycle_invariants -v` → FAIL.

- [ ] **Step 3: Add the quill template image**

Create `shell/Resources/quill.pdf` as a small monochrome vector quill (black on transparent, ~18×18pt). If generating vector art is impractical in-session, ship a single-color PNG named `quill.png` at 36×36 (retina) instead and load that; keep it pure black with alpha so `isTemplate` recolors it. Update the test's extension expectation accordingly.

- [ ] **Step 4: Add the status item + policy + lifecycle in AppDelegate.swift**

Add a stored item and builder, called from `applicationDidFinishLaunching`:

```swift
    private var statusItem: NSStatusItem?

    private func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let img = NSImage(named: "quill") ?? Bundle.main.image(forResource: "quill") {
            img.isTemplate = true
            img.size = NSSize(width: 18, height: 18)
            item.button?.image = img
        } else {
            item.button?.title = "✒"
        }
        // Differentiated clicks: left = focus the work, right/control-click = the
        // menu. So we do NOT set item.menu (that pops the menu on ANY click);
        // we take a button action and inspect the event instead.
        item.button?.target = self
        item.button?.action = #selector(statusClicked(_:))
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        statusItem = item
    }

    @objc private func statusClicked(_ sender: Any?) {
        let ev = NSApp.currentEvent
        let wantsMenu = ev?.type == .rightMouseUp
            || (ev?.modifierFlags.contains(.control) ?? false)
        if wantsMenu, let button = statusItem?.button {
            let menu = buildStatusMenu()
            menu.popUp(positioning: nil,
                       at: NSPoint(x: 0, y: button.bounds.height + 4),
                       in: button)
        } else {
            showWindow(nil)   // left-click: focus the window, or open the home
        }
    }

    private func buildStatusMenu() -> NSMenu {
        let menu = NSMenu()
        for it in projectsMenuItems() { menu.addItem(it) }
        for path in recents() {
            let m = NSMenuItem(title: (path as NSString).lastPathComponent,
                               action: #selector(openRecentPath(_:)), keyEquivalent: "")
            m.target = self; m.representedObject = path; menu.addItem(m)
        }
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open Folder…", action: #selector(showOpenPanel(_:)), keyEquivalent: "")
        open.target = self; menu.addItem(open)
        let show = NSMenuItem(title: "Show Window", action: #selector(showWindow(_:)), keyEquivalent: "")
        show.target = self; menu.addItem(show)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Manuscriptor", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        return menu
    }
```

Add a projects-menu helper (reuses `projectsJSON()`), rebuild the status menu when it opens so it stays fresh (implement `NSMenuDelegate.menuNeedsUpdate` on the status menu, or rebuild in `showWindow`/on open). Keep it simple: rebuild the whole status menu in `buildStatusItem` at launch and again after each successful `open(path:)`.

```swift
    private func projectsMenuItems() -> [NSMenuItem] {
        guard let data = ServerProcess.projectsJSON().data(using: .utf8),
              let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
        else { return [] }
        return arr.compactMap { p in
            guard let name = p["name"] as? String, let root = p["root"] as? String else { return nil }
            let m = NSMenuItem(title: name, action: #selector(openProject(_:)), keyEquivalent: "")
            m.target = self; m.representedObject = root; return m
        }
    }
    @objc private func openProject(_ sender: NSMenuItem) {
        guard let root = sender.representedObject as? String else { return }
        openHandled = true; open(path: root)
    }
    @objc private func showWindow(_ sender: Any?) {
        NSApp.setActivationPolicy(.regular)
        if let w = window { w.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
        else { loadHome(); window?.makeKeyAndOrderFront(nil) }
    }
```

**Lifecycle flip.** First, in `buildWindow()` add `window.isReleasedWhenClosed = false` so the window survives closing and can be reshown from the menubar (the app quit on close before, so this never mattered). Then change `applicationShouldTerminateAfterLastWindowClosed` to return `false`. Preserve the server invariant by stopping the server and dropping to accessory when the window closes — implement `NSWindowDelegate.windowWillClose` (set `window.delegate = self` in `buildWindow`, add `NSWindowDelegate` conformance):

```swift
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func windowWillClose(_ notification: Notification) {
        server.stop()                 // the server never outlives its window
        currentRoot = nil
        serverURL = nil
        NSApp.setActivationPolicy(.accessory)   // live on as a menubar launcher
    }
```

In `open(path:)`, when a server is (re)started for a real manuscript, ensure the app is `.regular`:

```swift
        NSApp.setActivationPolicy(.regular)
```

(Place it right before `server.start(...)`.) Call `buildStatusItem()` at the end of `applicationDidFinishLaunching`.

- [ ] **Step 5: Run the invariant test + build + manual lifecycle check**

Run: `pytest tests/test_shell.py::test_menubar_and_lifecycle_invariants -v` → PASS.
Run: `cd shell && ./build.sh && open build/Manuscriptor.app`.
Manual: (a) quill appears in the menubar; (b) open a manuscript from it, the window shows and the Dock/menu bar are normal while editing; (c) close the window — the app stays alive as just the quill, and `pgrep -fl "manuscriptor serve"` shows **no** orphaned server; (d) click the quill, reopen a manuscript, it works.

- [ ] **Step 6: Run the full shell + projects suite, then commit**

Run: `pytest tests/test_shell.py tests/test_projects.py -v` → all PASS.

```bash
git add shell/Sources/Manuscriptor/AppDelegate.swift shell/Resources/quill.pdf tests/test_shell.py
git commit -m "feat(shell): menubar quill launcher, dynamic activation policy, persist on window close"
```

---

## Final integration

- [ ] Run `pytest -q` (whole suite) to confirm nothing regressed, especially `tests/test_shell.py` root-rule parity and `tests/test_root.py`.
- [ ] `shell/install.sh` to put the built app in `/Applications`; launch from Spotlight and confirm the terminal is no longer needed for any of: open by project, open recent, open folder, switch project.
- [ ] Push the branch and open a PR against `main`:

```bash
git push -u origin feat/app-like-front-door
gh pr create --fill --base main
```
