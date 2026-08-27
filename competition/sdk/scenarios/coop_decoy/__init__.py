"""Scenario 2: multi-UAV cooperative decoy.

3 UAVs cooperate to track 3 real targets among 15 stationary decoys.
Coordination happens ONLY via the constrained comm channel (≤50B, rate/
range/jam limited) — strict isolation means no agent sees teammate poses.
"""
from __future__ import annotations

from pathlib import Path

from .agent import CoopAgent
from .observation import CoopObs
from .runner import CoopDecoyRunner, run

SCENARIO_DIR = Path(__file__).resolve().parents[3] / "scenarios" / "coop_decoy"
DEFAULT_SCENARIO_JSON = str(SCENARIO_DIR / "scenario.json")

__all__ = [
    "CoopAgent", "CoopObs", "CoopDecoyRunner",
    "run", "SCENARIO_DIR", "DEFAULT_SCENARIO_JSON",
]
