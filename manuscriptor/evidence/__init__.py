"""The citation-provenance pipeline, absorbed from cite-evidence.

Five resumable stages, each emitting a JSON artifact: parse, resolve, fetch,
extract, render. This is working code, not a stub. It is ported verbatim apart
from import paths so that `manuscriptor evidence` behaves exactly like
`cite-evidence build` did on day one of the new repo.

`parse.py` and `render.py` are the two modules slated for dismantling. M1 moves
source walking into `manuscriptor.source`, M2 moves pandoc invocation and HTML
augmentation into `manuscriptor.render`, and this package keeps only what is
genuinely about citations: resolve, fetch, extract.
"""
