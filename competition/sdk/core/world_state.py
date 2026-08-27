"""Full-truth world state — runner-internal only, never shown to players.

``WorldState`` is the complete parsed ``sim:state`` frame: every entity
(UAVs, targets, decoys) with ground-truth pose, plus the dynamic zones
bucket. It exists for two purposes only:

  1. The runner uses it to build per-entity player Observations via
     :mod:`competition.sdk.core.isolation` (which projects ONLY the
     agent's own SelfView — other entities' truth is never projected).
  2. The scoring evaluator uses it (scoring is the judge's job and may
     use truth; see contracts/isolation.md §5).

Players have NO path to a WorldState reference. This module is an
internal implementation detail of the SDK runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


_NON_ENTITY_KEYS = frozenset({
    "timestamp", "status", "sim_time", "sim_time_str", "step_perf",
    "reason", "zones",
})


@dataclass
class EntityTruth:
    """Ground-truth view of one entity (runner-internal).

    ``kind`` is the normalised preset label: ``uav`` / ``ground_vehicle`` /
    ``decoy_vehicle``. ``raw`` keeps the full entity dict for scenario-
    specific projections (detection, comm, gimbal, etc.).
    """
    uid: str
    kind: str
    name: str
    lat: float
    lon: float
    alt: float
    heading: float
    speed: float
    status: str                          # "active" | "destroyed"
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ZoneTruth:
    """One zone entry from the dynamic ``zones`` bucket (runner-internal).

    Includes BOTH static and dynamic zones — the runner/isolation layer
    decides which (if any) are static-pre-match-known and routes them to
    the briefing; dynamic zones never reach the player.
    """
    kind: str                            # "air_defense" | "comm_jam_static" | "comm_jam_random"
    polygon: List[Tuple[float, float]] = field(default_factory=list)
    alt_min: float = 0.0
    alt_max: float = 1e9
    # dynamic-jam specific (None for static zones)
    is_dynamic: bool = False


def _normalise_kind(raw_type: str, entity_type: str) -> str:
    """Map engine type strings to the normalised preset label."""
    t = (raw_type or entity_type or "").lower()
    if t in ("fixed_wing_uav", "uav"):
        return "uav"
    if t == "ground_vehicle":
        return "ground_vehicle"
    if t == "decoy_vehicle":
        return "decoy_vehicle"
    return t


@dataclass
class WorldState:
    """All entities + zones for one tick, plus sim metadata. Internal."""
    sim_time: float = 0.0
    timestamp: float = 0.0
    status: str = "running"
    entities: Dict[str, EntityTruth] = field(default_factory=dict)
    zones: List[ZoneTruth] = field(default_factory=list)

    # ── convenience views (for scoring / runner) ──────────────────────

    @property
    def uavs(self) -> Dict[str, EntityTruth]:
        return {u: e for u, e in self.entities.items() if e.kind == "uav"}

    @property
    def alive_uavs(self) -> Dict[str, EntityTruth]:
        return {u: e for u, e in self.uavs.items() if e.status == "active"}

    @property
    def targets(self) -> Dict[str, EntityTruth]:
        return {u: e for u, e in self.entities.items()
                if e.kind == "ground_vehicle"}

    @property
    def decoys(self) -> Dict[str, EntityTruth]:
        return {u: e for u, e in self.entities.items()
                if e.kind == "decoy_vehicle"}

    def is_destroyed(self, uid: str) -> bool:
        e = self.entities.get(uid)
        return e is not None and e.status == "destroyed"


def parse_world_state(raw: Dict[str, Any]) -> WorldState:
    """Parse one ``sim:state`` JSON frame into a WorldState.

    Tolerant of missing fields (safe defaults). Entity objects are keyed
    by unique_id; the zones bucket is parsed into ZoneTruth entries.
    """
    st = WorldState(
        sim_time=float(raw.get("sim_time", raw.get("timestamp", 0.0))),
        timestamp=float(raw.get("timestamp", 0.0)),
        status=str(raw.get("status", "running")),
    )

    for key, val in raw.items():
        if key in _NON_ENTITY_KEYS or not isinstance(val, dict):
            continue
        plat = val.get("platform", {}) or {}
        pos = plat.get("position", {}) or {}
        kind = _normalise_kind(str(val.get("type", "")),
                               str(plat.get("entity_type", "")))
        st.entities[key] = EntityTruth(
            uid=key,
            kind=kind,
            name=str(val.get("name", key)),
            lat=float(pos.get("latitude", 0.0)),
            lon=float(pos.get("longitude", 0.0)),
            alt=float(pos.get("altitude", 0.0)),
            heading=float(val.get("heading", plat.get("attitude", {}).get("yaw", 0.0))),
            speed=float(val.get("velocity", val.get("speed", 0.0))),
            status=str(plat.get("status", "active")),
            raw=val,
        )

    zones_obj = raw.get("zones", {}) or {}
    for kind_key in ("air_defense", "comm_jam_static", "comm_jam_random"):
        is_dyn = (kind_key == "comm_jam_random")
        for z in (zones_obj.get(kind_key, []) or []):
            st.zones.append(ZoneTruth(
                kind=kind_key,
                polygon=[(float(p[0]), float(p[1]))
                         for p in (z.get("polygon", []) or [])],
                alt_min=float(z.get("alt_min", 0.0)),
                alt_max=float(z.get("alt_max", 1e9)),
                is_dynamic=is_dyn,
            ))

    return st
