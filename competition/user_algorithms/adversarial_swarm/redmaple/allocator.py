"""RedMaple V2 distributed task allocator."""

import math


class TargetAllocator:
    def __init__(self, uid=None):
        self.uid = uid

    def distance_cost(self, a, b):
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        return math.sqrt(dx * dx + dy * dy)

    def utility(self, target, self_pos):
        dist = self.distance_cost((target.lat, target.lon), self_pos)
        observers = getattr(target, "observers", [])
        state_bonus = 0.2 if getattr(target, "state", "") == "CONFIRMED" else 0.0
        return (
            getattr(target, "confidence", 0.0)
            + state_bonus
            - 0.001 * dist
            - 0.15 * len(observers)
        )

    def choose_target(self, targets, self_pos):
        if not targets:
            return None
        return max(targets, key=lambda t: self.utility(t, self_pos))

    # compatibility alias
    def select(self, targets, lat, lon):
        return self.choose_target(targets, (lat, lon))
