"""Tests for batch aggregation and target randomization (T043, T044)."""
import json
import random
from pathlib import Path

import pytest

from search_track.batch import BatchRunner, randomize_target, summarize
from search_track.metrics import RunMetrics


def test_randomize_target_is_deterministic():
    r1 = randomize_target(seed=42)
    r2 = randomize_target(seed=42)
    assert r1 == r2
    r3 = randomize_target(seed=43)
    assert r1 != r3


def test_randomize_target_returns_lat_lon_alt():
    r = randomize_target(seed=0)
    assert "lat" in r and "lon" in r and "alt" in r
    assert -90.0 <= r["lat"] <= 90.0
    assert -180.0 <= r["lon"] <= 180.0


def test_summarize_computes_stats():
    results = [
        RunMetrics(run_id="r1", search_time=10.0, total_track_time=20.0,
                   track_in_view_time=18.0, track_in_view_fraction=0.9,
                   mode_switches=1, searched_successfully=True),
        RunMetrics(run_id="r2", search_time=20.0, total_track_time=15.0,
                   track_in_view_time=12.0, track_in_view_fraction=0.8,
                   mode_switches=1, searched_successfully=True),
        RunMetrics(run_id="r3", search_time=30.0, total_track_time=0.0,
                   track_in_view_time=0.0, track_in_view_fraction=0.0,
                   mode_switches=0, searched_successfully=False),
    ]
    s = summarize(results)
    assert s["runs"] == 3
    assert s["success_rate"] == pytest.approx(2 / 3, rel=1e-3)
    # mean computed only over successful runs (2 of 3)
    assert s["search_time_mean"] == pytest.approx(15.0)
    assert s["track_in_view_fraction_mean"] == pytest.approx(0.5666, rel=1e-3)


def test_batch_runner_dry_run(monkeypatch, tmp_path):
    """Test that BatchRunner orchestrates runs without actually spawning sims.

    We monkey-patch _run_one to return synthetic metrics."""
    from search_track import batch as batch_mod

    runs: list[RunMetrics] = []
    def fake_run(*, run_idx: int, seed: int, output_dir, sim_binary, scenario_path,
                 duration, controller_spec) -> RunMetrics:
        m = RunMetrics(run_id=f"r{run_idx}", search_time=10.0 + run_idx,
                       total_track_time=20.0, track_in_view_time=18.0,
                       track_in_view_fraction=0.9, mode_switches=1,
                       searched_successfully=True)
        runs.append(m)
        return m
    monkeypatch.setattr(batch_mod, "_run_one", fake_run)

    results = BatchRunner(output_dir=tmp_path).run(n=3, seed_base=0,
                                                    controller_name="X",
                                                    config_snapshot={})
    assert len(results) == 3
    assert runs == results
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["runs"] == 3
    assert summary["success_rate"] == 1.0
