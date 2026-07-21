"""Source-side passes: flatten, segment, anchor, splice.

This package owns the mapping between rendered output and manuscript bytes.
Everything else in Manuscriptor depends on that mapping being exact, so these
modules are the ones to keep small and heavily tested.
"""
