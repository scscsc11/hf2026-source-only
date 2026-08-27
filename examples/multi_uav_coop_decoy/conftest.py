"""Pytest conftest — ensure repo root + example dir are importable.

Pytest's rootdir insertion happens after test-module collection, so
intra-example `from search_track...` imports fail without this fixture.
We inject both paths at the very start of collection (session-scoped).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent           # examples/multi_uav_coop_decoy
REPO_ROOT = HERE.parents[2]                       # repo root

for p in (str(REPO_ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
