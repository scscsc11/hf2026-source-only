"""Adversarial-swarm scenario Agent base class."""
from __future__ import annotations

from typing import List

from ...core.agent import Agent
from ...core.commands import Command
from .observation import SwarmObs


class SwarmAgent(Agent):
    """Player base class for the adversarial-swarm scenario.

    Implement ``decide(obs, dt)`` for THIS UAV (``self.my_uid``). Each UAV
    gets its own instance. Key awareness channels:

      * ``obs.briefing.known_threats`` — pre-match-known STATIC threats
        (SAM site polygon, static jam region). Plan routes to avoid these.
      * ``obs.self.jammed`` — TRUE when this UAV is currently inside a
        (possibly dynamic) jam region. Use it to estimate and broadcast
        dynamic-jam awareness to teammates.
      * ``obs.comm_inbox`` — teammate messages (e.g. confirmed target
        positions, jam warnings).

    If your UAV is destroyed (``obs.self.status == "destroyed"``) the
    runner stops calling decide() for it (self-termination).
    """

    def decide(self, obs: SwarmObs, dt: float) -> List[Command]:  # type: ignore[override]
        raise NotImplementedError
