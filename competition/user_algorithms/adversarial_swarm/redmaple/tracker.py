"""RedMaple V2 cooperative tracking controller."""


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

    def track(self, target, me):
        point = self.command_point(target, getattr(me, "uid", 0))
        if point is None:
            return []
        return [self._make_command(point, me)]

    def _make_command(self, point, me):
        return me.agent.fly_to(point[0], point[1], getattr(me, "alt", 120.0))
