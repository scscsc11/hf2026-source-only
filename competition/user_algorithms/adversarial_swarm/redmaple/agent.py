"""RedMaple RC1 runnable agent.

Simple competition loop:
perception -> belief -> allocation -> role -> action.
"""

from typing import List

from competition.sdk.scenarios.adversarial_swarm.agent import SwarmAgent
from competition.sdk.core.commands import Command

from .target_manager import TargetManager
from .communication import CommunicationManager
from .allocator import TargetAllocator
from .tracker import CooperativeTracker
from .search_planner import SearchPlanner
from .role_manager import RoleManager


class RedMapleAgent(SwarmAgent):
    def __init__(self, my_uid: str):
        super().__init__(my_uid)
        self.uid = str(my_uid)
        self.targets = TargetManager()
        self.allocator = TargetAllocator(self.uid)
        self.tracker = CooperativeTracker()
        self.search = SearchPlanner(self.uid)
        self.comm = CommunicationManager()
        self.role = RoleManager(self.uid)
        self.claimed_target = None

    def configure(self, config):
        self.config = config

    def reset(self):
        self.targets.clear()
        self.tracker.clear()
        self.search.reset()
        self.claimed_target = None

    def update_targets(self, obs):
        now = getattr(obs, "time", 0.0)

        for msg in getattr(obs, "comm_inbox", []):
            data = self.comm.decode(getattr(msg, "payload", msg))
            if data and data.get("type") == "target":
                self.targets.fuse_remote(
                    data["id"],
                    data["lat"],
                    data["lon"],
                    data["confidence"],
                    now,
                )

        me = obs.self
        detections = getattr(me, "detections", [])
        if not detections:
            detection = getattr(me, "detection", None)
            detections = [detection] if detection else []

        for det in detections:
            self.targets.update_detection(
                getattr(det, "target_lat", me.lat),
                getattr(det, "target_lon", me.lon),
                getattr(det, "confidence", 0.5),
                now,
                self.uid,
            )

        self.targets.decay(now)

    def decide(self, obs, dt: float) -> List[Command]:
        commands = []
        me = obs.self

        self.update_targets(obs)

        target = self.allocator.choose_target(
            list(self.targets.targets.values()),
            (me.lat, me.lon),
        )

        if target:
            self.role.assign(target)
            self.tracker.assign(target.target_id, self.role.role)

            if self.claimed_target != target.target_id:
                self.claimed_target = target.target_id
                commands.append(
                    self.broadcast(self.comm.encode_claim(target.target_id, self.uid))
                )

            commands.append(self.broadcast(self.comm.encode_target(target)))

            point = self.tracker.command_point(target, self.uid)
            commands.append(self.fly_to(point[0], point[1], getattr(me, "alt", 120.0)))

        else:
            if self.claimed_target:
                commands.append(
                    self.broadcast(self.comm.encode_release(self.claimed_target, self.uid))
                )
                self.claimed_target = None

            lat, lon = self.search.next_point(me, getattr(obs, "briefing", None))
            commands.append(self.fly_to(lat, lon, getattr(me, "alt", 120.0)))

        return commands
