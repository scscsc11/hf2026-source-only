"""RedMaple V2 RC2 main agent entry.

Decision pipeline:
communication -> perception -> belief -> allocation -> role -> action.
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
        self.debug_state = {}
        self.claimed_target = None

    def configure(self, config: dict):
        self.config = config or {}

    def reset(self):
        self.targets.clear()
        self.tracker.clear()
        self.search.reset()
        self.role.assign(None)
        self.claimed_target = None
        self.debug_state = {}

    def _safe_position(self, me):
        return getattr(me, "lat", 0.0), getattr(me, "lon", 0.0)

    def _update_perception(self, obs):
        me = obs.self
        now = getattr(obs, "time", 0.0)
        detections = getattr(me, "detections", None)
        if detections is None:
            single = getattr(me, "detection", None)
            detections = [single] if single else []

        for det in detections or []:
            if not getattr(det, "detected", True):
                continue
            self.targets.update_detection(
                getattr(det, "target_lat", getattr(me, "lat", 0.0)),
                getattr(det, "target_lon", getattr(me, "lon", 0.0)),
                getattr(det, "confidence", 0.5),
                now,
                self.uid,
            )

    def _update_communication(self, obs):
        now = getattr(obs, "time", 0.0)
        for msg in getattr(obs, "comm_inbox", []):
            payload = getattr(msg, "payload", msg)
            data = self.comm.decode(payload)
            if not data:
                continue
            if data.get("type") == "target":
                self.targets.fuse_remote(
                    data["id"],
                    data["lat"],
                    data["lon"],
                    data["confidence"],
                    now,
                )

    def _update_debug(self, role, target, action):
        self.debug_state = {
            "uid": self.uid,
            "role": role,
            "target": getattr(target, "target_id", None),
            "action": action,
        }

    def decide(self, obs, dt: float) -> List[Command]:
        cmds = []
        me = obs.self
        now = getattr(obs, "time", 0.0)

        self._update_communication(obs)
        self._update_perception(obs)
        self.targets.decay(now)

        target = self.allocator.choose_target(
            list(self.targets.targets.values()),
            self._safe_position(me),
        )

        self.role.assign(target)

        if target is not None:
            self.tracker.assign(target.target_id, self.role.role)

            if self.claimed_target != target.target_id:
                self.claimed_target = target.target_id
                cmds.append(self.broadcast(self.comm.encode_claim(target.target_id, self.uid)))

            cmds.append(self.broadcast(self.comm.encode_target(target)))

            point = self.tracker.command_point(target, self.uid)
            if point:
                self._update_debug(self.role.role, target, "TRACK")
                cmds.append(self.fly_to(point[0], point[1], getattr(me, "alt", 120.0)))
        else:
            if self.claimed_target is not None:
                cmds.append(self.broadcast(self.comm.encode_release(self.claimed_target, self.uid)))
                self.claimed_target = None

            lat, lon = self.search.next_point(me, getattr(obs, "briefing", None))
            self._update_debug("SEARCHER", None, "SEARCH")
            cmds.append(self.fly_to(lat, lon, getattr(me, "alt", 120.0)))

        return cmds
