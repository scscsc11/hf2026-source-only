"""
RedMaple V2 runtime agent.

Runtime pipeline:
Observation
 -> local belief update
 -> communication fusion
 -> target allocation
 -> cooperative tracking/search

This file is the actual SwarmAgent entry point.
"""

from typing import List

from competition.sdk.scenarios.adversarial_swarm.agent import SwarmAgent
from competition.sdk.core.commands import Command

from .target_manager import TargetManager
from .communication import encode_target, decode_message
from .allocator import TargetAllocator
from .tracker import CooperativeTracker


class RedMapleAgent(SwarmAgent):
    """RedMaple distributed cooperative swarm controller."""

    def __init__(self, my_uid: str):
        super().__init__(my_uid)
        self.uid = my_uid
        self.targets = TargetManager()
        self.allocator = TargetAllocator()
        self.tracker = CooperativeTracker()

        self.config = {}
        self.home = None
        self.search_index = 0
        self.last_broadcast = -999
        self.current_target = None

    def configure(self, config: dict):
        self.config = config or {}

    def reset(self):
        self.targets.clear()
        self.tracker.reset()
        self.home = None
        self.search_index = 0
        self.last_broadcast = -999
        self.current_target = None

    def decide(self, obs, dt: float) -> List[Command]:
        commands = []

        me = obs.self

        if self.home is None:
            self.home = (me.lat, me.lon)

        sim_time = getattr(obs, "time", 0.0)

        # 1. Fuse teammate information
        for msg in getattr(obs, "comm_inbox", []):
            payload = getattr(msg, "payload", msg)
            decoded = decode_message(payload)
            if decoded:
                self.targets.fuse_remote(decoded, sim_time)

        # 2. Update local perception
        detections = getattr(me, "detections", None)
        if detections is None:
            single = getattr(me, "detection", None)
            detections = [single] if single else []

        self.targets.update_local(detections, sim_time)
        self.targets.decay(sim_time)

        # 3. Periodically share strongest belief
        if sim_time - self.last_broadcast > 2.0:
            best = self.targets.best_target()
            if best:
                commands.append(self.broadcast(encode_target(best)))
                self.last_broadcast = sim_time

        # 4. Distributed allocation
        self.current_target = self.allocator.select(
            self.targets.all(),
            me.lat,
            me.lon,
        )

        # 5. Execute mission
        if self.current_target is not None:
            commands.extend(
                self.tracker.track(
                    self.current_target,
                    me,
                    dt,
                )
            )
        else:
            commands.extend(self._search(me))

        return commands

    def _search(self, me):
        """Deterministic coverage fallback."""
        uid_seed = abs(hash(self.uid)) % 1000
        angle = (uid_seed + self.search_index * 17) % 360
        self.search_index += 1

        offset = 0.002
        lat = me.lat + offset
        lon = me.lon + offset

        if angle % 4 == 0:
            lon -= offset * 2
        elif angle % 4 == 1:
            lat -= offset * 2

        return [
            self.fly_to(
                lat,
                lon,
                getattr(me, "alt", 120.0),
            )
        ]
