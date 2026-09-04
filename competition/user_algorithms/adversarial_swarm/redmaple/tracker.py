"""RedMaple V2 tracking controller."""


class CooperativeTracker:
    def __init__(self):
        self.role = "SEARCHER"
        self.target_id = None

    def assign(self, target_id, role="FOLLOWER"):
        self.target_id = target_id
        self.role = role

    def clear(self):
        self.target_id = None
        self.role = "SEARCHER"

    def offset(self, uid):
        # deterministic role separation
        slot = uid % 3
        if slot == 0:
            return 0.0003, 0.0
        if slot == 1:
            return -0.00015, 0.00025
        return -0.00015, -0.00025

    def command_point(self, target, uid):
        if target is None:
            return None
        dx, dy = self.offset(uid)
        return target.lat + dx, target.lon + dy
