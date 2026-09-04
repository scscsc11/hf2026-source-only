"""RedMaple V2 distributed task allocator."""

import math


class TargetAllocator:
    def __init__(self, uid):
        self.uid = uid

    def distance_cost(self, a, b):
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        return math.sqrt(dx * dx + dy * dy)

    def utility(self, target, self_pos):
        dist = self.distance_cost((target.lat, target.lon), self_pos)
        load_penalty = len(getattr(target, "observers", [])) * 0.15
        return target.confidence - 0.001 * dist - load_penalty

    def choose_target(self, targets, self_pos):
        if not targets:
            return None
        return max(targets, key=lambda t: self.utility(t, self_pos))
