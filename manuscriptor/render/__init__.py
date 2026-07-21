"""Render-side passes: pandoc invocation, cross-reference resolution, postprocess.

The pandoc invocation and the HTML augmentation logic both already exist in
working form inside `manuscriptor.evidence` (ported from cite-evidence). M2
moves them here and generalizes the augmentation from citation spans to every
block.
"""
