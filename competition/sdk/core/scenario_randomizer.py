"""Seed-driven scenario randomization for training/validation variety.

A player passes ``--seed N`` to ``run()``; the runner materializes a
*modified* scenario.json (target trajectories, UAV start positions, decoy
positions, threat-zone geometry) derived deterministically from N, and
points the engine at it. This lets a player test their algorithm across
many distinct scenes without editing config by hand.

Design rules:
  * **Deterministic**: same seed → same scene (reproducible runs).
  * **Scenario-agnostic core**: the randomizer mutates generic scenario
    fields (entity positions, trajectory waypoints, zone polygons). A
    scenario opts in via a small :class:`RandomizePolicy`.
  * **search_track invariant**: the UAV and the target are translated by
    the SAME offset so their relative distance is preserved (per the
    stakeholder requirement). The trajectory the target follows is
    re-routed around the new start.
  * Players do NOT see the randomization details (it's engine-side). They
    only see the resulting world via their (isolated) observations.
"""
from __future__ import annotations

import copy
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_DEG_M = 111_320.0   # meters per degree of latitude (approx, for offsets)


def _meters_to_deg(meters_n: float, meters_e: float,
                   ref_lat: float) -> Tuple[float, float]:
    """Convert a north/east meter offset to (dlat, dlon) at ref_lat."""
    dlat = meters_n / _DEG_M
    dlon = meters_e / (_DEG_M * max(0.1, math.cos(math.radians(ref_lat))))
    return dlat, dlon


def _shift_polygon(poly: List[List[float]], dlat: float,
                   dlon: float) -> List[List[float]]:
    return [[p[0] + dlat, p[1] + dlon] for p in poly]


class RandomizePolicy:
    """How a scenario wants to be randomized. Override per scenario.

    Defaults implement "shift everything by a seed-derived global offset
    + jitter positions + re-route target trajectories". Subclasses can
    tighten this (e.g. search_track's same-offset invariant).
    """

    def __init__(self, *, jitter_m: float = 200.0, route_span_m: float = 800.0):
        self.jitter_m = jitter_m          # per-entity position jitter radius
        self.route_span_m = route_span_m  # target re-route radius

    def apply(self, scenario: Dict[str, Any], rng: random.Random) -> None:
        """Mutate ``scenario`` in place using ``rng``."""
        # 1) global offset applied to all positions (keeps the cluster shape)
        ref_lat = self._reference_lat(scenario)
        dlat, dlon = _meters_to_deg(
            rng.uniform(-self.route_span_m * 0.3, self.route_span_m * 0.3),
            rng.uniform(-self.route_span_m * 0.3, self.route_span_m * 0.3),
            ref_lat)
        for ent in scenario.get("entities", []):
            self._shift_entity(ent, dlat, dlon, rng)
        # 2) re-route moving target trajectories around their new start
        for ent in scenario.get("entities", []):
            if ent.get("type") in ("TargetVehicle", "ground_vehicle"):
                self._reroute_trajectory(ent, rng)
        # 3) relocate threat zones (adversarial_swarm) by the same offset
        zones = scenario.get("zones")
        if isinstance(zones, list):
            for z in zones:
                if "polygon" in z and isinstance(z["polygon"], list):
                    z["polygon"] = _shift_polygon(z["polygon"], dlat, dlon)
                # reseed dynamic zones so their spawn pattern varies
                if z.get("type") == "comm_jam_random":
                    z["rng_seed"] = float(rng.randint(0, 1_000_000))

    # ── helpers ───────────────────────────────────────────────────────

    def _reference_lat(self, scenario: Dict[str, Any]) -> float:
        for ent in scenario.get("entities", []):
            lat = ent.get("params", {}).get("initial_latitude")
            if lat is not None:
                return float(lat)
        return 27.0

    def _shift_entity(self, ent: Dict[str, Any], dlat: float, dlon: float,
                      rng: random.Random) -> None:
        p = ent.get("params")
        if not p:
            return
        # extra per-entity jitter (decoys/UAVs spread out a bit)
        jl, jlo = _meters_to_deg(
            rng.uniform(-self.jitter_m, self.jitter_m),
            rng.uniform(-self.jitter_m, self.jitter_m),
            p.get("initial_latitude", 27.0))
        for key, delta in (("initial_latitude", dlat + jl),
                           ("initial_longitude", dlon + jlo)):
            if key in p:
                p[key] = float(p[key]) + delta
        # shift any trajectory waypoints the entity already carries
        comps = ent.get("components", {}) or {}
        traj = (comps.get("trajectory", {}) or {}).get("params", {})
        for wp in traj.get("waypoints", []) or []:
            if "lat" in wp:
                wp["lat"] = float(wp["lat"]) + dlat
            if "lon" in wp:
                wp["lon"] = float(wp["lon"]) + dlon

    def _reroute_trajectory(self, ent: Dict[str, Any],
                            rng: random.Random) -> None:
        """Generate fresh waypoints for a moving target around its start.

        Keeps the trajectory's speed; replaces waypoints with a seed-derived
        route so the target maneuvers differently each seed.
        """
        p = ent.get("params", {}) or {}
        lat0 = p.get("initial_latitude")
        lon0 = p.get("initial_longitude")
        if lat0 is None or lon0 is None:
            return
        comps = ent.get("components", {}) or {}
        traj = (comps.get("trajectory", {}) or {})
        tparams = traj.get("params", {}) or {}
        speed = tparams.get("speed", 8.0)
        n_wp = 3 + rng.randint(0, 2)
        wps = []
        for _ in range(n_wp):
            dlat, dlon = _meters_to_deg(
                rng.uniform(-self.route_span_m, self.route_span_m),
                rng.uniform(-self.route_span_m, self.route_span_m),
                lat0)
            wps.append({"lat": float(lat0) + dlat,
                        "lon": float(lon0) + dlon, "alt": 0})
        tparams["speed"] = float(speed)
        tparams["waypoints"] = wps


class SearchTrackPolicy(RandomizePolicy):
    """search_track: translate the UAV and the target by the SAME offset.

    Their relative distance is preserved (stakeholder requirement). Target
    trajectory is re-routed around the new start.
    """

    def apply(self, scenario: Dict[str, Any], rng: random.Random) -> None:
        ref_lat = self._reference_lat(scenario)
        dlat, dlon = _meters_to_deg(
            rng.uniform(-self.route_span_m, self.route_span_m),
            rng.uniform(-self.route_span_m, self.route_span_m),
            ref_lat)
        # SAME offset for UAV and target (no per-entity jitter) → relative
        # distance invariant. Only the absolute location changes.
        for ent in scenario.get("entities", []):
            p = ent.get("params")
            if not p:
                continue
            for key, delta in (("initial_latitude", dlat),
                               ("initial_longitude", dlon)):
                if key in p:
                    p[key] = float(p[key]) + delta
            # The target's base waypoints (now in scenario.json) are not
            # shifted here: the route is fully re-generated below from the
            # seed, so only the start location needs translating.
        # re-route the target's startup trajectory deterministically so the
        # runner can read it. We stash it where the runner looks.
        for ent in scenario.get("entities", []):
            if ent.get("type") in ("TargetVehicle", "ground_vehicle"):
                p = ent.get("params", {}) or {}
                lat0 = p.get("initial_latitude", ref_lat)
                lon0 = p.get("initial_longitude", 125.0)
                wps = []
                for _ in range(3):
                    wl, wlo = _meters_to_deg(
                        rng.uniform(200.0, 600.0),
                        rng.uniform(200.0, 600.0), lat0)
                    wps.append({"lat": float(lat0) + wl,
                                "lon": float(lon0) + wlo, "alt": 0})
                comps = ent.setdefault("components", {}).setdefault(
                    "trajectory", {}).setdefault("params", {})
                comps["speed"] = 8.0
                comps["waypoints"] = wps


# registry: scenario name → policy
_POLICIES = {
    "search_track": SearchTrackPolicy,
    "coop_decoy": RandomizePolicy,
    "adversarial_swarm": RandomizePolicy,
}


def randomize_scenario(scenario_path: str, seed: int,
                       scenario_name: str,
                       out_dir: str = "output") -> str:
    """Materialize a randomized copy of ``scenario_path`` for ``seed``.

    Returns the path to the new scenario.json (under ``out_dir``). The
    caller points the engine at this path. If ``seed`` is None/0 the
    original path is returned unchanged.
    """
    if not seed:
        return scenario_path
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8-sig"))
    scenario = copy.deepcopy(scenario)
    rng = random.Random(int(seed))
    policy_cls = _POLICIES.get(scenario_name, RandomizePolicy)
    policy_cls().apply(scenario, rng)
    out = Path(out_dir) / f"scenario_{scenario_name}_seed{seed}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
    return str(out)
