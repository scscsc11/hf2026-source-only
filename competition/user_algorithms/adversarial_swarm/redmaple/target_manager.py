"""RedMaple target management module.

Maintains local target belief for distributed adversarial swarm control.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import math


@dataclass
class TargetRecord:
    target_id: str
    lat: float
    lon: float
    confidence: float = 0.0
    last_seen: float = 0.0
    state: str = "UNKNOWN"
    observers: List[str] = field(default_factory=list)
    assigned: List[str] = field(default_factory=list)


class TargetManager:
    def __init__(self):
        self.targets: Dict[str, TargetRecord] = {}

    def _key(self, lat: float, lon: float) -> str:
        return f"{round(lat, 5)}_{round(lon, 5)}"

    def update_detection(self, lat: float, lon: float, confidence: float, now: float, uid: str = ""):
        key = self._key(lat, lon)
        if key not in self.targets:
            self.targets[key] = TargetRecord(key, lat, lon)

        t = self.targets[key]
        t.lat = lat
        t.lon = lon
        t.confidence = min(1.0, 0.7 * t.confidence + 0.3 * confidence + 0.05)
        t.last_seen = now
        t.state = "CONFIRMED" if t.confidence > 0.6 else "SUSPECT"

        if uid and uid not in t.observers:
            t.observers.append(uid)

        return t

    def fuse_remote(self, target_id, lat, lon, confidence, now):
        if target_id not in self.targets:
            self.targets[target_id] = TargetRecord(target_id, lat, lon)

        t = self.targets[target_id]
        t.lat = 0.7 * t.lat + 0.3 * lat
        t.lon = 0.7 * t.lon + 0.3 * lon
        t.confidence = min(1.0, 0.7 * t.confidence + 0.3 * confidence)
        t.last_seen = max(t.last_seen, now)
        return t

    def decay(self, now):
        remove = []
        for k, t in self.targets.items():
            dt = now - t.last_seen
            t.confidence *= math.exp(-0.01 * dt)
            if t.confidence < 0.05:
                remove.append(k)
        for k in remove:
            del self.targets[k]

    def best_target(self, lat, lon):
        best = None
        best_score = -1e9
        for t in self.targets.values():
            d = self.distance(lat, lon, t.lat, t.lon)
            load_penalty = len(t.assigned) * 20
            score = t.confidence * 100 - d / 1000 - load_penalty
            if score > best_score:
                best_score = score
                best = t
        return best

    @staticmethod
    def distance(lat1, lon1, lat2, lon2):
        r = 6371000
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))
