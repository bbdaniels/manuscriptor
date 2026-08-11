# shell

`Manuscriptor.app`: a window, a `WKWebView`, a menu bar, and the server as a
child process, so double-clicking a `.tex` file works and quitting cleans up.
About 300 lines of Swift across four files.

The server is the product and this is a client. Any number of clients can attach
to the same port, which is what lets the author work in this window while a
verification pass drives the same page through devtools. Nothing here renders,
parses, or edits: it starts a process, loads a URL, and gets out of the way.

## Build

```bash
./build.sh          # -> shell/build/Manuscriptor.app
```

Swift Package Manager produces a plain executable and the script wraps it in the
bundle, because the only thing an `.app` adds over the command is the
`Info.plist` that declares `.tex` to LaunchServices. The signature is ad hoc,
which is enough to launch on the machine that built it. The script also runs
`lsregister`, so Open With offers Manuscriptor without a logout.

There is no checked-in Xcode project. `Package.swift` and `build.sh` are the
whole build.

## Install

Move or symlink the bundle into `/Applications` if you want it in Spotlight.
Neither is required: the app runs from `shell/build`.

The app runs `manuscriptor serve` as a child, so the command has to be
installed (`pip install -e .` from a clone of this repository). A Finder-launched app
inherits `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, so the app searches
`~/.local/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, and every
`~/Library/Python/*/bin` itself, and hands the same directories to the child so
that pandoc and pdftotext resolve. If the command lives somewhere else:

```bash
defaults write com.bbdaniels.manuscriptor ServerBinary /path/to/manuscriptor
```

When it cannot be found the window says so and names the fix. A blank web view
is a failure, so every failure path loads a page instead of nothing.

## Open With

Right click any `.tex` file, choose Manuscriptor, and the manuscript opens with
the page on that file.

The file is not what gets served. Opening `appendix/e_data_details.tex` and
getting a preamble-less fragment with no bibliography would make the feature
feel broken on the first thing anyone tries, so the app walks up from the file
looking for a directory that holds `main.tex`, or exactly one `.tex` that
declares a document class. Two candidates is not a root: guessing between them
would silently serve the wrong paper, so the walk continues. It stops at a
repository root, because a `main.tex` outside the repository belongs to someone
else. When nothing above the file is a manuscript, the file's own directory is
served and the server's diagnostics say what is missing.

Once the root is serving, the page jumps to the file that was opened. Opening a
second file from the same manuscript jumps again rather than restarting the
server.

The rule lives in `Sources/Manuscriptor/ManuscriptRoot.swift` and, a second
time, in `resolve_root.py`. The duplication is deliberate and guarded:
`tests/test_shell.py` runs both over one shared table of cases and fails when
they disagree, so the Swift can be tested without a GUI.

## The server is owned, not shared

On open the app runs `manuscriptor serve <root> --no-window --port 0`, reads the
port off the child's stdout, and loads it. On quit it terminates the child and
waits for it. A server outliving its window is a process quietly holding a
manuscript open, still watching the tree and still writing on save, with nothing
on screen to say so. Closing the window quits the app, `SIGTERM` and `SIGINT`
are routed through `terminate`, and both paths were checked by killing the app
and looking for the orphan.

The child's environment carries `PYTHONUNBUFFERED=1`. Python block-buffers a
piped stdout, and without it the banner never arrives: measured at zero bytes
after twelve seconds against the demo manuscript. The root fix belongs in
`server/app.py`, whose banner `print` should flush, and that file is not this
track's to edit.

## Menu

About, Open, Close Window, Quit, Reload, at their standard shortcuts, plus an
Edit menu. The Edit menu is not decoration: `WKWebView` takes Cut, Copy, Paste,
and Select All from the main menu's key equivalents, and without it the source
editor cannot be pasted into.

`isInspectable` is on, so right click gives Inspect Element and Web Inspector
works as it does in a browser. Window size and position are remembered between
launches under the autosave name `ManuscriptorMainWindow`.

## Flags

Three, all for verification, none needed in normal use.

```bash
Manuscriptor --resolve-root <path>       # root, main .tex, jump target
Manuscriptor --parse-port "<line>"       # the port read off a banner line
Manuscriptor --snapshot out.png <path>   # render, photograph the page, quit
```

`--snapshot` exists because `screencapture` needs Screen Recording consent that
an automated session does not have, and "it compiled" is not evidence that
anything reached the screen. A web view can photograph itself.

## What is tested and what is not

`tests/test_shell.py` covers the root rule case by case, the banner parsing, the
`.tex` declaration in `Info.plist`, the build script, and the Swift and Python
resolutions agreeing. Every one of those guards was broken on purpose and
watched to fail.

Rendering, the jump, window persistence, and the child dying with its parent are
not unit tested. They were checked by running the app against a scratch copy of
a real manuscript and reading the result.
