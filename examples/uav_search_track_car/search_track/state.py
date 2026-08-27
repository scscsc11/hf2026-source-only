"""SimState data classes — representation of one tick of simulation state.

Source of truth for field shapes: ``src/components/gimbal_tracking_component.cc``
state() and ``src/entities/fixed_wing_uav.cc``/``target_vehicle.cc`` state().
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class GeoPosition:
    latitude: float
    longitude: float
    altitude: float


@dataclass(frozen=True)
class Attitude:
    yaw: float
    pitch: float
    roll: float


@dataclass(frozen=True)
class UavState:
    position: GeoPosition
    attitude: Attitude
    velocity: float
    heading: float


@dataclass(frozen=True)
class GimbalState:
    pan_angle: float
    tilt_angle: float
    track_enabled: bool
    fov_deg: float | None = None


@dataclass(frozen=True)
class Detection:
    detected: bool
    confidence: float
    target_position: GeoPosition | None
    azimuth_error_deg: float | None


@dataclass(frozen=True)
class TargetState:
    """Ground truth target. Used by metrics, NOT by controller."""

    position: GeoPosition
    speed: float
    heading: float


@dataclass(frozen=True)
class SimState:
    sim_time: float
    timestamp: float
    status: str
    uav: UavState
    gimbal: GimbalState
    detection: Detection
    target_truth: TargetState | None

    def without_truth(self) -> "SimState":
        """Return a copy with target_truth stripped. Used to enforce FR-007
        and invariant I-5 (controllers only see detection-derived fields)."""
        return replace(self, target_truth=None)


def parse_sim_state(
    raw: dict[str, Any],
    *,
    uav_id: str,
    target_id: str,
) -> SimState:
    """Build a SimState from a parsed sim:state JSON message.

    Missing fields are filled with safe defaults (zero / None)."""
    sim_time = float(raw.get("sim_time", 0.0))
    timestamp = float(raw.get("timestamp", 0.0))
    status = str(raw.get("status", "unknown"))

    uav_raw = raw.get(uav_id, {}) or {}
    platform = uav_raw.get("platform", {}) or {}
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
        velocity=float(uav_raw.get("velocity", 0.0)),
        heading=float(uav_raw.get("heading", att.get("yaw", 0.0))),
    )

    gimbal_raw = uav_raw.get("gimbal_tracking", {}) or {}
    gimbal = GimbalState(
        pan_angle=float(gimbal_raw.get("pan_angle", 0.0)),
        tilt_angle=float(gimbal_raw.get("tilt_angle", 0.0)),
        track_enabled=bool(gimbal_raw.get("track_enabled", False)),
        fov_deg=gimbal_raw.get("fov_deg"),
    )

    det_raw = gimbal_raw.get("detection", {}) or {}
    det_pos_raw = det_raw.get("target_position")
    det_pos = None
    if det_pos_raw:
        det_pos = GeoPosition(
            latitude=float(det_pos_raw.get("latitude", 0.0)),
            longitude=float(det_pos_raw.get("longitude", 0.0)),
            altitude=float(det_pos_raw.get("altitude", 0.0)),
        )
    detection = Detection(
        detected=bool(det_raw.get("detected", False)),
        confidence=float(det_raw.get("confidence", 0.0)),
        target_position=det_pos,
        azimuth_error_deg=det_raw.get("azimuth_error"),
    )

    target_raw = raw.get(target_id, {}) or {}
    target_platform = target_raw.get("platform", {}) or {}
    target_pos = target_platform.get("position", {}) or {}
    truth = TargetState(
        position=GeoPosition(
            latitude=float(target_pos.get("latitude", 0.0)),
            longitude=float(target_pos.get("longitude", 0.0)),
            altitude=float(target_pos.get("altitude", 0.0)),
        ),
        speed=float(target_raw.get("speed", 0.0)),
        heading=float(target_raw.get("heading", 0.0)),
    )

    return SimState(
        sim_time=sim_time,
        timestamp=timestamp,
        status=status,
        uav=uav,
        gimbal=gimbal,
        detection=detection,
        target_truth=truth,
    )
