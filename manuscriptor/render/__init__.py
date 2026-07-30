"""Render-side passes: table normalization, pandoc invocation, cross-reference
resolution, postprocess.

`tables.py` is the one home of LaTeX column-type normalization, for this package
and for the Word submission skills, which import it rather than carrying a copy.

The pandoc invocation and the HTML augmentation logic both already exist in
working form inside `manuscriptor.evidence` (ported from cite-evidence). M2
moves them here and generalizes the augmentation from citation spans to every
block.
"""
