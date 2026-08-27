"""OpenSim examples — shared glue layer (Spec 020 / T11).

Consolidates the duplicated plumbing across the four example runners:
  * subprocess sim launch + teardown
  * target-trajectory load/inject (Feature 007 auto-static)
  * Redis pubsub subscribe template
  * standard argparser + path bootstrap
  * metrics summary write-out

Importing this package adds neither the example dir nor the repo root to
``sys.path`` on its own — call :func:`bootstrap_paths` (or the example's
own bootstrap) for that, then import from here.
"""
