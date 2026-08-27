"""Metrics recorder — search time, track time, track-in-view fraction."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunMetrics:
    run_id: str
    sim_duration: float = 0.0
    wall_duration: float = 0.0
    search_time: float = 0.0
    searched_successfully: bool = False
    total_track_time: float = 0.0
    track_in_view_time: float = 0.0
    track_in_view_fraction: float = 0.0
    mode_switches: int = 0
    controller_name: str = ""
    seed: int | None = None
    config_snapshot: dict[str, Any] = field(default_factory=dict)


class MetricsRecorder:
    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or f"run_{time.strftime('%Y%m%d_%H%M%S')}"
        self._metrics = RunMetrics(run_id=self.run_id)
        self._tick_log: list[dict[str, Any]] = []
        self._last_mode: str = "SEARCH"
        self._last_sim_time: float | None = None
        self._wall_start: float = time.time()
        self._found_sim_time: float | None = None
        self._sim_t0: float | None = None

    def record_tick(
        self,
        *,
        sim_time: float,
        mode: str,
        detected: bool,
        uav_lat: float,
        uav_lon: float,
        uav_alt: float,
        target_lat: float | None = None,
        target_lon: float | None = None,
        distance_m: float | None = None,
    ) -> None:
        if self._sim_t0 is None:
            self._sim_t0 = sim_time
        if mode != self._last_mode:
            self._metrics.mode_switches += 1
            self._last_mode = mode
        # Time accounting
        if self._last_sim_time is not None:
            dt = sim_time - self._last_sim_time
            if dt < 0:
                dt = 0
            if mode == "TRACK":
                self._metrics.total_track_time += dt
                if detected:
                    self._metrics.track_in_view_time += dt
        if mode == "TRACK" and self._found_sim_time is None and detected:
            self._found_sim_time = sim_time
            self._metrics.search_time = sim_time - (self._sim_t0 or 0.0)
            self._metrics.searched_successfully = True
        self._last_sim_time = sim_time
        self._tick_log.append(
            {
                "sim_time": sim_time,
                "mode": mode,
                "detected": detected,
                "uav_lat": uav_lat,
                "uav_lon": uav_lon,
                "uav_alt": uav_alt,
                "target_lat": target_lat,
                "target_lon": target_lon,
                "distance_m": distance_m,
            }
        )

    def finalize(self, *, controller_name: str, seed: int | None, config_snapshot: dict[str, Any]) -> RunMetrics:
        self._metrics.sim_duration = (
            (self._last_sim_time - self._sim_t0)
            if self._last_sim_time is not None and self._sim_t0 is not None
            else 0.0
        )
        self._metrics.wall_duration = time.time() - self._wall_start
        if self._metrics.total_track_time > 0:
            self._metrics.track_in_view_fraction = (
                self._metrics.track_in_view_time / self._metrics.total_track_time
            )
        self._metrics.controller_name = controller_name
        self._metrics.seed = seed
        self._metrics.config_snapshot = config_snapshot
        return self._metrics

    def save(self, output_dir: str | Path) -> tuple[Path, Path]:
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        j = d / f"{self.run_id}.json"
        c = d / f"{self.run_id}.csv"
        with j.open("w", encoding="utf-8") as f:
            json.dump(asdict(self._metrics), f, indent=2, ensure_ascii=False)
        if self._tick_log:
            keys = list(self._tick_log[0].keys())
            with c.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows(self._tick_log)
        return j, c
