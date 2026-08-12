#!/bin/sh
# Build the published demo page: examples/demo-paper -> docs/demo/index.html.
#
# The page is the real renderer's output, banner and all. The banner is the only
# thing added here, and it is added by replacement rather than by appending, so
# running this twice leaves one banner rather than two.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$here"

# Compile first, and not for the PDF. `build` resolves \ref from an .aux, so a
# demo built without one ships "Table ??" and "Section ??" on the page -- which
# is what this did before the compile was added. The .aux lands in the hidden
# cache, where git ignores it, and the resolved numbers are baked into the HTML.
manuscriptor compile examples/demo-paper --pdf

manuscriptor build examples/demo-paper --output docs/demo

# The compile delivers a PDF beside the .tex on success. The deliverable here is
# the page, so the byproduct goes rather than sitting untracked in the tree.
rm -f examples/demo-paper/main.pdf

python3 - "$here/docs/demo/index.html" "$here" <<'PY'
"""Put the banner immediately after <body>, replacing any banner already there.

Idempotent by construction: the block is delimited by markers and every run
strips what the markers enclose before inserting. `manuscriptor build` rewrites
index.html anyway, so the strip is belt and braces -- but a banner that stacks
is exactly the failure a marker-less append would produce the first time the
build step was skipped.

The styling borrows the page's own variables rather than restating colours, so
the banner follows the light and dark palettes the renderer already ships.
`.app` is `height: 100vh` under a `body` that never scrolls, so a banner placed
above it would push the interface off the bottom of the window. Body becomes the
flex column and the app takes what is left.
"""
import re
import sys

START = "<!-- demo-banner:start -->"
END = "<!-- demo-banner:end -->"

BANNER = f"""{START}
<style>
  body {{ display: flex; flex-direction: column; height: 100vh; height: 100dvh; }}
  #demo-banner {{
    flex: none; padding: 1rem 1.25rem;
    background: var(--ground); color: var(--ink);
    border-bottom: 1px solid var(--rule); font-family: var(--ui);
  }}
  #demo-banner h1 {{ margin: 0; font-size: 1.15rem; font-weight: 600; letter-spacing: -.01em; }}
  #demo-banner .sub {{ margin: .15rem 0 0; font-size: .9rem; color: var(--muted); }}
  #demo-banner .note {{ margin: .4rem 0 0; font-size: .82rem; color: var(--faint); }}
  #demo-banner a {{ color: var(--accent); }}
  .app {{ height: auto; flex: 1 1 auto; min-height: 0; }}
</style>
<header id="demo-banner">
  <h1>Manuscriptor</h1>
  <p class="sub">AI-native manuscript, code, and citation co-development</p>
  <p class="note">A synthetic manuscript with a real agent session; every word is invented.
    <a href="https://github.com/bbdaniels/manuscriptor">https://github.com/bbdaniels/manuscriptor</a></p>
</header>
{END}"""

path, repo = sys.argv[1], sys.argv[2].rstrip("/")
with open(path, encoding="utf-8") as fh:
    html = fh.read()

# The title bar prints the manuscript's path on disk, which is right when you
# are serving your own paper and wrong on a page published to the web: it would
# show whichever directory the release happened to be built in. Say where the
# file lives in the repository instead.
html = html.replace(repo + "/", "")

# Drop any banner a previous run left, markers included.
html = re.sub(re.escape(START) + ".*?" + re.escape(END), "", html, flags=re.S).strip()

if "<body>" not in html:
    sys.exit("no <body> in the built page; the template changed shape")
html = html.replace("<body>", "<body>\n" + BANNER, 1)

with open(path, "w", encoding="utf-8") as fh:
    fh.write(html + "\n")

print(f"banner injected -> {path}")
PY
