"""Coop-decoy scenario Observation.

Same fixed top-level (self/comm_inbox/briefing). ``obs.self`` carries the
comm_stats self-perception signal (legitimate — it's this agent's own
comm statistics). Teammate info arrives only via ``comm_inbox`` payloads.
"""
from __future__ import annotations

from ...core.observation import Observation


class CoopObs(Observation):
    """Observation for the cooperative-decoy scenario.

    No extra own-state fields beyond the base. The player coordinates via
    ``obs.comm_inbox`` (teammate messages) and senses the radio environment
    via ``obs.self.comm_stats`` / ``obs.self.jammed``.
    """
    pass
