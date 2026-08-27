"""GreedyController — simplest possible custom controller (reference impl).

Just sweeps the gimbal horizontally when no detection; on detection, aims
at target. Demonstrates the 30-line custom algorithm that the spec calls
for (SC-006)."""
from __future__ import annotations

import math

from search_track.commands import CommandTarget, ControlCommand
from search_track.controller import Controller
from search_track.geometry import los_angles
from search_track.state import SimState


class GreedyController(Controller):
    """No FSM, no loiter — minimal example for users to copy."""

    def __init__(self) -> None:
        self._t: float = 0.0

    def decide(self, state: SimState, dt: float) -> list[ControlCommand]:
        self._t += dt
        if state.detection.detected and state.detection.target_position is not None:
            tgt = state.detection.target_position
            pan, tilt = los_angles(
                state.uav.position.latitude, state.uav.position.longitude,
                state.uav.position.altitude, state.uav.attitude.yaw,
                tgt.latitude, tgt.longitude, tgt.altitude,
            )
            return [
                ControlCommand(
                    target=CommandTarget.UAV,
                    cmd="component.gimbal_tracking.set_orientation",
                    params={"pan": pan, "tilt": tilt},
                )
            ]
        # No detection: sweep pan slowly
        pan = (self._t * 30.0) % 360.0 - 180.0
        return [
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="component.gimbal_tracking.set_orientation",
                params={"pan": pan, "tilt": -45.0},
            )
        ]

    def reset(self) -> None:
        self._t = 0.0
