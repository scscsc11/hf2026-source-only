"""Spec 019 — algorithm config loader.

Reuses the same YAML loader shape as 016/017. We re-import the helper so
operators don't have to maintain two loader implementations.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Reuse the loader from the 017 example — it does the right thing.
import sys
_EXAMPLE_017 = Path(__file__).resolve().parents[2] / "multi_uav_coop_decoy"
if str(_EXAMPLE_017) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_017))

try:
    from search_track.config_reuse import load_algorithm_config  # type: ignore
except Exception:
    # If 017 isn't on the path, fall back to a tiny stub loader.
    def load_algorithm_config(path: str) -> dict[str, Any]:  # type: ignore
        import yaml
        with open(path, "r", encoding="utf-8-sig") as f:
            return yaml.safe_load(f) or {}
