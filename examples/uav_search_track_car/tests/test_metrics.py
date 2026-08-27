"""Tests for MetricsRecorder (T025)."""
import json
from pathlib import Path

import pytest

from search_track.metrics import MetricsRecorder, RunMetrics


def test_metrics_basic_accounting():
    m = MetricsRecorder(run_id="test_run")
    m.record_tick(sim_time=0.0, mode="SEARCH", detected=False, uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    m.record_tick(sim_time=0.1, mode="SEARCH", detected=False, uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    # Found at t=0.2
    m.record_tick(sim_time=0.2, mode="TRACK", detected=True, uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    m.record_tick(sim_time=0.3, mode="TRACK", detected=True, uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    m.record_tick(sim_time=0.4, mode="TRACK", detected=False, uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    final = m.finalize(controller_name="X", seed=0, config_snapshot={"k": 1})
    assert final.search_time == pytest.approx(0.2)
    assert final.searched_successfully is True
    # total_track_time accumulates dt for every TRACK tick:
    #   tick 0.2: dt=0.1 (from 0.1), tick 0.3: dt=0.1, tick 0.4: dt=0.1 → 0.3
    assert final.total_track_time == pytest.approx(0.3)
    # track_in_view_time: every TRACK tick where detected=True
    #   tick 0.2 (det=True): dt=0.1, tick 0.3 (det=True): dt=0.1, tick 0.4 (det=False): skip
    #   → 0.2
    assert final.track_in_view_time == pytest.approx(0.2)
    assert final.track_in_view_fraction == pytest.approx(2 / 3, rel=1e-3)
    assert final.mode_switches == 1


def test_metrics_saves_json_and_csv(tmp_path: Path):
    m = MetricsRecorder(run_id="save_test")
    for i in range(5):
        m.record_tick(sim_time=float(i), mode="SEARCH", detected=False,
                      uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    m.finalize(controller_name="X", seed=None, config_snapshot={})
    j, c = m.save(tmp_path)
    assert j.exists() and c.exists()
    data = json.loads(j.read_text())
    assert data["run_id"] == "save_test"
    assert c.read_text().splitlines()[0].startswith("sim_time,mode,detected")


def test_metrics_track_in_view_fraction_zero_when_no_track():
    m = MetricsRecorder(run_id="no_track")
    for i in range(5):
        m.record_tick(sim_time=float(i), mode="SEARCH", detected=False,
                      uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    final = m.finalize(controller_name="X", seed=None, config_snapshot={})
    assert final.total_track_time == 0.0
    assert final.track_in_view_fraction == 0.0
    assert final.searched_successfully is False


def test_metrics_relative_to_sim_t0_avoids_baseline_bug():
    """Regression: the engine's sim_time may start at a large value.
    search_time / sim_duration must be relative to the first tick's
    sim_time, not the raw absolute value (otherwise they come out as the
    huge baseline, or negative once a sim_t0 offset is mixed in)."""
    m = MetricsRecorder(run_id="baseline")
    base = 1_700_000_000.0  # large sim_time baseline as emitted by the engine
    m.record_tick(sim_time=base + 0.0, mode="SEARCH", detected=False,
                  uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    m.record_tick(sim_time=base + 5.0, mode="TRACK", detected=True,
                  uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    m.record_tick(sim_time=base + 6.0, mode="TRACK", detected=True,
                  uav_lat=27.0, uav_lon=125.0, uav_alt=300.0)
    final = m.finalize(controller_name="X", seed=0, config_snapshot={})
    assert final.search_time == pytest.approx(5.0)    # not base + 5
    assert final.sim_duration == pytest.approx(6.0)   # not base + 6
    assert final.search_time > 0
    assert final.sim_duration > 0
