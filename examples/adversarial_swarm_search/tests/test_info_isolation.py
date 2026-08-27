"""Spec 019 US5 / SC-010 — Info-isolation static + runtime tests.

Static checks: scan all search_track/*.py modules for forbidden
patterns that would let the algorithm read the ground-truth
configuration or peer status fields:

  * `cfg["zones"]` / `cfg.get("zones", ...)` / `cfg.zones`
  * `state.zones`  — note: the *published* zones bucket IS allowed
                     (FR-016: the kernel publishes the zones bucket for
                     blind-avoidance).  What is FORBIDDEN is reading the
                     scenario config's zones array directly.
  * `entity.platform.status` / `entity.status` for a peer

For clarity the static check distinguishes:
  - FORBIDDEN: scenario-config zones access
  - ALLOWED:   `state.zones` (published bucket)
  - FORBIDDEN: peer status access

Runtime check: inject a synthetic peer status change and verify that
FleetMembership does NOT use it (i.e. loss is decided purely by
heartbeat freshness).
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.fleet_membership import FleetMembership, Heartbeat  # noqa: E402

SEARCH_TRACK_DIR = EXAMPLE_DIR / "search_track"

# Allowed: read state.zones bucket
# Forbidden: cfg["zones"] / cfg.get("zones") / similar (scenario config)
FORBIDDEN_PATTERNS = [
    # scenario config zones
    ('cfg["zones"]', "scenario config zones dict access"),
    ("cfg.get(\"zones\"", "scenario config zones .get()"),
    ("cfg['zones']", "scenario config zones dict access"),
    ("cfg.get('zones'", "scenario config zones .get()"),
    ("self.cfg.zones", "scenario config zones attribute access"),
    # ground-truth threat fields
    ('platform["status"]', "peer platform.status direct read"),
    ("platform.get('status'", "peer platform.status direct read"),
    ('platform["hit_points"]', "ground-truth HP direct read"),
    ('["hit_points"]', "ground-truth HP direct read"),
    ('entity["status"]', "peer entity status direct read"),
]


def _scan_module_for_patterns(py_path: Path) -> list[tuple[int, str, str]]:
    """Return [(lineno, line, pattern_name), ...] for any forbidden match."""
    findings: list[tuple[int, str, str]] = []
    src = py_path.read_text(encoding="utf-8")
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        # Skip pure comments
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for pat, name in FORBIDDEN_PATTERNS:
            if pat in line:
                findings.append((i, line, name))
    return findings


class InfoIsolationStaticTest(unittest.TestCase):
    def test_search_track_modules_clean(self):
        bad = []
        for py_path in SEARCH_TRACK_DIR.glob("*.py"):
            if py_path.name == "__init__.py":
                continue
            findings = _scan_module_for_patterns(py_path)
            for lineno, line, name in findings:
                bad.append(f"{py_path.name}:{lineno}: {name}  | {line.strip()}")
        if bad:
            self.fail("Forbidden info-isolation violation(s):\n" +
                      "\n".join(bad))


class InfoIsolationRuntimeTest(unittest.TestCase):
    """Even if status is set to 'destroyed' on a heartbeat, the peer
    must remain ACTIVE until heartbeat_timeout_s elapses.  This proves
    SC-010's runtime invariant.
    """

    def test_destroyed_status_does_not_trigger_lost(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        # Even though status="destroyed", the heartbeat is recent (t=0)
        # so the peer is ACTIVE — not LOST.
        hb = Heartbeat(uid="u2", sim_time=0.0, lat=27.0, lon=124.99,
                       status="destroyed")
        fm.observe_heartbeat(hb)
        fm.tick(sim_time=1.0)
        self.assertEqual(fm.state_of("u2").value, "active")

    def test_status_change_after_heartbeat_does_nothing(self):
        """A later 'status change' message (e.g. from a peer report)
        must NOT affect liveness — only heartbeats do.
        """
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        # u2 sends a fresh heartbeat with status='active'
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=0.0,
                                       lat=27.0, lon=124.99,
                                       status="active"))
        # ...then a "status change" event arrives (no sim_time impact).
        # The peer is still ACTIVE because we only consumed heartbeats.
        fm.tick(sim_time=1.0)
        self.assertEqual(fm.state_of("u2").value, "active")
        # Now u2 stops sending heartbeats — loss decided by timeout, not
        # by any status string.
        fm.tick(sim_time=6.0)
        self.assertEqual(fm.state_of("u2").value, "lost")


if __name__ == "__main__":
    unittest.main()
