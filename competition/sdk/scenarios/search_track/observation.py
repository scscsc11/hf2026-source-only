"""Search-track scenario Observation.

Top-level shape is fixed (self/comm_inbox/briefing). This scenario adds
no extra own-state fields, so ``SearchTrackObs`` is a thin alias of the
base ``Observation`` — kept as a distinct type so player code and the IDE
know which scenario they're in.
"""
from __future__ import annotations

from ...core.observation import Observation


class SearchTrackObs(Observation):
    """Observation for the search-track scenario.

    No scenario-specific fields beyond the fixed top-level three. The
    player reads ``obs.self`` (pose/gimbal/detection) and decides search
    vs. track behavior. There are no teammates (1 UAV), so
    ``obs.comm_inbox`` is empty and ``briefing.fleet_size == 1``.
    """
    pass
