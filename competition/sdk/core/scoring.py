"""Scoring — re-export the Spec 025 cooperative-tracking evaluator.

Re-exports the vendored copy of ``coop_eval`` (byte-equivalent to
``examples/_common/coop_eval.py``) so the SDK stays self-contained. The
scoring engine is the judge's concern and MAY use ground truth (see
contracts/isolation.md §5); it is physically separate from the player's
Observation data flow.
"""
from __future__ import annotations

from .._vendored.coop_eval import (
    CoopTrackingEvaluator,
    ScoringProfile,
    profile_adversarial_swarm_search,
    profile_multi_uav_coop_decoy,
    profile_uav_search_track_car,
)

__all__ = [
    "CoopTrackingEvaluator",
    "ScoringProfile",
    "profile_adversarial_swarm_search",
    "profile_multi_uav_coop_decoy",
    "profile_uav_search_track_car",
]
