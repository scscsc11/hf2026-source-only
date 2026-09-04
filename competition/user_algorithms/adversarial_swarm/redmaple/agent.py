"""RedMaple V2 main agent entry."""

from typing import List

from competition.sdk.scenarios.adversarial_swarm.agent import SwarmAgent
from competition.sdk.core.commands import Command

from .target_manager import TargetManager
from .communication import encode_target, decode_message
from .allocator import TargetAllocator
from .tracker import CooperativeTracker


class RedMapleAgent(SwarmAgent):
    def __init__(self, my_uid: str):
        super().__init__(my_uid)
        self.uid = my_uid
        self.targets = TargetManager()
        self.allocator = TargetAllocator(my_uid)
        self.tracker = CooperativeTracker()
        self.phase = 0

    def configure(self, config: dict):
        self.config = config or {}

    def reset(self):
        self.targets.clear()
        self.tracker.clear()
        self.phase = 0

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

        target = self.allocator.select(
            self.targets.all(),
            getattr(me, "lat", 0.0),
            getattr(me, "lon", 0.0),
        )

        if target is not None:
            point = self.tracker.command_point(target, self.uid)
            if point:
                cmds.append(self.fly_to(point[0], point[1], getattr(me, "alt", 120.0)))
        else:
            cmds.append(self._search(me))

        return cmds

    def _search(self, me):
        self.phase += 1
        offset = (int(str(self.uid)[-1]) + self.phase % 20) * 0.0001
        return self.fly_to(
            getattr(me, "lat", 0.0) + offset,
            getattr(me, "lon", 0.0) + offset,
            getattr(me, "alt", 120.0),
        )
