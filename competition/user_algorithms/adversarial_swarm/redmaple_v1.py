"""RedMaple V1 - adversarial swarm agent.

First competition version. Keeps the official SEARCH/ACQUIRE/TRACK idea,
adds local target memory, confidence scoring and simple distributed target
selection.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from competition.sdk.core.commands import (
    Command,
    broadcast,
    fly_to,
    point_gimbal,
    report_target,
    set_gimbal_fov,
)
from competition.sdk.scenarios.adversarial_swarm import SwarmAgent
from competition.sdk.scenarios.adversarial_swarm.observation import SwarmObs


SEARCH = "SEARCH"
TRACK = "TRACK"


class RedMapleAgent(SwarmAgent):
    """Distributed cooperative search and tracking agent."""

    def configure(self, config) -> None:
        self.alt = 500.0
        self.speed = 25.0
        self.track_speed = 28.0
        self.fov = 30.0
        self.state = SEARCH
        self.targets: Dict[str, dict] = {}
        self.current_target: Optional[str] = None
        self.tick = 0
        self.time = 0.0
        self.search_point = None

    def reset(self) -> None:
        self.state = SEARCH
        self.targets = {}
        self.current_target = None
        self.tick = 0
        self.time = 0.0
        self.search_point = None

    def _distance(self, a, b, c, d):
        r = 6371000
        p1 = math.radians(a)
        p2 = math.radians(c)
        dp = math.radians(c - a)
        dl = math.radians(d - b)
        x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(x))

    def _update_memory(self, obs):
        detections = getattr(obs.self, "detections", None) or []
        if not detections:
            return

        for d in detections:
            if not getattr(d, "detected", False):
                continue
            lat = getattr(d, "target_lat", None)
            lon = getattr(d, "target_lon", None)
            if lat is None or lon is None:
                continue

            key = f"{round(lat,5)}_{round(lon,5)}"
            item = self.targets.get(key, {
                "lat": lat,
                "lon": lon,
                "confidence": 0.0,
                "count": 0,
            })
            item["lat"] = lat
            item["lon"] = lon
            item["count"] += 1
            item["confidence"] = min(1.0, item["confidence"] + 0.15)
            self.targets[key] = item

            if self.tick % 20 == 0:
                broadcast(f"T:{lat:.6f},{lon:.6f},{item['confidence']:.2f}")

    def _receive(self, obs):
        for msg in getattr(obs, "comm_inbox", []) or []:
            payload = getattr(msg, "payload", "")
            if not payload.startswith("T:"):
                continue
            try:
                _, lat, lon, conf = payload.split(":")
                key = f"{round(float(lat),5)}_{round(float(lon),5)}"
                self.targets[key] = {
                    "lat": float(lat),
                    "lon": float(lon),
                    "confidence": float(conf),
                    "count": 1,
                }
            except Exception:
                continue

    def _select_target(self, obs):
        if not self.targets:
            return None
        best = None
        best_score = -1e9
        for k, t in self.targets.items():
            dist = self._distance(
                obs.self.lat,
                obs.self.lon,
                t["lat"],
                t["lon"],
            )
            score = t["confidence"] * 100.0 - dist / 1000.0
            if score > best_score:
                best_score = score
                best = k
        return best

    def decide(self, obs: SwarmObs, dt: float) -> List[Command]:
        self.tick += 1
        self.time += dt
        cmds: List[Command] = [set_gimbal_fov(self.fov)]

        self._receive(obs)
        self._update_memory(obs)

        if self.current_target not in self.targets:
            self.current_target = self._select_target(obs)

        if self.current_target is not None:
            t = self.targets[self.current_target]
            dist = self._distance(obs.self.lat, obs.self.lon, t["lat"], t["lon"])

            if dist > 400:
                self.state = TRACK
                cmds.append(fly_to(t["lat"], t["lon"], self.alt, self.track_speed))
            else:
                cmds.append(point_gimbal(0.0, -20.0))
                if self.tick % 15 == 0:
                    cmds.append(report_target(t["lat"], t["lon"], self.current_target))
        else:
            self.state = SEARCH
            if self.search_point is None:
                self.search_point = (obs.self.lat, obs.self.lon)
            cmds.append(fly_to(
                self.search_point[0],
                self.search_point[1],
                self.alt,
                self.speed,
            ))

        return cmds
