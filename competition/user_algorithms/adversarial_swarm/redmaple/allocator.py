"""RedMaple RC2 distributed task allocator.

Stable runtime allocator. Avoids swarm collapse by penalizing claimed targets.
"""

import math


class TargetAllocator:
    def __init__(self, uid=None):
        self.uid = str(uid) if uid is not None else None

    def distance_cost(self, a, b):
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        return math.sqrt(dx * dx + dy * dy)

    def utility(self, target, self_pos):
        dist = self.distance_cost((target.lat, target.lon), self_pos)
        assigned = getattr(target, "assigned", [])
        observers = getattr(target, "observers", [])

        if self.uid in assigned:
            own_bonus = 0.25
        else:
            own_bonus = 0.0

        return (
            getattr(target, "confidence", 0.0)
            + own_bonus
            + (0.2 if getattr(target, "state", "") == "CONFIRMED" else 0.0)
            - 0.001 * dist
            - 0.12 * max(0, len(observers) - 3)
            - 0.18 * len(assigned)
        )

    def choose_target(self, targets, self_pos):
        if not targets:
            return None
        return max(targets, key=lambda t: self.utility(t, self_pos))

    def select(self, targets, lat, lon):
        return self.choose_target(targets, (lat, lon))
