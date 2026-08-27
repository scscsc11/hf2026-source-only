"""Tracking strategy — UAV loiter + algorithm-driven gimbal LOS."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .geometry import los_angles


@dataclass
class TrackerParams:
    loiter_radius: float
    loiter_refresh_period: float = 3.0
    altitude_hold: bool = True
    turn_direction: str = "right"
    # EMA smoothing factor for target_position (0 = fully smooth / sticky,
    # 1 = no smoothing).  Applied per-tick when computing LOS pan/tilt so
    # that nearest-target jumps between real vehicles and decoys produce
    # gradual gimbal motion rather than a hard snap.
    smoothing_alpha: float = 0.3


@dataclass
class LoiterTracker:
    """Computes per-tick commands for TRACK mode.

    Per-tick, when target_position is known:
      1. UAV set_destination to (lat, lon, uav.altitude) with loiter_radius
         (refreshed every loiter_refresh_period seconds)
      2. set_orientation with LOS pan/tilt derived from current UAV pose and
         target position
    """

    params: TrackerParams
    base_lat: float = 0.0
    base_lon: float = 0.0
    _last_refresh: float | None = field(default=None, init=False)
    _current_target: tuple[float, float] = field(default=(0.0, 0.0), init=False)
    # EMA-smoothed target position (per-tick, used for LOS computation)
    _smoothed_lat: float | None = field(default=None, init=False)
    _smoothed_lon: float | None = field(default=None, init=False)

    def __post_init__(self):
        # __post_init__ only fires on direct construction; reset() handles the runtime case
        if not hasattr(self, "_last_refresh") or self._last_refresh is None:
            self._last_refresh = None

    def reset(self, base_lat: float, base_lon: float) -> None:
        self.base_lat = base_lat
        self.base_lon = base_lon
        self._last_refresh = None
        self._current_target = (base_lat, base_lon)
        self._smoothed_lat = None
        self._smoothed_lon = None

    def __post_init__(self):
        # Sentinel: None means "never refreshed" so first frame always refreshes.
        if not hasattr(self, "_last_refresh") or self._last_refresh is None:
            self._last_refresh = None

    def commands(self, sim_time: float, uav_lat: float, uav_lon: float, uav_alt: float,
                 uav_yaw: float, tgt_lat: float, tgt_lon: float, tgt_alt: float):
        # Refresh loiter center on first call (last_refresh is None) or when period elapsed
        if self._last_refresh is None or sim_time - self._last_refresh >= self.params.loiter_refresh_period:
            self._current_target = (tgt_lat, tgt_lon)
            self._last_refresh = sim_time
        lat, lon = self._current_target

        # ── EMA smoothing of per-tick target position ──
        # When detection jumps between real vehicles and nearby decoys,
        # the raw tgt_lat/tgt_lon can change abruptly per tick.  Apply an
        # exponential moving average so the LOS pan/tilt computed below
        # changes gradually rather than snapping.  The loiter centre above
        # is deliberately NOT smoothed — that refreshes on a fixed timer
        # (loiter_refresh_period) and should track the latest target centre.
        alpha = self.params.smoothing_alpha
        if self._smoothed_lat is None:
            self._smoothed_lat = tgt_lat
            self._smoothed_lon = tgt_lon
        else:
            self._smoothed_lat += alpha * (tgt_lat - self._smoothed_lat)
            self._smoothed_lon += alpha * (tgt_lon - self._smoothed_lon)
        smooth_lat = self._smoothed_lat
        smooth_lon = self._smoothed_lon

        # Compute LOS
        pan, tilt = los_angles(
            uav_lat, uav_lon, uav_alt, uav_yaw,
            smooth_lat, smooth_lon, tgt_alt,
        )
        cmds = [
            {
                "target": "uav",
                "cmd": "set_destination",
                "params": {
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": uav_alt,
                    "loiter_radius": self.params.loiter_radius,
                    "turn_direction": self.params.turn_direction,
                },
            },
            {
                "target": "uav",
                "cmd": "component.gimbal_tracking.set_orientation",
                "params": {"pan": pan, "tilt": tilt},
            },
        ]
        return cmds
