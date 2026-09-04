"""RedMaple RC2 cooperative tracking controller.

Tracker converts target belief and assigned role into observation geometry.
Command construction remains in agent layer.
"""


class CooperativeTracker:
    def __init__(self):
        self.target_id = None
        self.role = "SEARCHER"

    def assign(self, target_id, role="SEARCHER"):
        self.target_id = target_id
        self.role = role

    def clear(self):
        self.target_id = None
        self.role = "SEARCHER"

    def offset(self, uid):
        try:
            slot = int(uid) % 3
        except Exception:
            slot = 0

        if self.role == "LEADER":
            return 0.0, 0.0

        if self.role == "FOLLOWER":
            if slot == 1:
                return -0.00015, 0.00025
            return -0.00015, -0.00025

        return 0.0005, 0.0005

    def command_point(self, target, uid):
        if target is None:
            return None
        dx, dy = self.offset(uid)
        return target.lat + dx, target.lon + dy

    def track_point(self, target, uid):
        return self.command_point(target, uid)
