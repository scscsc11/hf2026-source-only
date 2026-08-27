"""Coop-decoy scenario Agent base class."""
from __future__ import annotations

from typing import List

from ...core.agent import Agent
from ...core.commands import Command
from .observation import CoopObs


class CoopAgent(Agent):
    """Player base class for the cooperative-decoy scenario.

    Implement ``decide(obs, dt)`` for THIS UAV (``self.my_uid``). Each UAV
    gets its own instance. Coordinate with teammates by emitting
    ``broadcast()``/``send_to()`` commands and reading ``obs.comm_inbox``.
    You CANNOT see teammate poses — agree on a payload format (e.g.
    ``"R:lat,lon"`` for a rendezvous, ``"T:lat,lon"`` for a confirmed
    target) and parse incoming messages yourself.
    """

    def decide(self, obs: CoopObs, dt: float) -> List[Command]:  # type: ignore[override]
        raise NotImplementedError
