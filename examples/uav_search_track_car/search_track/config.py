"""Algorithm config loader (YAML)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Range bounds — kept in one place to mirror data-model.md §6
RANGES: dict[str, tuple[float, float]] = {
    "search_radius": (50.0, 5000.0),
    "search_altitude_agl": (50.0, 1500.0),
    "sweep_period": (0.5, 30.0),
    "loiter_radius": (50.0, 2000.0),
    "k_acquire": (1, 600),
    "k_lost": (1, 600),
    "spiral_growth_rate": (0.0, 500.0),
    "sweep_pitch_min": (-90.0, 0.0),
    "sweep_pitch_max": (-90.0, 0.0),
    "loiter_refresh_period": (0.1, 30.0),
    "control_rate_hz": (1, 120),
}

DEFAULTS: dict[str, Any] = {
    "controller": "search_track.fsm_controller:FsmSearchTrackController",
    "seed": None,
    "mode": "spiral",
    "search_radius": 500.0,
    "search_altitude_agl": 300.0,
    "sweep_period": 4.0,
    "loiter_radius": 200.0,
    "advanced": {
        "k_acquire": 5,
        "k_lost": 60,
        "spiral_growth_rate": 30.0,
        "sweep_pitch_min": -60.0,
        "sweep_pitch_max": -30.0,
        "loiter_refresh_period": 3.0,
        "control_rate_hz": 10,
    },
}


@dataclass
class AlgorithmConfig:
    controller: str
    seed: int | None
    mode: str
    search_radius: float
    search_altitude_agl: float
    sweep_period: float
    loiter_radius: float
    advanced: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.advanced:
            return self.advanced[key]
        return getattr(self, key, default)


def _coerce_and_check(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, (lo, hi) in RANGES.items():
        if key in d:
            v = d[key]
            try:
                v = float(v) if isinstance(lo, float) else int(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"config field {key!r} not numeric: {v!r}") from exc
            if v < lo or v > hi:
                raise ValueError(
                    f"config field {key!r}={v} out of range [{lo}, {hi}]"
                )
            out[key] = v
    return out


def from_yaml(path: str | Path) -> AlgorithmConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open("r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}
    merged = {**DEFAULTS, **raw}
    advanced_raw = merged.pop("advanced", {}) or {}
    advanced_checked = _coerce_and_check(advanced_raw)
    top_checked = _coerce_and_check(merged)
    controller = str(merged.get("controller", DEFAULTS["controller"]))
    if ":" not in controller:
        # accept dotted form too
        controller = controller.replace(".", ":", controller.count(".") - 1)
    return AlgorithmConfig(
        controller=controller,
        seed=merged.get("seed"),
        mode=str(merged.get("mode", "spiral")),
        search_radius=float(top_checked.get("search_radius", DEFAULTS["search_radius"])),
        search_altitude_agl=float(
            top_checked.get("search_altitude_agl", DEFAULTS["search_altitude_agl"])
        ),
        sweep_period=float(top_checked.get("sweep_period", DEFAULTS["sweep_period"])),
        loiter_radius=float(top_checked.get("loiter_radius", DEFAULTS["loiter_radius"])),
        advanced={**DEFAULTS["advanced"], **advanced_checked},
    )
