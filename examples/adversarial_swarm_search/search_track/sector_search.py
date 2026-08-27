"""Spec 019 US3 (FR-004) — Sector search for idle (non-tracking) UAVs.

Re-exports the 017 sector-search geometry by loading 017's
`sector_search.py` directly from disk (NOT via `importlib.import_module`,
which would resolve to this very file and cause a circular import).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load 017 sector_search.py by file path to avoid the circular import
# (the local package is also called `search_track`, so a plain
# `import search_track.sector_search` resolves to *this* file).
_017_FILE = (Path(__file__).resolve().parents[2] / "multi_uav_coop_decoy"
             / "search_track" / "sector_search.py")
_spec = importlib.util.spec_from_file_location(
    "_osim_017_sector_search", str(_017_FILE))
_ss_017 = importlib.util.module_from_spec(_spec)
sys.modules["_osim_017_sector_search"] = _ss_017
_spec.loader.exec_module(_ss_017)

SectorSearchParams = _ss_017.SectorSearchParams
sector_bearing = _ss_017.sector_bearing
sector_radius = _ss_017.sector_radius
sector_waypoint = _ss_017.sector_waypoint
destination_point = _ss_017.destination_point
point_bearing_from = _ss_017.point_bearing_from
point_radius_from = _ss_017.point_radius_from


def assign_sectors(alive_uids):
    """Map each alive uid -> sector index (0..n-1)."""
    n = max(1, len(alive_uids))
    return {uid: i % n for i, uid in enumerate(alive_uids)}


__all__ = [
    "SectorSearchParams", "sector_bearing", "sector_radius",
    "sector_waypoint", "destination_point", "point_bearing_from",
    "point_radius_from", "assign_sectors",
]

