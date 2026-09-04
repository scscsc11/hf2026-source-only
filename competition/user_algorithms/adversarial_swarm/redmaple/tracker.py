"""RedMaple V2 cooperative tracking controller.

The tracker only decides where a UAV should move around a target.
Command construction stays in the agent layer to avoid SDK coupling.
"""


class CooperativeTracker:
    def __init__(self):
        self.target_id = None
        self.role = "SEARCHER"

    def assign(self, target_id, role="FOLLOWER"):
        self.target_id = target_id
        self.role = role

    def clear(self):
        self.target_id = None
        self.role = "SEARCHER"

    def offset(self, uid):
        """Simple three-slot observation geometry."""
        slot = int(uid) % 3 if str(uid).isdigit() else 0
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

    def track_point(self, target, uid):
        return self.command_point(target, uid)
