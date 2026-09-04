"""RedMaple V1 - cooperative adversarial swarm agent.

Based on the official adversarial swarm baseline ideas:
- local target memory
- confidence fusion
- distributed target selection
- cooperative tracking state

This is the first real development version.
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


SEARCH = "SEARCH"
TRACK = "TRACK"


class RedMapleAgent(SwarmAgent):
    def configure(self, config) -> None:
        self.alt = 500.0
        self.search_speed = 25.0
        self.track_speed = 30.0
        self.fov = 30.0
        self.tick = 0
        self.state = SEARCH
        self.targets: Dict[str, dict] = {}
        self.current_target = None

    def reset(self) -> None:
        self.tick = 0
        self.state = SEARCH
        self.targets = {}
        self.current_target = None

    def _distance(self, lat1, lon1, lat2, lon2):
        r = 6371000
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2-lat1)
        dl = math.radians(lon2-lon1)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return 2*r*math.asin(math.sqrt(a))

    def _target_key(self, lat, lon):
        return f'{round(lat,5)}_{round(lon,5)}'

    def _fuse_detection(self, det):
        if not getattr(det, 'detected', False):
            return
        lat = getattr(det, 'target_lat', None)
        lon = getattr(det, 'target_lon', None)
        if lat is None or lon is None:
            return
        key = self._target_key(lat, lon)
        t = self.targets.get(key, {
            'lat': lat,
            'lon': lon,
            'confidence': 0.0,
            'seen': 0,
            'claimed': 0,
        })
        t['lat'] = lat
        t['lon'] = lon
        t['seen'] += 1
        t['confidence'] = min(1.0, t['confidence'] + 0.2)
        self.targets[key] = t

    def _receive(self, obs):
        for msg in getattr(obs, 'comm_inbox', []) or []:
            payload = getattr(msg, 'payload', '')
            if not payload.startswith('T:'):
                continue
            try:
                _, lat, lon, conf = payload.split(':')
                key = self._target_key(float(lat), float(lon))
                self.targets[key] = {
                    'lat': float(lat),
                    'lon': float(lon),
                    'confidence': float(conf),
                    'seen': 1,
                    'claimed': 0,
                }
            except Exception:
                pass

    def _select_target(self, obs):
        best = None
        best_score = -1e9
        for key, t in self.targets.items():
            d = self._distance(obs.self.lat, obs.self.lon, t['lat'], t['lon'])
            score = t['confidence'] * 100 - d / 1000 - t['claimed'] * 20
            if score > best_score:
                best_score = score
                best = key
        return best

    def decide(self, obs, dt) -> List[Command]:
        self.tick += 1
        cmds = [set_gimbal_fov(self.fov)]

        self._receive(obs)
        for det in getattr(obs.self, 'detections', []) or []:
            self._fuse_detection(det)

        if self.tick % 20 == 0:
            for t in list(self.targets.values())[:3]:
                cmds.append(broadcast(f"T:{t['lat']}:{t['lon']}:{t['confidence']}"))

        if self.current_target not in self.targets:
            self.current_target = self._select_target(obs)

        if self.current_target:
            t = self.targets[self.current_target]
            dist = self._distance(obs.self.lat, obs.self.lon, t['lat'], t['lon'])
            self.state = TRACK
            if dist > 300:
                cmds.append(fly_to(t['lat'], t['lon'], self.alt, self.track_speed))
            else:
                cmds.append(point_gimbal(0, -20))
                if self.tick % 10 == 0:
                    cmds.append(report_target(t['lat'], t['lon'], self.current_target))
        else:
            self.state = SEARCH
            angle = (self.tick % 360) * math.pi / 180
            lat = obs.self.lat + 0.002 * math.sin(angle)
            lon = obs.self.lon + 0.002 * math.cos(angle)
            cmds.append(fly_to(lat, lon, self.alt, self.search_speed))

        return cmds
