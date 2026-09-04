"""RedMaple cooperative tracking controller."""


class CooperativeTracker:
    """Convert target assignment into an observation point.

    Command generation stays in agent.py.
    """

    def __init__(self):
        self.target_id = None
        self.role = "SEARCHER"

    def assign(self, target_id, role="SEARCHER"):
        self.target_id = target_id
        self.role = role

    def clear(self):
        self.target_id = None
        self.role = "SEARCHER"

    def command_point(self, target, uid):
        if target is None:
            return None

        try:
            slot = int(uid) % 3
        except ValueError:
            slot = 0

        if self.role == "LEADER":
            offset = (0.0, 0.0)
        elif self.role == "FOLLOWER":
            offset = (-0.00015, 0.00025 if slot == 1 else -0.00025)
        else:
            offset = (0.0005, 0.0005)

        return target.lat + offset[0], target.lon + offset[1]

    track_point = command_point
