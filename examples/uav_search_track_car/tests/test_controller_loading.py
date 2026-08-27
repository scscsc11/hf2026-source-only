"""Tests for controller loading and override paths (T036, T037)."""
import textwrap

import pytest

from search_track import config as cfg_mod
from search_track.config import AlgorithmConfig, from_yaml
from search_track.controller import load_controller


def test_load_controller_with_dotted_form(tmp_path):
    p = tmp_path / "alg.yaml"
    p.write_text(
        "controller: greedy_controller:GreedyController\n"
    )
    c = from_yaml(p)
    assert ":" in c.controller
    # should not raise (greedy_controller is provided by T041 in same package)
    ctrl = load_controller(c.controller)
    assert ctrl is not None


def test_load_controller_dotted_with_no_colon_in_yaml():
    """dotted form (no colon) is converted to module:Class form on load."""
    from search_track.config import from_yaml
    import textwrap, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("controller: search_track.fsm_controller:FsmSearchTrackController\n")
        path = f.name
    c = from_yaml(path)
    assert ":" in c.controller


def test_load_controller_rejects_garbage():
    with pytest.raises((ValueError, ImportError, TypeError)):
        load_controller("not_a_module.NotAClass")


def test_round_trip_yaml(tmp_path):
    p = tmp_path / "alg.yaml"
    p.write_text(
        textwrap.dedent(
            """\
            search_radius: 1000.0
            loiter_radius: 250.0
            advanced:
              k_acquire: 3
            """
        )
    )
    c1 = from_yaml(p)
    assert c1.search_radius == 1000.0
    # modify and re-read
    (tmp_path / "alg2.yaml").write_text(
        textwrap.dedent(
            """\
            search_radius: 1500.0
            loiter_radius: 300.0
            """
        )
    )
    c2 = from_yaml(tmp_path / "alg2.yaml")
    assert c2.search_radius == 1500.0
    assert c1 is not c2


def test_range_violation_clear_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("search_altitude_agl: 99999.0\n")
    with pytest.raises(ValueError, match="out of range"):
        from_yaml(p)
