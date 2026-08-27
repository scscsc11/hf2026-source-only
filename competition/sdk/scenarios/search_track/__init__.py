"""Scenario 1: UAV search-and-track a single car.

The simplest scenario (1 UAV / 1 target, no decoys). Validates the core
SDK end-to-end. Player implements ``SearchTrackAgent.decide()``.
"""
from __future__ import annotations

from pathlib import Path

from .agent import SearchTrackAgent
from .observation import SearchTrackObs
from .runner import SearchTrackRunner, run

SCENARIO_DIR = Path(__file__).resolve().parents[3] / "scenarios" / "search_track"
DEFAULT_SCENARIO_JSON = str(SCENARIO_DIR / "scenario.json")

__all__ = [
    "SearchTrackAgent", "SearchTrackObs", "SearchTrackRunner",
    "run", "SCENARIO_DIR", "DEFAULT_SCENARIO_JSON",
]
