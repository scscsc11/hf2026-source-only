"""UAV -> target resolution by nearest-neighbour position matching.

The C++ gimbal_tracking component publishes ``detection.target_position``
(the locked target's true position when ``detected=True``) but **not** the
target's uid (see ``src/components/gimbal_tracking_component.cc`` state(),
lines 388-427). To answer "which target is each UAV tracking" — needed by
the cooperative continuous-tracking evaluator (:mod:`coop_eval`) — we match
``detection.target_position`` to the nearest known true-target / decoy
position by haversine distance.

Decoys misidentified as real (``misid_flag=True``) still report a
``target_position`` pointing at the decoy, so nearest-neighbour matches
them to the decoy and the caller treats that match as a non-effective
(misid) track — exactly the semantics the evaluator wants.

This module is deliberately dependency-free w.r.t. example state classes:
callers (each example's ``run.py``) adapt their own state shape
(``SimState`` / ``MultiSimState`` / ``SwarmState``) into the standardised
:class:`UavDetection` inputs. That keeps the common package unit-testable
without circular imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from examples.uav_search_track_car.search_track.geometry import haversine_m


@dataclass(frozen=True)
class UavDetection:
    """Standardised per-UAV detection view, built by the caller from one
    sim:state frame.

    All position/type fields come straight from the UAV's
    ``gimbal_tracking.detection`` record; ``destroyed`` comes from
    ``platform.status == "destroyed"``.
    """
    uid: str
    detected: bool
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    target_type: str = ""          # "ground_vehicle" | "decoy_vehicle" | ""
    misid_flag: bool = False
    destroyed: bool = False
    confidence: float = 0.0        # detection.confidence: 1.0=centered, 0=edge


@dataclass(frozen=True)
class TargetMatch:
    """Result of resolving one UAV's detection to a target."""
    target_uid: Optional[str]      # matched true-target uid, or None
    is_effective: bool             # tracking a real (non-decoy) target
    was_misid: bool                # matched a decoy (misidentification)
    confidence: float = 0.0        # tracking quality: 1.0=centered, 0=edge


def resolve_uav_to_target(
    uavs: list[UavDetection],
    true_targets: dict[str, tuple[float, float]],
    decoys: dict[str, tuple[float, float]],
    *,
    max_match_m: float = 120.0,
) -> dict[str, TargetMatch]:
    """Map each UAV's detection to the nearest target by position.

    Args:
        uavs: per-UAV detection views for this tick.
        true_targets: ``{target_uid: (lat, lon)}`` real-target positions.
        decoys: ``{decoy_uid: (lat, lon)}`` decoy positions.
        max_match_m: reject matches farther than this. ``detected=True``
            implies the target is inside the camera FOV, so its reported
            position should be near the true target; this guards against
            stale / anomalous positions binding to an unrelated target.

    Returns:
        ``{uav_uid: TargetMatch}``. UAVs that are destroyed, not detecting,
        have no target_position, or have no in-range match map to
        ``TargetMatch(None, False, False)``.
    """
    # Build a single candidate list tagged with a real/decoy flag.
    candidates: list[tuple[str, tuple[float, float], bool]] = [
        (uid, pos, False) for uid, pos in true_targets.items()
    ] + [
        (uid, pos, True) for uid, pos in decoys.items()
    ]

    result: dict[str, TargetMatch] = {}
    for u in uavs:
        if (u.destroyed or not u.detected
                or u.target_lat is None or u.target_lon is None
                or not candidates):
            result[u.uid] = TargetMatch(None, False, False)
            continue

        best_uid: Optional[str] = None
        best_is_decoy = False
        best_d: Optional[float] = None
        for cuid, cpos, is_decoy in candidates:
            d = haversine_m(u.target_lat, u.target_lon, cpos[0], cpos[1])
            if best_d is None or d < best_d:
                best_d, best_uid, best_is_decoy = d, cuid, is_decoy

        if best_uid is None or best_d is None or best_d > max_match_m:
            result[u.uid] = TargetMatch(None, False, False)
            continue

        if best_is_decoy:
            result[u.uid] = TargetMatch(best_uid, is_effective=False,
                                        was_misid=True, confidence=0.0)
        else:
            result[u.uid] = TargetMatch(best_uid, is_effective=True,
                                        was_misid=False,
                                        confidence=u.confidence)
    return result
