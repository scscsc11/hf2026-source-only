"""Multi-entity state view for the 017 cooperative example.

Reuses 016's dataclasses (UavState, GimbalState, Detection, TargetState,
GeoPosition, Attitude) and adds:
  - EntityState: per-entity bundle (uav OR vehicle), keyed by unique_id
  - MultiSimState: dict[uid -> EntityState] + sim metadata
  - parse_multi_sim_state: parse one sim:state frame into MultiSimState

Design (research.md D-8): parse_sim_state in 016 is single-uav/single-target;
017 needs a multi-entity view. We keep 016's parse_sim_state intact and add
a parallel parser here so 016 stays stable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Reuse 016's dataclasses for field-level compatibility.
from examples.uav_search_track_car.search_track.state import (
    Attitude, Detection, GeoPosition, GimbalState, TargetState, UavState,
)


@dataclass(frozen=True)
class CommStats:
    sent: int = 0
    delivered: int = 0
    received: int = 0
    rejected_bytes: int = 0
    rejected_rate: int = 0
    rejected_range: int = 0
    rejected_jam: int = 0


@dataclass(frozen=True)
class CommInboxEntry:
    sender: str
    payload: str
    recv_time: float


@dataclass(frozen=True)
class CommState:
    """Per-UAV communication state (only present on UAV entities)."""
    enabled: bool = False
    range_m: float = 1000.0
    max_bytes: int = 50
    max_rate_hz: float = 4.0
    inbox: tuple[CommInboxEntry, ...] = field(default_factory=tuple)
    stats: CommStats = field(default_factory=CommStats)


@dataclass(frozen=True)
class ExtendedDetection(Detection):
    """016 Detection + 017 misid fields (FR-014/015).

    ``target_uid`` mirrors ``gimbal_tracking.target_entity`` if the engine
    ever publishes it; today it is empty, so the Spec 025 evaluator falls
    back to nearest-neighbour position matching
    (uav_target_map.resolve_uav_to_target).
    """
    target_type: str = ""          # "ground_vehicle" | "decoy_vehicle" | ""
    misid_flag: bool = False
    misid_count: int = 0
    misid_track_duration: float = 0.0
    target_uid: str = ""           # resolved target uid (empty until engine publishes)


@dataclass(frozen=True)
class EntityState:
    """One entity (UAV or vehicle) in the multi-entity view."""
    uid: str
    kind: str                      # "uav" | "ground_vehicle" | "decoy_vehicle"
    name: str
    uav: UavState | None = None
    gimbal: GimbalState | None = None
    detection: ExtendedDetection | None = None
    comm: CommState | None = None
    vehicle_truth: TargetState | None = None


@dataclass(frozen=True)
class MultiSimState:
    """All entities for one tick + sim metadata."""
    sim_time: float
    timestamp: float
    status: str
    entities: dict[str, EntityState]


def _parse_detection(det_raw: dict[str, Any]) -> ExtendedDetection:
    det_pos_raw = det_raw.get("target_position")
    det_pos = None
    if det_pos_raw:
        det_pos = GeoPosition(
            latitude=float(det_pos_raw.get("latitude", 0.0)),
            longitude=float(det_pos_raw.get("longitude", 0.0)),
            altitude=float(det_pos_raw.get("altitude", 0.0)),
        )
    return ExtendedDetection(
        detected=bool(det_raw.get("detected", False)),
        confidence=float(det_raw.get("confidence", 0.0)),
        target_position=det_pos,
        azimuth_error_deg=det_raw.get("azimuth_error"),
        target_type=str(det_raw.get("target_type", "")),
        misid_flag=bool(det_raw.get("misid_flag", False)),
        misid_count=int(det_raw.get("misid_count", 0)),
        misid_track_duration=float(det_raw.get("misid_track_duration", 0.0)),
        target_uid=str(det_raw.get("target_entity", "")),
    )


def _parse_uav(uid: str, raw: dict[str, Any]) -> EntityState:
    platform = raw.get("platform", {}) or {}
    pos = platform.get("position", {}) or {}
    att = platform.get("attitude", {}) or {}
    uav = UavState(
        position=GeoPosition(
            latitude=float(pos.get("latitude", 0.0)),
            longitude=float(pos.get("longitude", 0.0)),
            altitude=float(pos.get("altitude", 0.0)),
        ),
        attitude=Attitude(
            yaw=float(att.get("yaw", 0.0)),
            pitch=float(att.get("pitch", 0.0)),
            roll=float(att.get("roll", 0.0)),
        ),
        velocity=float(raw.get("velocity", 0.0)),
        heading=float(raw.get("heading", att.get("yaw", 0.0))),
    )
    gimbal_raw = raw.get("gimbal_tracking", {}) or {}
    gimbal = GimbalState(
        pan_angle=float(gimbal_raw.get("pan_angle", 0.0)),
        tilt_angle=float(gimbal_raw.get("tilt_angle", 0.0)),
        track_enabled=bool(gimbal_raw.get("track_enabled", False)),
        fov_deg=gimbal_raw.get("fov_deg"),
    )
    det_raw = gimbal_raw.get("detection", {}) or {}
    detection = _parse_detection(det_raw)
    comm_raw = raw.get("comm", {}) or {}
    comm: CommState | None = None
    if comm_raw:
        inbox = tuple(
            CommInboxEntry(
                sender=str(e.get("sender", "")),
                payload=str(e.get("payload", "")),
                recv_time=float(e.get("recv_time", 0.0)),
            )
            for e in (comm_raw.get("inbox", []) or [])
        )
        stats_raw = comm_raw.get("stats", {}) or {}
        comm = CommState(
            enabled=bool(comm_raw.get("enabled", False)),
            range_m=float(comm_raw.get("range_m", 1000.0)),
            max_bytes=int(comm_raw.get("max_bytes", 50)),
            max_rate_hz=float(comm_raw.get("max_rate_hz", 4.0)),
            inbox=inbox,
            stats=CommStats(
                sent=int(stats_raw.get("sent", 0)),
                delivered=int(stats_raw.get("delivered", 0)),
                received=int(stats_raw.get("received", 0)),
                rejected_bytes=int(stats_raw.get("rejected_bytes", 0)),
                rejected_rate=int(stats_raw.get("rejected_rate", 0)),
                rejected_jam=int(stats_raw.get("rejected_jam", 0)),
            ),
        )
    return EntityState(
        uid=uid, kind="uav", name=str(raw.get("name", uid)),
        uav=uav, gimbal=gimbal, detection=detection, comm=comm,
    )


def _parse_vehicle(uid: str, raw: dict[str, Any], kind: str) -> EntityState:
    platform = raw.get("platform", {}) or {}
    pos = platform.get("position", {}) or {}
    truth = TargetState(
        position=GeoPosition(
            latitude=float(pos.get("latitude", 0.0)),
            longitude=float(pos.get("longitude", 0.0)),
            altitude=float(pos.get("altitude", 0.0)),
        ),
        speed=float(raw.get("speed", 0.0)),
        heading=float(raw.get("heading", 0.0)),
    )
    return EntityState(
        uid=uid, kind=kind, name=str(raw.get("name", uid)),
        vehicle_truth=truth,
    )


_NON_ENTITY_KEYS = frozenset({
    "timestamp", "status", "sim_time", "sim_time_str", "step_perf",
})


def parse_multi_sim_state(raw: dict[str, Any]) -> MultiSimState:
    """Parse one sim:state frame into a MultiSimState."""
    sim_time = float(raw.get("sim_time", 0.0))
    timestamp = float(raw.get("timestamp", 0.0))
    status = str(raw.get("status", "unknown"))
    entities: dict[str, EntityState] = {}
    for key, val in raw.items():
        if key in _NON_ENTITY_KEYS or not isinstance(val, dict):
            continue
        etype = str(val.get("type", ""))
        if etype in ("fixed_wing_uav", "uav"):
            entities[key] = _parse_uav(key, val)
        elif etype == "ground_vehicle":
            entities[key] = _parse_vehicle(key, val, "ground_vehicle")
        elif etype == "decoy_vehicle":
            entities[key] = _parse_vehicle(key, val, "decoy_vehicle")
    return MultiSimState(sim_time=sim_time, timestamp=timestamp,
                         status=status, entities=entities)
