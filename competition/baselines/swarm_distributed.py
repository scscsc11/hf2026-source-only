"""Baseline agent for the adversarial-swarm scenario.

A distributed agent under strict isolation. Coordination and threat
awareness use ONLY:
  * obs.briefing.approximate_zones — pre-match-known STATIC threats (SAM,
    static jam) exposed as coarse bbox+area (exact polygons are no longer
    given). Routes avoid these by climbing above the zone's alt_max
    whenever a planned point falls inside a zone bbox (conservative — the
    bbox is expanded larger than the true region, so entering it triggers
    avoidance).
  * obs.self.jammed — dynamic jam self-perception; when jammed, broadcast a
    warning so teammates can detour.
  * obs.comm_inbox — teammate target shares ("T:lat,lon") and jam warnings.

Strategy:
  * SEARCH: continuous Archimedean spiral offset by a per-UAV phase (uid
    hash) so the 10-UAV fleet fans out. Gimbal sweeps.
  * AVOID STATIC THREATS: if a planned point is inside a briefing zone
    bbox, climb above that zone's alt_max (blind avoidance).
  * DYNAMIC-JAM SENSE: when obs.self.jammed, broadcast "J:lat,lon".
  * TARGET SHARE: on confirmed detection, broadcast "T:lat,lon"; on
    receiving one, converge to co-track (meet the K gate).
"""
from __future__ import annotations

import hashlib
import math
from typing import List

from competition.sdk.core.commands import (Command, broadcast, fly_to,
                                           point_gimbal, report_target)
from competition.sdk.scenarios.adversarial_swarm import SwarmAgent
from competition.sdk.scenarios.adversarial_swarm.observation import SwarmObs


def _uid_phase(uid: str) -> float:
    h = int(hashlib.md5(uid.encode()).hexdigest(), 16)
    return (h % 1000) / 1000.0


def _in_any_approx_zone(lat, lon, briefing):
    """点是否落在任一近似区域 bbox 内（保守规避）。返回区域或 None。

    approximate_zones 暴露的是外扩 bbox（比真区域大一圈），点在 bbox
    内即视为受威胁——保守触发避险更安全。
    """
    for z in getattr(briefing, "approximate_zones", ()) or ():
        (lat_min, lon_min), (lat_max, lon_max) = z.bbox
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return z   # 返回区域用于读 alt_max
    return None


class SwarmDistributedAgent(SwarmAgent):
    """Distributed swarm agent with static-threat avoidance."""

    def configure(self, config) -> None:
        self._search_alt: float = 500.0
        self._evade_alt: float = 3000.0   # above SAM alt_max (2500)
        self._search_radius: float = 900.0
        self._growth: float = 40.0         # m per revolution
        self._ang_speed: float = 25.0      # deg/s
        self._sweep_period: float = 4.0
        self._pitch_min: float = -65.0
        self._pitch_max: float = -35.0
        # decoy rejection by motion (obs disguises decoys as ground_vehicle).
        self._motion_window: int = 20
        self._move_thresh: float = 2.0e-4   # ~22 m over the window
        # state
        self._home_lat = 0.0
        self._home_lon = 0.0
        self._t: float = 0.0
        self._phase: float = _uid_phase(self.my_uid)
        self._confirmed_lat: float | None = None
        self._confirmed_lon: float | None = None
        self._det_window: list = []
        self._shared_target: tuple[float, float] | None = None
        self._bc_counter: int = 0

    def reset(self) -> None:
        self._t = 0.0
        self._confirmed_lat = None
        self._confirmed_lon = None
        self._det_window = []
        self._shared_target = None
        self._bc_counter = 0
        self._home_lat = 0.0
        self._home_lon = 0.0

    def decide(self, obs: SwarmObs, dt: float) -> List[Command]:
        if self._home_lat == 0.0:
            self._home_lat = obs.self.lat
            self._home_lon = obs.self.lon

        # ingest messages
        for m in obs.comm_inbox:
            payload = m.payload
            if payload.startswith("T:") and self._shared_target is None:
                try:
                    lat, lon = payload[2:].split(",")
                    self._shared_target = (float(lat), float(lon))
                except Exception:
                    pass

        det = obs.self.detection
        cmds: List[Command] = []

        # Decoy rejection by motion: obs disguises misidentified decoys as
        # "ground_vehicle", so type can't be trusted. Confirm only detections
        # that actually move over a window (real targets travel; decoys sit).
        if det.detected and det.target_lat is not None:
            self._det_window.append((det.target_lat, det.target_lon))
            if len(self._det_window) > self._motion_window:
                self._det_window.pop(0)
            if len(self._det_window) >= self._motion_window:
                lats = [p[0] for p in self._det_window]
                lons = [p[1] for p in self._det_window]
                move = ((max(lats) - min(lats)) ** 2
                        + (max(lons) - min(lons)) ** 2) ** 0.5
                if move > self._move_thresh:
                    self._confirmed_lat = det.target_lat
                    self._confirmed_lon = det.target_lon
                    if self._bc_counter % 10 == 0:
                        cmds.append(broadcast(
                            f"T:{det.target_lat:.5f},{det.target_lon:.5f}"))
        else:
            self._det_window.clear()
        self._bc_counter += 1

        # dynamic-jam self-perception → broadcast warning
        if obs.self.jammed and self._bc_counter % 10 == 0:
            cmds.append(broadcast(
                f"J:{obs.self.lat:.5f},{obs.self.lon:.5f}"))

        # choose action
        tgt = None
        if self._confirmed_lat is not None:
            tgt = (self._confirmed_lat, self._confirmed_lon)
        elif self._shared_target is not None:
            tgt = self._shared_target

        if tgt is not None:
            lat, lon = tgt
            zone = _in_any_approx_zone(lat, lon, obs.briefing)
            # inside an approx-zone bbox → climb above that zone's alt_max
            # (conservative; _evade_alt floors the safe altitude)
            alt = (max(self._evade_alt, zone.alt_max + 1.0)
                   if zone is not None else self._search_alt)
            cmds.append(fly_to(lat, lon, alt=alt, speed=30.0))
            cmds.append(point_gimbal(0.0, -60.0))
            if self._bc_counter % 10 == 0:
                cmds.append(report_target(lat, lon))
            return cmds

        # SEARCH: phase-offset spiral, avoiding static threats by climbing
        self._t += dt
        lat, lon, pan, tilt = self._spiral()
        zone = _in_any_approx_zone(lat, lon, obs.briefing)
        alt = (max(self._evade_alt, zone.alt_max + 1.0)
               if zone is not None else self._search_alt)
        cmds.append(fly_to(lat, lon, alt=alt, speed=30.0))
        cmds.append(point_gimbal(pan, tilt))
        return cmds

    def _spiral(self) -> tuple[float, float, float, float]:
        t = self._t + self._phase * 14.0   # ~14s phase spread across 10 UAVs
        bearing = (self._ang_speed * t) % 360.0
        revs = (self._ang_speed * t) / 360.0
        radius = max(1.0, min(self._search_radius, self._growth * revs))
        dlat = (radius * math.cos(math.radians(bearing))) / 111320.0
        dlon = (radius * math.sin(math.radians(bearing))) / \
               (111320.0 * math.cos(math.radians(self._home_lat)))
        phase = (t % self._sweep_period) / self._sweep_period
        tilt = self._pitch_min + (self._pitch_max - self._pitch_min) * 0.5 * \
               (1 - math.cos(2 * math.pi * phase))
        pan_phase = (t % (self._sweep_period * 2)) / (self._sweep_period * 2)
        pan = -90.0 + 180.0 * 0.5 * (1 - math.cos(2 * math.pi * pan_phase))
        return self._home_lat + dlat, self._home_lon + dlon, pan, tilt
