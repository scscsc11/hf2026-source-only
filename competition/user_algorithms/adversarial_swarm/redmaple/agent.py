"""RedMaple V2 main agent entry."""

from typing import List

from competition.sdk.scenarios.adversarial_swarm.agent import SwarmAgent
from competition.sdk.core.commands import Command

from .target_manager import TargetManager
from .communication import encode_target, decode_message
from .allocator import TargetAllocator
from .tracker import CooperativeTracker
from .search_planner import SearchPlanner


class RedMapleAgent(SwarmAgent):
    def __init__(self, my_uid: str):
        super().__init__(my_uid)
        self.uid = my_uid
        self.targets = TargetManager()
        self.allocator = TargetAllocator(my_uid)
        self.tracker = CooperativeTracker()
        self.search = SearchPlanner(my_uid)

    def configure(self, config: dict):
        self.config = config or {}

    def reset(self):
        self.targets.clear()
        self.tracker.clear()
        self.search.reset()

    def decide(self, obs, dt: float) -> List[Command]:
        cmds = []
        me = obs.self

        for msg in getattr(obs, "comm_inbox", []):
            payload = getattr(msg, "payload", msg)
            data = decode_message(payload)
            if data:
                self.targets.fuse_remote(data)

        detections = getattr(me, "detections", []) or []
        self.targets.update_local(detections, getattr(obs, "time", 0.0))

        best = self.targets.best_target()
        if best is not None:
            cmds.append(self.broadcast(encode_target(best)))

        target = self.allocator.choose_target(
            self.targets.all(),
            (getattr(me, "lat", 0.0), getattr(me, "lon", 0.0)),
        )

        if target is not None:
            point = self.tracker.command_point(target, self.uid)
            if point:
                cmds.append(self.fly_to(point[0], point[1], getattr(me, "alt", 120.0)))
        else:
            lat, lon = self.search.next_point(me, getattr(obs, "briefing", None))
            cmds.append(self.fly_to(lat, lon, getattr(me, "alt", 120.0)))

        return cmds
