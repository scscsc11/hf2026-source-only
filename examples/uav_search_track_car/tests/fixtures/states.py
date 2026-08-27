"""Helpers for constructing SimState fixtures in tests."""
from search_track.state import (
    Attitude,
    Detection,
    GeoPosition,
    GimbalState,
    SimState,
    TargetState,
    UavState,
)


def make_state(
    *,
    sim_time: float = 1.0,
    status: str = "running",
    uav_lat: float = 27.0,
    uav_lon: float = 125.0,
    uav_alt: float = 300.0,
    uav_yaw: float = 90.0,
    uav_pitch: float = 0.0,
    uav_roll: float = 0.0,
    uav_velocity: float = 20.0,
    uav_heading: float = 90.0,
    pan: float = 0.0,
    tilt: float = -30.0,
    track_enabled: bool = False,
    fov_deg: float | None = 60.0,
    detected: bool = False,
    confidence: float = 0.0,
    tgt_lat: float | None = None,
    tgt_lon: float | None = None,
    tgt_alt: float | None = None,
    azimuth_error: float | None = None,
    target_truth_present: bool = True,
) -> SimState:
    det_target = None
    if detected and tgt_lat is not None:
        det_target = GeoPosition(tgt_lat, tgt_lon or 0.0, tgt_alt or 0.0)
    detection = Detection(
        detected=detected,
        confidence=confidence,
        target_position=det_target,
        azimuth_error_deg=azimuth_error,
    )
    truth = None
    if target_truth_present:
        truth = TargetState(
            position=GeoPosition(tgt_lat or 27.01, tgt_lon or 125.01, tgt_alt or 0.0),
            speed=10.0,
            heading=0.0,
        )
    return SimState(
        sim_time=sim_time,
        timestamp=sim_time,
        status=status,
        uav=UavState(
            position=GeoPosition(uav_lat, uav_lon, uav_alt),
            attitude=Attitude(yaw=uav_yaw, pitch=uav_pitch, roll=uav_roll),
            velocity=uav_velocity,
            heading=uav_heading,
        ),
        gimbal=GimbalState(
            pan_angle=pan,
            tilt_angle=tilt,
            track_enabled=track_enabled,
            fov_deg=fov_deg,
        ),
        detection=detection,
        target_truth=truth,
    )
