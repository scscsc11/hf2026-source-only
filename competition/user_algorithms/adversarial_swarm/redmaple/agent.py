"""
RedMaple V2 main agent entry.

Integrates:
- target_manager: local target belief
- communication: distributed messages
- allocator: target assignment
- tracker: tracking behavior
"""

from typing import List

from competition.sdk.scenarios.adversarial_swarm.agent import SwarmAgent
from competition.sdk.core.commands import Command

from .target_manager import TargetManager
from .communication import encode_target, decode_message
from .allocator import TargetAllocator
from .tracker import CooperativeTracker


class RedMapleAgent(SwarmAgent):
    """Distributed cooperative swarm agent."""

    def __init__(self, my_uid: str):
        super().__init__(my_uid)
        self.uid = my_uid
        self.targets = TargetManager()
        self.allocator = TargetAllocator()
        self.tracker = CooperativeTracker()
        self.home = None
        self.phase = 0

    def configure(self, config: dict):
        self.config = config or {}

    def reset(self):
        self.targets.clear()
        self.home = None
        self.phase = 0
        self.tracker.reset()

    def decide(self, obs, dt: float) -> List[Command]:
        cmds = []

        me = obs.self
        if self.home is None:
            self.home = (me.lat, me.lon)

        # receive cooperative information
        for msg in getattr(obs, "comm_inbox", []):
            payload = getattr(msg, "payload", msg)
            data = decode_message(payload)
            if data:
                self.targets.fuse_remote(data)

        # update local detections
        detections = getattr(me, "detections", None)
        if detections is None:
            detections = []
        self.targets.update_local(detections, getattr(obs, "time", 0.0))

        # broadcast strongest local belief
        best = self.targets.best_target()
        if best is not None:
            cmds.append(self.broadcast(encode_target(best)))

        # choose mission
        target = self.allocator.select(self.targets.all(), me.lat, me.lon)

        if target is not None:
            cmds.extend(self.tracker.track(target, me))
        else:
            # deterministic search pattern
            cmds.extend(self._search(me))

        return cmds

    def _search(self, me):
        # simple spiral-like deterministic exploration fallback
        step = (hash(self.uid) % 360) * 0.01 + self.phase
        self.phase += 0.02
        lat = me.lat + 0.001 * step
        lon = me.lon + 0.001 * step
        return [self.fly_to(lat, lon, getattr(me, "alt", 120.0))]
