"""Search-track scenario Agent base class."""
from __future__ import annotations

from typing import List

from ...core.agent import Agent
from ...core.commands import Command
from .observation import SearchTrackObs


class SearchTrackAgent(Agent):
    """Player base class for the search-track scenario.

    Implement ``decide(obs, dt)`` to return commands for THIS UAV
    (``self.my_uid``). Use ``obs.self.detection`` to switch between
    SEARCH (sweep the gimbal, fly a search pattern) and TRACK (aim the
    gimbal, follow the detected target).
    """

    def decide(self, obs: SearchTrackObs, dt: float) -> List[Command]:  # type: ignore[override]
        raise NotImplementedError
