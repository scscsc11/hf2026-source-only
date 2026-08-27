"""Config loader for 017 — reuses 016's AlgorithmConfig + from_yaml.

017 adds one new top-level field (coop_broadcast_period) which we surface
via a thin wrapper so the rest of the 016 machinery (range checks,
defaults) is reused unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from examples.uav_search_track_car.search_track.config import (
    AlgorithmConfig, DEFAULTS as BASE_DEFAULTS, from_yaml as base_from_yaml,
)


def load_algorithm_config(path: str | Path) -> AlgorithmConfig:
    """Load 017 algorithm.yaml, reusing 016's loader.

    The 017 yaml adds several top-level cooperation/search fields beyond
    016's schema; we surface them through ``AlgorithmConfig.advanced`` so
    the rest of the 016 validation/reuse path stays untouched and
    CoopController reads them via ``cfg.get(...)``.
    """
    cfg = base_from_yaml(path)
    # Re-read the raw yaml to pick up 017-only top-level keys and merge
    # them into advanced (where CoopController.configure looks).
    p = Path(path)
    with p.open("r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}
    _017_TOP_KEYS = (
        "coop_broadcast_period",
        "use_sector_search",
        "expand_time",
        "search_sweep_time",
        "sector_angular_speed_dps",
        "initial_radius_frac",
        "radius_dither_frac",
        "sector_center_latitude",
        "sector_center_longitude",
        "cooperative_summon",
        "dwell_target_s",
        "sector_commitment",
    )
    for k in _017_TOP_KEYS:
        if k in raw:
            cfg.advanced[k] = raw[k]
    return cfg
