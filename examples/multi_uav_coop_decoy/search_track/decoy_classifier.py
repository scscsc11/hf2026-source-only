"""Decoy classifier — distinguish a real (moving) target from a static decoy.

Design (Task 2): the engine reports ``detection.target_position`` = the
locked target's true position every tick it is detected. Real TargetVehicles
move (scenario speed > 0); DecoyVehicles are static (speed 0). So over a
short observation window the reported position of a real target *displaces*,
while a decoy's stays (near-)fixed. This motion signal is a robust, honest
classifier: it uses only what the camera reports, never the ground-truth
``target_type``/``misid_flag`` the engine also publishes.

Decision rule: collect target_position samples while detected; once we have
>= ``min_window_s`` of samples (and >= ``min_samples``), compute the
bounding-box span of the samples. If the span >= ``move_threshold_m`` the
target is REAL, else DECOY. A real target at 5 m/s covers 10 m in 2 s; a
static decoy covers ~0 m (only per-tick GPS jitter). A 5 m threshold
separates them with wide margin.

The classifier can decide early (before the full window) once the running
span already exceeds the threshold, so fast targets are classified in well
under 2 s.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from examples.uav_search_track_car.search_track.geometry import haversine_m


@dataclass
class DecoyClassifier:
    """Motion-based real-vs-decoy classifier.

    Feed it ``observe(sim_time, lat, lon)`` each tick the target is detected.
    Read ``decision`` (None until decided) and ``label``.
    """
    move_threshold_m: float = 5.0
    min_window_s: float = 1.5
    min_samples: int = 10
    # Smoothness gate (Task 3): a REAL target moves smoothly (per-tick
    # displacement <= a few m even at 12 m/s). But when the engine's
    # nearest-target detection jumps between a real target and nearby decoys,
    # the reported position also jumps (tens-hundreds of m per tick) — which
    # inflates the bounding-box span and would falsely read as REAL. So a
    # REAL decision additionally requires the max per-tick displacement to
    # stay under this threshold (smooth motion). A jumping/mixed stream is
    # classified DECOY (released), not REAL.
    max_jump_m: float = 15.0
    # Samples: list of (sim_time, lat, lon).
    samples: list[tuple[float, float, float]] = field(default_factory=list)
    decision: str | None = None        # "real" | "decoy" | None
    decided_at: float | None = None    # sim_time when decided
    started_at: float | None = None    # sim_time of first sample
    _max_jump: float = 0.0             # max per-tick displacement seen

    def reset(self) -> None:
        self.samples.clear()
        self.decision = None
        self.decided_at = None
        self.started_at = None
        self._max_jump = 0.0

    def observe(self, sim_time: float, lat: float | None,
                lon: float | None) -> str | None:
        """Record one detected-frame sample and maybe decide.

        Returns the decision ("real"/"decoy") once made, else None. After a
        decision is made, subsequent observes are no-ops (decision is final).
        """
        if self.decision is not None:
            return self.decision
        if lat is None or lon is None:
            return None  # not detected this tick — skip (gap-tolerant)
        if self.started_at is None:
            self.started_at = sim_time
        # Track max per-tick displacement (smoothness signal).
        if self.samples:
            d = haversine_m(self.samples[-1][1], self.samples[-1][2], lat, lon)
            if d > self._max_jump:
                self._max_jump = d
        self.samples.append((sim_time, lat, lon))

        span = self._span_m()
        window = sim_time - self.started_at
        smooth = self._max_jump < self.max_jump_m

        # Early decision: a SMOOTH, moving target is REAL before the full
        # window elapses. (Jumping/mixed streams do NOT qualify — they are
        # the nearest-target switching between target and decoys.)
        if (span >= self.move_threshold_m and len(self.samples) >= self.min_samples
                and smooth):
            self.decision = "real"
            self.decided_at = sim_time
            return self.decision
        # Full window elapsed: classify. Smooth + moving -> REAL; otherwise
        # (static OR jumping/mixed) -> DECOY (release).
        if window >= self.min_window_s and len(self.samples) >= self.min_samples:
            if span >= self.move_threshold_m and smooth:
                self.decision = "real"
            else:
                self.decision = "decoy"
            self.decided_at = sim_time
            return self.decision
        return None

    def _span_m(self) -> float:
        """Bounding-box diagonal (m) of the collected samples — the max
        pairwise distance, approximated by the lat/lon extents. Robust to
        a couple of jitter outliers because it only grows with extremes."""
        if len(self.samples) < 2:
            return 0.0
        lats = [s[1] for s in self.samples]
        lons = [s[2] for s in self.samples]
        lat_span_m = (max(lats) - min(lats)) * 111320.0
        mid_lat = sum(lats) / len(lats)
        lon_span_m = (max(lons) - min(lons)) * (111320.0 * _cos_safe(mid_lat))
        # Diagonal of the bounding box = the worst-case pairwise distance.
        return (lat_span_m ** 2 + lon_span_m ** 2) ** 0.5

    @property
    def time_to_decide_s(self) -> float | None:
        if self.decided_at is None or self.started_at is None:
            return None
        return self.decided_at - self.started_at


def _cos_safe(lat_deg: float) -> float:
    import math
    c = math.cos(math.radians(lat_deg))
    return c if c > 0.01 else 0.01
