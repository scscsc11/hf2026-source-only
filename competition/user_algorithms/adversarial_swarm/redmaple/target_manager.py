"""RedMaple target management module.

Maintains local target belief for distributed adversarial swarm control.
"""

from dataclasses import dataclass, field
from typing import Dict, List
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

    def clear(self):
        self.targets.clear()

    def _key(self, lat: float, lon: float) -> str:
        return f"{round(lat, 5)}_{round(lon, 5)}"

    def update_detection(self, lat: float, lon: float, confidence: float, now: float, uid: str = ""):
        key = self._key(lat, lon)
        if key not in self.targets:
            self.targets[key] = TargetRecord(key, lat, lon)

        target = self.targets[key]
        target.lat = lat
        target.lon = lon
        target.confidence = min(1.0, 0.7 * target.confidence + 0.3 * confidence + 0.05)
        target.last_seen = now
        target.state = "CONFIRMED" if target.confidence > 0.6 else "SUSPECT"

        if uid and uid not in target.observers:
            target.observers.append(uid)
        return target

    def fuse_remote(self, target_id, lat, lon, confidence, now):
        if target_id not in self.targets:
            self.targets[target_id] = TargetRecord(target_id, lat, lon)

        target = self.targets[target_id]
        target.lat = 0.7 * target.lat + 0.3 * lat
        target.lon = 0.7 * target.lon + 0.3 * lon
        target.confidence = min(1.0, 0.7 * target.confidence + 0.3 * confidence)
        target.last_seen = max(target.last_seen, now)
        target.state = "CONFIRMED" if target.confidence > 0.6 else "SUSPECT"
        return target

    def claim(self, target_id, uid):
        target = self.targets.get(target_id)
        if target and uid not in target.assigned:
            target.assigned.append(uid)

    def release(self, target_id, uid):
        target = self.targets.get(target_id)
        if target and uid in target.assigned:
            target.assigned.remove(uid)

    def decay(self, now):
        remove = []
        for key, target in self.targets.items():
            dt = max(0.0, now - target.last_seen)
            target.confidence *= math.exp(-0.01 * dt)
            if target.confidence < 0.05:
                remove.append(key)
        for key in remove:
            del self.targets[key]

    def best_target(self, lat, lon):
        best = None
        best_score = -1e9
        for target in self.targets.values():
            distance = self.distance(lat, lon, target.lat, target.lon)
            score = (
                target.confidence * 100
                - distance / 1000
                - len(target.assigned) * 20
            )
            if score > best_score:
                best_score = score
                best = target
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
