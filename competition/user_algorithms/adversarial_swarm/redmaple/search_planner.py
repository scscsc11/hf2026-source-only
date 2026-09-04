"""RedMaple V2 search planner.

Deterministic multi-UAV coverage pattern.
"""

import math


class SearchPlanner:
    def __init__(self, uid):
        self.uid = str(uid)
        self.step = 0

    def reset(self):
        self.step = 0

    def next_point(self, me, briefing=None):
        self.step += 1
        uid_num = sum(ord(c) for c in self.uid) % 10
        ring = self.step % 30
        angle = (uid_num * 0.62) + ring * 0.18
        radius = 0.002 + ring * 0.00005

        lat = getattr(me, "lat", 0.0) + radius * math.cos(angle)
        lon = getattr(me, "lon", 0.0) + radius * math.sin(angle)
        return lat, lon
