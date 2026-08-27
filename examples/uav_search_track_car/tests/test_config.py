"""Tests for AlgorithmConfig YAML loader (T013)."""
import os
import textwrap
import pytest

from search_track import config as cfg_mod
from search_track.config import AlgorithmConfig, from_yaml


def test_from_yaml_defaults(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    c = from_yaml(p)
    assert isinstance(c, AlgorithmConfig)
    assert c.controller.endswith(":FsmSearchTrackController")
    assert c.search_radius == 500.0
    assert c.advanced["k_acquire"] == 5


def test_from_yaml_overrides(tmp_path):
    p = tmp_path / "alg.yaml"
    p.write_text(
        textwrap.dedent(
            """\
            search_radius: 1200.0
            loiter_radius: 350.0
            advanced:
              k_acquire: 3
            """
        )
    )
    c = from_yaml(p)
    assert c.search_radius == 1200.0
    assert c.loiter_radius == 350.0
    assert c.advanced["k_acquire"] == 3
    # untouched defaults remain
    assert c.advanced["k_lost"] == 60


def test_from_yaml_rejects_out_of_range(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("search_radius: 99999.0\n")
    with pytest.raises(ValueError, match="out of range"):
        from_yaml(p)


def test_from_yaml_missing_file():
    with pytest.raises(FileNotFoundError):
        from_yaml("/tmp/does_not_exist_xyz.yaml")


def test_from_yaml_accepts_dotted_controller(tmp_path):
    p = tmp_path / "alg.yaml"
    p.write_text(
        "controller: search_track.fsm_controller.FsmSearchTrackController\n"
    )
    c = from_yaml(p)
    assert ":" in c.controller
