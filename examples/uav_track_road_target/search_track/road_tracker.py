"""Road-following tracker controller — UAV chases a target along A* road waypoints.

Strategy:
  1. Send ``set_goal`` for each road waypoint; the A* planner component
     computes a path and pushes it to the trajectory component automatically.
  2. The trajectory component (auto-advance) drives the target along the
     A* path.  When the path ends, the target stops (arrival latch at final wp).
  3. Python detects the stall and sends ``set_goal`` for the next waypoint.
  4. If ``loop`` is true, restart from the first waypoint after the last.
  5. The UAV continuously receives ``set_destination`` commands to chase
     behind the target, plus gimbal orientation updates.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EARTH_RADIUS_M = 6_371_000.0


# ── Geometry helpers ──────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def los_angles(
    uav_lat: float, uav_lon: float, uav_alt: float, uav_yaw: float,
    tgt_lat: float, tgt_lon: float, tgt_alt: float,
) -> tuple[float, float]:
    brg = bearing_deg(uav_lat, uav_lon, tgt_lat, tgt_lon)
    d_h = haversine_m(uav_lat, uav_lon, tgt_lat, tgt_lon)
    elv = math.degrees(math.atan2(tgt_alt - uav_alt, d_h)) if d_h > 1e-6 else 0.0
    pan = ((brg - uav_yaw + 540.0) % 360.0) - 180.0
    return pan, elv


def offset_position(lat: float, lon: float, bearing: float, dist_m: float) -> tuple[float, float]:
    phi = math.radians(lat)
    dlat = dist_m * math.cos(math.radians(bearing)) / EARTH_RADIUS_M
    dlon = dist_m * math.sin(math.radians(bearing)) / (EARTH_RADIUS_M * math.cos(phi))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


# ── Road / Waypoint loading ──────────────────────────────────────────────

@dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float
    wait: float
    label: str


def load_road(points_path: str | Path, road_name: str) -> dict[str, Any]:
    with open(points_path, encoding="utf-8-sig") as f:
        data = json.load(f)
    for p in data["Paths"]:
        if p["Name"] == road_name:
            return p
    names = [p["Name"] for p in data["Paths"]]
    raise ValueError(f"Road '{road_name}' not found in {points_path}. Available: {names}")


def build_waypoint_list(road: dict[str, Any]) -> list[Waypoint]:
    wps: list[Waypoint] = []
    start = road["Start"]
    wps.append(Waypoint(
        lat=start["Latitude"], lon=start["Longitude"],
        wait=start.get("WaitTime", 0.0), label="Start",
    ))
    for i, wp in enumerate(road["Waypoints"]):
        wps.append(Waypoint(
            lat=wp["Latitude"], lon=wp["Longitude"],
            wait=wp.get("WaitTime", 0.0), label=f"WPT[{i}]",
        ))
    end = road["End"]
    wps.append(Waypoint(
        lat=end["Latitude"], lon=end["Longitude"],
        wait=end.get("WaitTime", 0.0), label="End",
    ))
    return wps


# ── RoadTracker controller ───────────────────────────────────────────────

@dataclass
class RoadTrackerConfig:
    target_speed: float = 10.0
    loop: bool = True
    chase_altitude_agl: float = 400.0
    follow_distance: float = 300.0
    chase_interval: float = 2.0
    loiter_radius: float = 200.0

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RoadTrackerConfig":
        import yaml
        with open(path, encoding="utf-8-sig") as f:
            raw = yaml.safe_load(f) or {}
        known = {field.name for field in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in raw.items() if k in known}
        return cls(**filtered)


class RoadTracker:
    """Manages target waypoint navigation via A* set_goal and UAV chase.

    State machine per waypoint:
      NAV   — target navigating toward current road waypoint via A*
      WAIT  — target arrived, waiting WaitTime seconds
      DONE  — last waypoint reached, no loop

    Arrival detection: when the A* path finishes, the trajectory component
    latches at the final path waypoint and the target stops moving.  We
    detect this stall (position unchanged for *still_ticks* consecutive
    ticks) and advance to the next road waypoint.
    """

    # Stall detection params
    STILL_TICKS = 15          # consecutive ticks target must be still
    STILL_RADIUS_M = 2.0      # max displacement to count as "still"
    SETTLE_MOVE_M = 15.0      # target must move this far before stall check begins

    def __init__(self, waypoints: list[Waypoint], cfg: RoadTrackerConfig) -> None:
        self.waypoints = waypoints
        self.cfg = cfg
        # Skip the Start waypoint — the target already spawns there.
        # Starting from wp_idx=1 avoids the "already at goal, never settled" stall.
        self.wp_idx: int = 1 if len(waypoints) > 1 else 0
        self._phase: str = "NAV"   # NAV | WAIT | DONE
        self._wait_until: float = 0.0
        self._goal_sent: bool = False
        self._last_chase: float = 0.0
        self._gimbal_enabled: bool = False
        self._target_speed_set: bool = False
        self._lap_count: int = 0
        # Stall detection
        self._still_count: int = 0
        self._ref_lat: float = 0.0
        self._ref_lon: float = 0.0
        self._settled: bool = False   # target has started moving after set_goal

    @property
    def current_waypoint(self) -> Waypoint | None:
        if 0 <= self.wp_idx < len(self.waypoints):
            return self.waypoints[self.wp_idx]
        return None

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def lap_count(self) -> int:
        return self._lap_count

    def reset(self) -> None:
        self.wp_idx = 0
        self._phase = "NAV"
        self._wait_until = 0.0
        self._goal_sent = False
        self._last_chase = 0.0
        self._gimbal_enabled = False
        self._target_speed_set = False
        self._lap_count = 0
        self._still_count = 0
        self._ref_lat = 0.0
        self._ref_lon = 0.0
        self._settled = False

    def decide(
        self,
        *,
        sim_time: float,
        wall_time: float,
        tgt_lat: float, tgt_lon: float, tgt_alt: float, tgt_heading: float,
        uav_lat: float, uav_lon: float, uav_alt: float, uav_yaw: float,
        publish_fn,
    ) -> list[dict[str, Any]]:
        cmds: list[dict[str, Any]] = []
        wp = self.current_waypoint

        # ── One-time setup ────────────────────────────────────────────
        if not self._gimbal_enabled:
            publish_fn("uav", "component.gimbal_tracking.set_enabled", {"enabled": True})
            publish_fn("uav", "component.gimbal_tracking.set_target_entity",
                       {"entity_name": "target"})
            self._gimbal_enabled = True

        if not self._target_speed_set:
            publish_fn("target", "set_speed", {"speed": self.cfg.target_speed})
            self._target_speed_set = True

        # ── Waypoint navigation via set_goal ──────────────────────────
        if wp is not None:
            if not self._goal_sent and self._phase == "NAV":
                publish_fn("target", "set_goal", {
                    "latitude": wp.lat,
                    "longitude": wp.lon,
                })
                self._goal_sent = True
                self._settled = False
                self._still_count = 0
                self._ref_lat = tgt_lat
                self._ref_lon = tgt_lon
                cmds.append({"cmd": "set_goal", "wp": wp.label,
                             "lat": wp.lat, "lon": wp.lon})

            # Arrival detection via stall
            if self._goal_sent and self._phase == "NAV":
                if self._check_stall(tgt_lat, tgt_lon):
                    d = haversine_m(tgt_lat, tgt_lon, wp.lat, wp.lon)
                    if wp.wait > 0:
                        self._phase = "WAIT"
                        self._wait_until = wall_time + wp.wait
                        cmds.append({"cmd": "arrived", "wp": wp.label,
                                     "dist": d, "wait": wp.wait})
                    else:
                        cmds.append({"cmd": "arrived", "wp": wp.label, "dist": d})
                        self._advance_waypoint()

            # Wait expiry
            if self._phase == "WAIT" and wall_time >= self._wait_until:
                self._advance_waypoint()

        # ── UAV chase ─────────────────────────────────────────────────
        if (wall_time - self._last_chase) >= self.cfg.chase_interval:
            behind_bearing = (tgt_heading + 180.0) % 360.0
            fl_lat, fl_lon = offset_position(
                tgt_lat, tgt_lon, behind_bearing, self.cfg.follow_distance,
            )
            chase_alt = (tgt_alt or 0.0) + self.cfg.chase_altitude_agl
            publish_fn("uav", "set_destination", {
                "latitude": fl_lat,
                "longitude": fl_lon,
                "altitude": chase_alt,
                "loiter_radius": self.cfg.loiter_radius,
                "turn_direction": "right",
            })
            self._last_chase = wall_time

            pan, tilt = los_angles(
                uav_lat, uav_lon, chase_alt, uav_yaw,
                tgt_lat, tgt_lon, tgt_alt,
            )
            publish_fn("uav", "component.gimbal_tracking.set_orientation",
                       {"pan": pan, "tilt": tilt})

        return cmds

    def _check_stall(self, tgt_lat: float, tgt_lon: float) -> bool:
        """Detect target stall (A* path finished, trajectory latched)."""
        d_from_ref = haversine_m(tgt_lat, tgt_lon, self._ref_lat, self._ref_lon)

        # Grace period: wait for target to start moving after set_goal
        if not self._settled:
            if d_from_ref > self.SETTLE_MOVE_M:
                self._settled = True
                self._still_count = 0
                self._ref_lat = tgt_lat
                self._ref_lon = tgt_lon
            return False

        # Count consecutive still ticks
        if d_from_ref <= self.STILL_RADIUS_M:
            self._still_count += 1
        else:
            self._still_count = 0
            self._ref_lat = tgt_lat
            self._ref_lon = tgt_lon

        return self._still_count >= self.STILL_TICKS

    def _advance_waypoint(self) -> None:
        self.wp_idx += 1
        if self.wp_idx >= len(self.waypoints):
            if self.cfg.loop:
                self._lap_count += 1
                self.wp_idx = 0
            else:
                self.wp_idx = len(self.waypoints) - 1
                self._phase = "DONE"
                return
        self._phase = "NAV"
        self._goal_sent = False
