"""Vendored copies of the shared evaluation/runtime helpers.

These are byte-equivalent to ``examples/_common/`` (plus a vendored
``geometry`` module) so the competition SDK is self-contained for release:
it imports ONLY from within ``competition/`` and never touches
``examples/``. If the upstream ``examples/_common`` modules improve, re-copy
them here and re-run the tests.
"""
