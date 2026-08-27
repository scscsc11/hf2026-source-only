"""FSM controller — Search ↔ Track with hysteresis."""
from __future__ import annotations

import time
from typing import Any

from .commands import CommandTarget, ControlCommand
from .controller import Controller
from .search_strategies import SpiralParams, spiral_next_waypoint, sweep_orientation
from .tracking_strategy import LoiterTracker, TrackerParams
from .state import SimState


class FsmSearchTrackController(Controller):
    """Default controller. Implements the FSM and all command generation.

    Invariants (data-model.md):
      I-1: SEARCH waypoints within search_radius of base
      I-2: TRACK set_enabled=False always
      I-3: TRACK loiter_radius == config.loiter_radius
      I-4: TRACK set_orientation present, pan within 5° of current
      I-5: decide() returns ≤ 5 commands
      I-6: after K_acquire consecutive detected, TRACK entry commands issued
      I-7: set_target_entity never issued
    """

    def __init__(self) -> None:
        self._consecutive_detected: int = 0
        self._consecutive_lost: int = 0
        self._mode: str = "SEARCH"
        self._k_acquire: int = 5
        self._k_lost: int = 60
        self._spiral_growth_rate: float = 30.0
        self._sweep_pitch_min: float = -60.0
        self._sweep_pitch_max: float = -30.0
        self._sweep_period: float = 4.0
        self._search_radius: float = 500.0
        self._search_altitude_agl: float = 300.0
        self._loiter_radius: float = 200.0
        self._loiter_refresh_period: float = 3.0
        self._control_rate_hz: int = 10
        self._base_lat: float = 0.0
        self._base_lon: float = 0.0
        self._base_alt: float = 0.0
        # Last-known gimbal pan/tilt (used as holdover when detection flickers)
        self._last_pan: float = 0.0
        self._last_tilt: float = -45.0
        self._tracker: LoiterTracker | None = None
        self._configured: bool = False

    def configure(self, cfg: Any) -> None:
        """Called once before the first decide() with AlgorithmConfig."""
        self._k_acquire = int(cfg.get("k_acquire", self._k_acquire))
        self._k_lost = int(cfg.get("k_lost", self._k_lost))
        self._spiral_growth_rate = float(cfg.get("spiral_growth_rate", self._spiral_growth_rate))
        self._sweep_pitch_min = float(cfg.get("sweep_pitch_min", self._sweep_pitch_min))
        self._sweep_pitch_max = float(cfg.get("sweep_pitch_max", self._sweep_pitch_max))
        self._sweep_period = float(cfg.get("sweep_period", self._sweep_period))
        self._search_radius = float(cfg.get("search_radius", self._search_radius))
        self._search_altitude_agl = float(cfg.get("search_altitude_agl", self._search_altitude_agl))
        self._loiter_radius = float(cfg.get("loiter_radius", self._loiter_radius))
        self._loiter_refresh_period = float(
            cfg.get("loiter_refresh_period", self._loiter_refresh_period)
        )
        self._control_rate_hz = int(cfg.get("control_rate_hz", self._control_rate_hz))
        self._configured = True

    def reset(self) -> None:
        self._consecutive_detected = 0
        self._consecutive_lost = 0
        self._mode = "SEARCH"
        self._tracker = None

    @property
    def mode(self) -> str:
        return self._mode

    def decide(self, state: SimState, dt: float) -> list[ControlCommand]:
        if not self._configured:
            # No configuration yet; do nothing safe
            return []
        if self._base_lat == 0.0 and self._base_lon == 0.0:
            self._base_lat = state.uav.position.latitude
            self._base_lon = state.uav.position.longitude
            self._base_alt = state.uav.position.altitude
        detected = state.detection.detected
        if detected:
            self._consecutive_detected += 1
            self._consecutive_lost = 0
        else:
            self._consecutive_lost += 1
            self._consecutive_detected = 0

        if self._mode == "SEARCH" and self._consecutive_detected >= self._k_acquire:
            self._mode = "TRACK"
            self._tracker = LoiterTracker(
                params=TrackerParams(
                    loiter_radius=self._loiter_radius,
                    loiter_refresh_period=self._loiter_refresh_period,
                ),
            )
            self._tracker.reset(self._base_lat, self._base_lon)
        elif self._mode == "TRACK" and self._consecutive_lost >= self._k_lost:
            self._mode = "SEARCH"

        if self._mode == "SEARCH":
            return self._search_commands(state, dt)
        return self._track_commands(state, dt)

    def _search_commands(self, state: SimState, dt: float) -> list[ControlCommand]:
        params = SpiralParams(
            base_lat=self._base_lat,
            base_lon=self._base_lon,
            base_alt=self._base_alt,
            search_radius=self._search_radius,
            spiral_growth_rate=self._spiral_growth_rate,
        )
        lat, lon, alt = spiral_next_waypoint(state.sim_time, params)
        _, tilt = sweep_orientation(
            state.sim_time,
            period=self._sweep_period,
            pitch_min=self._sweep_pitch_min,
            pitch_max=self._sweep_pitch_max,
        )
        return [
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="set_destination",
                params={"latitude": lat, "longitude": lon, "altitude": alt},
            ),
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="component.gimbal_tracking.set_orientation",
                params={"pan": 0.0, "tilt": tilt},
            ),
        ]

    def _track_commands(self, state: SimState, dt: float) -> list[ControlCommand]:
        # If no detection this tick, hold the last-known gimbal pan/tilt instead of
        # snapping to a fixed forward-looking pose.  This prevents large-angle jumps
        # when detection flickers (e.g. decoy misid roll alternates true/false).
        if not state.detection.detected or state.detection.target_position is None:
            return [
                ControlCommand(
                    target=CommandTarget.UAV,
                    cmd="component.gimbal_tracking.set_orientation",
                    params={"pan": self._last_pan, "tilt": self._last_tilt},
                )
            ]
        assert self._tracker is not None
        tpos = state.detection.target_position
        cmds_dict = self._tracker.commands(
            sim_time=state.sim_time,
            uav_lat=state.uav.position.latitude,
            uav_lon=state.uav.position.longitude,
            uav_alt=state.uav.position.altitude,
            uav_yaw=state.uav.attitude.yaw,
            tgt_lat=tpos.latitude,
            tgt_lon=tpos.longitude,
            tgt_alt=tpos.altitude,
        )
        for c in cmds_dict:
            if c["cmd"] == "component.gimbal_tracking.set_orientation":
                self._last_pan = float(c["params"].get("pan", self._last_pan))
                self._last_tilt = float(c["params"].get("tilt", self._last_tilt))
                break
        return [
            ControlCommand(target=CommandTarget(c["target"]), cmd=c["cmd"], params=c["params"])
            for c in cmds_dict
        ]
