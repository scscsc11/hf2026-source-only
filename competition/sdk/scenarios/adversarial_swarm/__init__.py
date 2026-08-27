"""Scenario 3: adversarial swarm search.

10 UAVs search for 10 real targets among 20 decoys while evading a SAM
air-defense zone and comm jamming. Pre-match-known static threats (SAM,
static jam) appear in ``briefing.known_threats``; dynamic jam regions are
sensed via ``obs.self.jammed`` and shared via comms.
"""
from __future__ import annotations

from pathlib import Path

from .agent import SwarmAgent
from .observation import SwarmObs
from .runner import AdversarialSwarmRunner, run

SCENARIO_DIR = Path(__file__).resolve().parents[3] / "scenarios" / "adversarial_swarm"
DEFAULT_SCENARIO_JSON = str(SCENARIO_DIR / "scenario.json")

__all__ = [
    "SwarmAgent", "SwarmObs", "AdversarialSwarmRunner",
    "run", "SCENARIO_DIR", "DEFAULT_SCENARIO_JSON",
]
