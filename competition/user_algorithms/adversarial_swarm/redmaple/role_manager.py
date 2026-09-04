"""RedMaple cooperative role assignment."""


class RoleManager:
    """Assign lightweight distributed roles for target engagement."""

    def __init__(self, uid):
        self.uid = str(uid)
        self.role = "SEARCHER"
        self.target_id = None

    def assign(self, target):
        if target is None:
            self.role = "SEARCHER"
            self.target_id = None
            return self.role

        self.target_id = target.target_id
        observers = list(getattr(target, "observers", []))
        if self.uid in observers:
            index = observers.index(self.uid)
        else:
            index = len(observers)

        if index == 0:
            self.role = "LEADER"
        elif index < 3:
            self.role = "FOLLOWER"
        else:
            self.role = "SEARCHER"

        return self.role
