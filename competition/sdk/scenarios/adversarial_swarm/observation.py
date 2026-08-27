"""Adversarial-swarm scenario Observation.

Fixed top-level (self/comm_inbox/briefing). ``obs.self.jammed`` is the
self-perception channel for dynamic comm-jam regions; pre-match-known
static threats live in ``obs.briefing.known_threats``.
"""
from __future__ import annotations

from ...core.observation import Observation


class SwarmObs(Observation):
    """Observation for the adversarial-swarm scenario.

    No extra own-state fields beyond the base. Sense the dynamic threat
    environment via ``obs.self.jammed``/``obs.self.comm_stats`` and
    coordinate via ``obs.comm_inbox``.
    """
    pass
