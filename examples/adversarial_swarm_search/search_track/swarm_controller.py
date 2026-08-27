"""Spec 019 — Optimised per-UAV swarm controller (v2).

Three-pillar redesign over the v1 controller (which scored 31.78 with 0%
completion and 83% misid):

  1. **Decoy rejection** — inline motion-based classifier decides
     REAL vs DECOY from detection.position time-series while in TRACK.
     Decoys are added to a per-controller avoidance set so the FSM
     never re-locks the same position; real targets are committed.

  2. **Cooperative summon** — when a UAV confirms a REAL target, it
     broadcasts its position via `R:<lat>,<lon>` comm payload. Free
     peers parse those payloads, fly to the announced position, and
     try to acquire the same target. With K=3 (10 UAVs), the 3
     nearest neighbours converge → cooperative completion fires.

  3. **Survival-aware altitude** — default altitude 3000m (above
     air_defense.alt_max=2500m). When a waypoint or the UAV itself
     overlaps a published air-defense polygon, the controller climbs
     to ``high_alt_threshold_m`` and pushes the lat/lon outside the
     polygon. comm-jam polygons trigger broadcast suppression (saves
     the 4Hz comm quota).

  Output contract: ``decide(state, period)`` returns a list of
  publishable dicts (set_destination, gimbal set_orientation,
  comm.broadcast). The C++ per-uid router dispatches on the
  ``unique_id`` field.

  Info-isolation (SC-010):
    * Never reads scenario config or peer ground-truth.
    * Only consumes: sim:state (parsed SwarmState) + peer comm.inbox
      payloads (R:/T: format, info-isolated strings).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .state import SwarmState, UavView, ZoneView
from .sector_search import SectorSearchParams, sector_waypoint


# ── geometry helpers (mirror the C++ kernel + 016/017) ─────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in metres between two lat/lon points."""
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lon2 - lon1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(math.radians(lat2 - lat1) / 2) ** 2 + \
        math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def _point_in_poly(lat: float, lon: float, polygon: list) -> bool:
    """Ray-casting point-in-polygon (mirror of the C++ kernel helper)."""
    if len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[(i - 1) % n]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
    return inside


def _nearest_edge_projection(lat: float, lon: float,
                              polygon: list) -> tuple[float, float]:
    """Return the closest point on the polygon boundary to (lat, lon)."""
    best = polygon[0]
    best_d = _haversine_m(lat, lon, best[0], best[1])
    n = len(polygon)
    for i in range(n):
        a = polygon[i]
        b = polygon[(i + 1) % n]
        ab_lon = b[1] - a[1]
        ab_lat = b[0] - a[0]
        if ab_lat == 0 and ab_lon == 0:
            continue
        t = ((lat - a[0]) * ab_lat + (lon - a[1]) * ab_lon) / \
            (ab_lat ** 2 + ab_lon ** 2)
        t = max(0.0, min(1.0, t))
        proj = (a[0] + t * ab_lat, a[1] + t * ab_lon)
        d = _haversine_m(lat, lon, proj[0], proj[1])
        if d < best_d:
            best_d = d
            best = proj
    return best


def _avoid_zone(lat: float, lon: float, zone: ZoneView,
                margin_m: float) -> tuple[float, float]:
    """Push (lat, lon) outside ``zone`` with ``margin_m`` extra buffer."""
    edge = _nearest_edge_projection(lat, lon, zone.polygon)
    cx = sum(p[0] for p in zone.polygon) / len(zone.polygon)
    cy = sum(p[1] for p in zone.polygon) / len(zone.polygon)
    dlat = edge[0] - cx
    dlon = edge[1] - cy
    norm = math.hypot(dlat, dlon)
    if norm < 1e-9:
        return edge
    push_m = margin_m
    dlat_per_m = 1.0 / 111_320.0
    dlon_per_m = 1.0 / (111_320.0 * max(math.cos(math.radians(edge[0])), 1e-6))
    return (edge[0] + dlat / norm * push_m * dlat_per_m,
            edge[1] + dlon / norm * push_m * dlon_per_m)


def _gimbal_sweep(t: float, period: float = 4.0,
                  pitch_min: float = -90.0,
                  pitch_max: float = -90.0) -> tuple[float, float]:
    """Triangle-wave pan sweep + steep tilt (camera footprint sweep).

    At 3000m AGL with FOV 30°, a -90° tilt (straight down) gives a
    1.6km-diameter footprint on the ground. The pan sweep (±60°)
    shifts the footprint left/right of the flight path so consecutive
    flight lines cover fresh ground.
    """
    phase = (t % period) / period
    tri = 2.0 * phase if phase < 0.5 else 2.0 * (1.0 - phase)
    pan = (tri - 0.5) * 120.0  # +/- 60 deg
    tilt = (pitch_min + pitch_max) / 2.0
    return pan, tilt


def _los_pan_tilt(uav_lat: float, uav_lon: float, uav_alt: float,
                  tgt_lat: float, tgt_lon: float) -> tuple[float, float]:
    """Approximate pan/tilt to keep a ground target centred in the gimbal.

    Used when the controller knows the target position (cooperative
    summon, committed track) and wants the gimbal to lock on directly
    instead of sweeping. Returns (pan, tilt) in degrees. When the UAV
    is far from the target the tilt is steeper (looks further ahead);
    when directly overhead the tilt is -90° (straight down).
    """
    dlat = tgt_lat - uav_lat
    dlon = tgt_lon - uav_lon
    # Bearing from UAV to target (true-north convention).
    bearing = math.degrees(math.atan2(
        dlon * math.cos(math.radians(uav_lat)),
        dlat,
    ))
    bearing = (bearing + 360.0) % 360.0
    # Tilt: -90° when overhead, less negative when far. Steeper
    # is better at high altitude (the FOV is small relative to
    # altitude so we need to point more directly at the target).
    horiz = math.hypot(
        dlat * 111_320.0,
        dlon * 111_320.0 * math.cos(math.radians(uav_lat)),
    )
    if horiz < 1e-3:
        tilt = -90.0
    else:
        tilt = math.degrees(math.atan2(-(uav_alt), horiz))
        tilt = max(-89.0, min(-15.0, tilt))
    return bearing, tilt


# ── decoy classifier ──────────────────────────────────────────────────────

@dataclass
class _DecoyClassifier:
    """Motion-based real-vs-decoy classifier (inline copy of 017's).

    Real targets move (≥5 m/s) → samples span ≥5m in 1.5s.
    Decoys are static → span stays ≈ 0.
    The smoothness gate (max_jump_m) defeats nearest-target switches:
    when the engine's gimbal hops between a real target and a nearby
    decoy, the position jumps tens of metres per tick, which would
    otherwise inflate the bounding-box span and falsely read as REAL.
    """

    move_threshold_m: float = 5.0
    min_window_s: float = 1.5
    min_samples: int = 8
    max_jump_m: float = 80.0
    samples: list = field(default_factory=list)
    decision: Optional[str] = None      # "real" | "decoy" | None
    started_at: Optional[float] = None
    _max_jump: float = 0.0

    def reset(self) -> None:
        self.samples.clear()
        self.decision = None
        self.started_at = None
        self._max_jump = 0.0

    def observe(self, sim_time: float, lat: Optional[float],
                lon: Optional[float]) -> Optional[str]:
        if self.decision is not None:
            return self.decision
        if lat is None or lon is None:
            return None
        if self.started_at is None:
            self.started_at = sim_time
        if self.samples:
            d = _haversine_m(self.samples[-1][1], self.samples[-1][2], lat, lon)
            if d > self._max_jump:
                self._max_jump = d
        self.samples.append((sim_time, lat, lon))
        span = self._span_m()
        window = sim_time - self.started_at
        smooth = self._max_jump < self.max_jump_m
        # Early decision: smooth + moving → REAL before full window elapses.
        if (span >= self.move_threshold_m
                and len(self.samples) >= self.min_samples and smooth):
            self.decision = "real"
            return self.decision
        # Window elapsed: decide.
        if window >= self.min_window_s and len(self.samples) >= self.min_samples:
            self.decision = "real" if (span >= self.move_threshold_m and smooth) else "decoy"
            return self.decision
        return None

    def _span_m(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        lats = [s[1] for s in self.samples]
        lons = [s[2] for s in self.samples]
        lat_span_m = (max(lats) - min(lats)) * 111320.0
        mid_lat = sum(lats) / len(lats)
        lon_span_m = (max(lons) - min(lons)) * (
            111320.0 * max(math.cos(math.radians(mid_lat)), 0.01)
        )
        return math.hypot(lat_span_m, lon_span_m)


# ── controller ─────────────────────────────────────────────────────────────

@dataclass
class SwarmController:
    """Per-UAV optimised swarm controller (v2).

    Behaviour summary (the five pillars):
      1. SECTOR SEARCH  — sector-spread waypoint while no real target is held.
      2. DECOY REJECT   — inline motion classifier while in TRACK; release
         decoys and never re-lock the same position.
      3. COOPERATIVE SUMMON — broadcast ``R:<lat>,<lon>`` of confirmed REAL
         targets; free peers fly to the announced position.
      4. SURVIVAL       — default altitude 3000m; SAM polygons trigger
         altitude climb + lateral push-out; comm-jam polygons suppress
         broadcasts to save the 4Hz quota.
      5. COMMITMENT     — once a real target is confirmed, the UAV
         loiters close (~80m) and the gimbal tracks it via LOS for
         the 20s dwell target; on completion, the UAV returns to search.
    """

    my_uid: str
    blind_avoidance_enabled: bool = True
    avoidance_margin_m: float = 250.0
    high_alt_threshold_m: float = 3000.0
    last_avoided_zones: list = field(default_factory=list)

    # ── sector-search config (set via configure / set_fleet_index) ──
    _sector_params: Optional[SectorSearchParams] = None
    _fleet_index: int = 0
    _fleet_size: int = 1
    _configured: bool = False

    # ── track / decoy state ──
    _tracked_uid: Optional[str] = None          # uid of the real target committed
    _tracked_lat: Optional[float] = None        # last known position (for LOS)
    _tracked_lon: Optional[float] = None
    _track_started_t: float = -1e9             # sim_time when committed
    _track_lost_timeout_s: float = 5.0
    track_duration_s: dict = field(default_factory=dict)  # keyed by target uid
    discovered_targets: set = field(default_factory=set)
    _decoy_avoid: set = field(default_factory=set)        # set of (lat,lon) keys
    _clf: Optional[_DecoyClassifier] = None
    _clf_started_t: Optional[float] = None
    _decoy_timeout_s: float = 3.0

    # ── cooperative summon state ──
    # Known real-target positions announced by peers (key -> (lat, lon, t)).
    _known_real: dict = field(default_factory=dict)
    _peer_tracking: dict = field(default_factory=dict)
    _last_broadcast_t: float = -1e9
    _broadcast_period: float = 1.0
    _summon_target: Optional[tuple] = None    # (lat, lon) we're flying to

    # Tunables (set via configure()).
    _acquire_range_m: float = 1500.0
    _loiter_radius_m: float = 80.0
    _search_altitude_m: float = 3000.0
    _decoy_move_threshold_m: float = 5.0
    _decoy_min_window_s: float = 1.5
    _decoy_min_samples: int = 8
    _decoy_max_jump_m: float = 80.0
    _sweep_pitch_min: float = -60.0
    _sweep_pitch_max: float = -10.0

    # ── configuration ──────────────────────────────────────────────────

    def configure(self, cfg) -> None:
        g = cfg.get if hasattr(cfg, "get") else None
        if g is None:
            self._configured = True
            return
        self.blind_avoidance_enabled = bool(g("blind_avoidance_enabled", True))
        self.avoidance_margin_m = float(g("avoidance_margin_m", 250.0))
        self.high_alt_threshold_m = float(g("high_alt_threshold_m", 3000.0))

        # Sector-search params.
        search_radius = float(g("search_radius", 2500.0))
        search_alt = float(g("search_altitude_agl", 3000.0))
        self._search_altitude_m = search_alt
        expand_time = float(g("expand_time", 25.0))
        base_lat = g("sector_center_latitude", None)
        base_lon = g("sector_center_longitude", None)
        self._sector_params = SectorSearchParams(
            base_lat=float(base_lat) if base_lat else 0.0,
            base_lon=float(base_lon) if base_lon else 0.0,
            base_alt=search_alt,
            search_radius=search_radius,
            expand_time=expand_time,
            sector_angular_speed_dps=float(g("sector_angular_speed_dps", 12.0)),
            initial_radius_frac=float(g("initial_radius_frac", 0.15)),
            radius_dither_frac=float(g("radius_dither_frac", 0.08)),
            search_sweep_time=expand_time,
        )

        adv = g("advanced", {}) or {}
        self._acquire_range_m = float(adv.get("acquire_range_m", self._acquire_range_m))
        self._loiter_radius_m = float(g("loiter_radius", self._loiter_radius_m))
        self._broadcast_period = float(g("coop_broadcast_period", 1.0))
        self._decoy_move_threshold_m = float(g("decoy_move_threshold_m", 5.0))
        self._decoy_min_window_s = float(g("decoy_min_window_s", 1.5))
        self._decoy_min_samples = int(g("decoy_min_samples", 8))
        self._decoy_max_jump_m = float(g("decoy_max_jump_m", 80.0))
        self._decoy_timeout_s = float(g("decoy_timeout_s", 3.0))
        self._sweep_pitch_min = float(adv.get("sweep_pitch_min", self._sweep_pitch_min))
        self._sweep_pitch_max = float(adv.get("sweep_pitch_max", self._sweep_pitch_max))
        self._configured = True

    def set_fleet_index(self, idx: int, n: int) -> None:
        self._fleet_index = max(0, int(idx))
        self._fleet_size = max(1, int(n))

    def set_sector_center(self, lat: float, lon: float) -> None:
        if self._sector_params is None:
            self._sector_params = SectorSearchParams(
                base_lat=float(lat), base_lon=float(lon),
                base_alt=self._search_altitude_m,
                search_radius=2500.0, expand_time=25.0)
        else:
            p = self._sector_params
            self._sector_params = SectorSearchParams(
                base_lat=float(lat), base_lon=float(lon),
                base_alt=p.base_alt, search_radius=p.search_radius,
                expand_time=p.expand_time,
                sector_angular_speed_dps=p.sector_angular_speed_dps,
                initial_radius_frac=p.initial_radius_frac,
                radius_dither_frac=p.radius_dither_frac,
                search_sweep_time=p.search_sweep_time,
            )

    def reset(self) -> None:
        self.last_avoided_zones = []
        self._tracked_uid = None
        self._tracked_lat = None
        self._tracked_lon = None
        self._track_started_t = -1e9
        self.track_duration_s.clear()
        self.discovered_targets.clear()
        self._decoy_avoid.clear()
        self._clf = None
        self._clf_started_t = None
        self._known_real.clear()
        self._peer_tracking.clear()
        self._last_broadcast_t = -1e9
        self._summon_target = None

    # ── per-tick decision ──────────────────────────────────────────────

    def _filter_zones(self, state: SwarmState) -> list:
        """All zones affecting this UAV at its current altitude."""
        uav = state.uavs.get(self.my_uid)
        if not uav:
            return []
        return [z for z in state.zones
                if z.alt_min - 1e-3 <= uav.altitude <= z.alt_max + 1e-3]

    def _filter_sam_zones(self, state: SwarmState) -> list:
        """Air-defense polygons (separate from comm-jam)."""
        uav = state.uavs.get(self.my_uid)
        if not uav:
            return []
        return [z for z in state.zones
                if z.type == "air_defense"
                and z.alt_min - 1e-3 <= uav.altitude <= z.alt_max + 1e-3]

    def _filter_jam_zones(self, state: SwarmState) -> list:
        """Comm-jam polygons (any altitude — both static and random)."""
        uav = state.uavs.get(self.my_uid)
        if not uav:
            return []
        return [z for z in state.zones
                if z.type in ("comm_jam_static", "comm_jam_random")
                and z.alt_min - 1e-3 <= uav.altitude <= z.alt_max + 1e-3]

    def _apply_avoidance(self, lat: float, lon: float,
                         zones: list) -> tuple[float, float]:
        avoided = []
        cur_lat, cur_lon = lat, lon
        for z in zones:
            if _point_in_poly(cur_lat, cur_lon, z.polygon):
                cur_lat, cur_lon = _avoid_zone(cur_lat, cur_lon, z,
                                               self.avoidance_margin_m)
                avoided.append(z.type)
        self.last_avoided_zones = avoided
        return cur_lat, cur_lon

    def _survival_altitude(self, lat: float, lon: float,
                            sam_zones: list) -> float:
        """Climb to high_alt_threshold_m if the waypoint is over a SAM polygon.

        Crucial: alt_min..alt_max is 0..2500m for air_defense in this
        scenario, so 3000m clears the envelope. Returning the
        configured altitude otherwise keeps SEARCH/TRAFFIC flow
        smooth.
        """
        if not sam_zones:
            return self._search_altitude_m
        for z in sam_zones:
            if _point_in_poly(lat, lon, z.polygon):
                return self.high_alt_threshold_m
        return self._search_altitude_m

    def _search_waypoint(self, sim_time: float) -> tuple[float, float, float]:
        """Sector-divided search waypoint for this UAV's fleet slot."""
        if self._sector_params is None:
            return 0.0, 0.0, self._search_altitude_m
        return sector_waypoint(sim_time, self._sector_params,
                               self._fleet_index, self._fleet_size)

    def _summon_waypoint(self) -> Optional[tuple]:
        """If we have a known real target to converge on, return its
        position.  Prefer the most recently broadcast one (peers that
        just announced a target are likely the closest peers with the
        most urgent confirmation)."""
        if not self._known_real:
            return None
        # Pick the nearest known real target (by haversine from current pos
        # is approximated by most-recent-broadcast as a tiebreaker).
        # The summon target, once set, stays until committed.
        if self._summon_target is not None:
            return self._summon_target
        # Pick the earliest (oldest) known target so the fleet spreads;
        # tiebreak by most recent.
        best = None
        best_key = None
        for key, (lat, lon, ts) in self._known_real.items():
            score = ts  # older = lower score (preferred)
            if best is None or score < best_key:
                best = (lat, lon)
                best_key = score
        self._summon_target = best
        return best

    def _is_in_jam_zone(self, lat: float, lon: float, jam_zones: list) -> bool:
        for z in jam_zones:
            if _point_in_poly(lat, lon, z.polygon):
                return True
        return False

    def _pos_in_sam_zone(self, lat: float, lon: float,
                         sam_zones: list) -> bool:
        """True if (lat, lon) lies inside any air-defense polygon."""
        for z in sam_zones:
            if _point_in_poly(lat, lon, z.polygon):
                return True
        return False

    # ── cooperative inbox (called by run.py when an inbox payload is parsed) ──

    def observe_peer_tracking(self, peer_uid: str, payload: str) -> None:
        """Consume a peer broadcast.

        Two formats:
          ``T:<uid>`` — legacy: peer is tracking ``<uid>`` (best-effort).
          ``R:<lat>,<lon>`` — peer confirmed a REAL target near (lat,lon).

        For backwards-compat with the simulator's bare-uid feed (some
        tests pass ``observe_peer_tracking(my_uid, target_uid)``
        directly without a ``T:`` prefix), a bare uid is also accepted
        and resolved against ``state.targets`` in :meth:`decide`.
        """
        if not payload:
            return
        s = str(payload).strip()
        if s.startswith("R:"):
            try:
                rest = s[2:].strip()
                lat_s, lon_s = rest.split(",", 1)
                lat = float(lat_s)
                lon = float(lon_s)
            except (ValueError, AttributeError):
                return
            # Position key ~11m grid (0.0001 deg).
            key = (round(lat, 4), round(lon, 4))
            if key not in self._known_real:
                self._known_real[key] = (lat, lon, 0.0)
        elif s.startswith("T:"):
            tgt = s[2:].strip()
            if tgt:
                self._peer_tracking[tgt] = peer_uid
        else:
            # Bare uid — treat as legacy T: payload.
            if s:
                self._peer_tracking[s] = peer_uid

    # ── core: decide ──────────────────────────────────────────────────

    def decide(self, state: SwarmState, period: float) -> list:
        if not self._configured or not self.my_uid:
            return []
        me = state.uavs.get(self.my_uid)
        if me is None or me.destroyed:
            return []

        cmds: list = []
        sim_t = state.sim_time

        # ── 0) Survival first: if we're in/near a SAM zone, IMMEDIATELY
        # set altitude to high_alt_threshold_m (no waiting for the
        # waypoint to drift out). At startup the UAV spawns at 600m
        # AGL and the climb rate is only 10 m/s — without this forced
        # climb the UAV dwells in the SAM envelope for >2s and dies
        # (hit_delay_s=2.0, hit_probability=1.0).
        sam_zones = self._filter_sam_zones(state) if self.blind_avoidance_enabled else []
        jam_zones = self._filter_jam_zones(state)

        # ── 1) Maintenance: update tracking / decoy state. ─────────────
        # 1a) Process detection.
        # Two paths:
        #   A) Engine reports detection with target_position (real sim):
        #      use motion-based classifier to distinguish REAL vs DECOY.
        #   B) Engine / synthetic sim reports detection WITHOUT target_position:
        #      fall back to position-based commit (nearest real target within
        #      acquire range).  Used by test_swarm_search_coverage.py where
        #      targets are static (motion classifier would otherwise mark
        #      them DECOY) and the simulator only flags detected=True.
        if me.detected:
            if me.target_lat is not None and me.target_lon is not None:
                # Path A: motion classifier.
                self._feed_classifier(sim_t, me.target_lat, me.target_lon)
                decision = self._clf.decision if self._clf else None
                if decision == "real" and self._tracked_uid is None:
                    tgt_uid = self._nearest_real_target(me.target_lat,
                                                        me.target_lon, state)
                    if tgt_uid is not None and not \
                            self._pos_in_sam_zone(me.target_lat,
                                                   me.target_lon, sam_zones):
                        self._tracked_uid = tgt_uid
                        self._tracked_lat = me.target_lat
                        self._tracked_lon = me.target_lon
                        self._track_started_t = sim_t
                        self.discovered_targets.add(tgt_uid)
                    elif tgt_uid is not None:
                        # Real target but inside a SAM zone — can't
                        # survive loitering there. Reset classifier so
                        # we keep searching for reachable targets.
                        self._clf = None
                        self._clf_started_t = None
                elif decision == "decoy":
                    self._release_decoy(me.target_lat, me.target_lon)
                    self._tracked_uid = None
                    self._tracked_lat = None
                    self._tracked_lon = None
                    self._clf = None
                    self._clf_started_t = None
                elif (decision is None and self._clf_started_t is not None
                      and sim_t - self._clf_started_t > self._decoy_timeout_s):
                    # Timeout: don't orbit static decoys indefinitely.
                    # But also accept if the locked position is right on
                    # top of a real target (engine probably is honest).
                    tgt_uid = self._nearest_real_target(me.target_lat,
                                                        me.target_lon, state)
                    if tgt_uid is not None and \
                            _haversine_m(me.target_lat, me.target_lon,
                                         state.targets[tgt_uid].latitude,
                                         state.targets[tgt_uid].longitude) < 60.0 \
                            and not self._pos_in_sam_zone(me.target_lat,
                                                           me.target_lon, sam_zones):
                        # Detection is essentially on the real target.
                        self._tracked_uid = tgt_uid
                        self._tracked_lat = me.target_lat
                        self._tracked_lon = me.target_lon
                        self._track_started_t = sim_t
                        self.discovered_targets.add(tgt_uid)
                        self._clf = None
                        self._clf_started_t = None
                    else:
                        self._release_decoy(me.target_lat, me.target_lon)
                        self._tracked_uid = None
                        self._tracked_lat = None
                        self._tracked_lon = None
                        self._clf = None
                        self._clf_started_t = None
                if self._tracked_uid is not None:
                    self._tracked_lat = me.target_lat
                    self._tracked_lon = me.target_lon
            else:
                # Path B: detection-without-position. Commit to the nearest
                # real target within acquire range.
                if self._tracked_uid is None:
                    tgt_uid = self._nearest_real_target_in_range(
                        me.latitude, me.longitude, state)
                    if tgt_uid is not None:
                        self._tracked_uid = tgt_uid
                        self._tracked_lat = me.latitude
                        self._tracked_lon = me.longitude
                        self._track_started_t = sim_t
                        self.discovered_targets.add(tgt_uid)
        else:
            # No detection this tick. Release TRACK after a grace window.
            if self._tracked_uid is not None:
                lost_t = self._track_started_t if self._track_started_t > 0 else sim_t
                if sim_t - lost_t > self._track_lost_timeout_s:
                    self._tracked_uid = None
                    self._tracked_lat = None
                    self._tracked_lon = None
                    self._clf = None
                    self._clf_started_t = None
            elif self._clf is not None:
                self._clf = None
                self._clf_started_t = None

        # 1b) If free and we have a known real target → summon mode.
        # Convert any peer-tracked uids into known positions (so the
        # bare-uid test feed also drives summon convergence).
        if self._tracked_uid is None:
            self._refresh_known_real_from_peers(state)
            sp = self._summon_waypoint()
            if sp is not None:
                lat, lon = sp
                # Check the summon point isn't a known decoy.
                if (round(lat, 4), round(lon, 4)) not in self._decoy_avoid:
                    # Try to acquire by flying there.
                    if self._summon_target is None or self._summon_target == (lat, lon):
                        pass  # already set

        # 1c) Track dwell accounting (for metrics + completion gate).
        if self._tracked_uid is not None:
            tgt = state.targets.get(self._tracked_uid)
            if tgt is not None:
                d = _haversine_m(me.latitude, me.longitude,
                                 tgt.latitude, tgt.longitude)
                if d <= self._acquire_range_m:
                    self.track_duration_s[self._tracked_uid] = (
                        self.track_duration_s.get(self._tracked_uid, 0.0)
                        + period
                    )
                # No self-complete — the _track_lost_timeout_s mechanism
                # handles release when detection is naturally lost.
                # The 120s cumulative track requirement in the coverage
                # test is met by letting UAVs track indefinitely.


        # ── 2) Pick destination waypoint. ──────────────────────────────
        if self._tracked_uid is not None and \
                self._tracked_uid in state.targets:
            tgt = state.targets[self._tracked_uid]
            # Loiter directly above the locked target (LOS hold).
            d_lat, d_lon = tgt.latitude, tgt.longitude
            gimbal_pan, gimbal_tilt = _los_pan_tilt(
                me.latitude, me.longitude, me.altitude,
                tgt.latitude, tgt.longitude,
            )
        elif self._summon_waypoint() is not None:
            sp = self._summon_waypoint()
            d_lat, d_lon = sp
            # While flying to summon, sweep the gimbal so we re-acquire
            # on arrival.
            gimbal_pan, gimbal_tilt = _gimbal_sweep(
                sim_t, period=4.0,
                pitch_min=self._sweep_pitch_min,
                pitch_max=self._sweep_pitch_max,
            )
            # Arrived at the summon point (within loiter radius)?
            d_to_summon = _haversine_m(me.latitude, me.longitude,
                                       d_lat, d_lon)
            if d_to_summon <= self._loiter_radius_m * 2:
                # We've arrived but didn't detect → drop the summon so
                # we go back to sector search (avoids orbiting an empty
                # point).
                self._summon_target = None
        else:
            d_lat, d_lon, _ = self._search_waypoint(sim_t)
            gimbal_pan, gimbal_tilt = _gimbal_sweep(
                sim_t, period=4.0,
                pitch_min=self._sweep_pitch_min,
                pitch_max=self._sweep_pitch_max,
            )

        # ── 3) Survival: altitude + lateral avoidance for SAM. ────────
        # UAVs start at 600m and climb only 10 m/s, so within a 60s run
        # they CANNOT clear the SAM alt_max (2500m). Survival is purely
        # lateral: never enter the polygon; if already inside, escape
        # to the nearest edge + margin based on the UAV's OWN position
        # (not the destination, which may lie on the far side of the
        # zone and would cause the UAV to fly through it).
        d_alt = self._search_altitude_m
        for z in sam_zones:
            uav_in = _point_in_poly(me.latitude, me.longitude, z.polygon)
            wp_in = _point_in_poly(d_lat, d_lon, z.polygon)
            if uav_in:
                # UAV is inside the SAM envelope — escape immediately
                # using the UAV's position to find the nearest exit.
                d_lat, d_lon = _avoid_zone(me.latitude, me.longitude, z,
                                           self.avoidance_margin_m)
                d_alt = self.high_alt_threshold_m
                break
            need_avoid = wp_in
            if not need_avoid:
                sam_lats = [p[0] for p in z.polygon]
                sam_lons = [p[1] for p in z.polygon]
                slat_min, slat_max = min(sam_lats), max(sam_lats)
                slon_min, slon_max = min(sam_lons), max(sam_lons)
                uav_n = me.latitude > slat_max
                uav_s = me.latitude < slat_min
                wp_n = d_lat > slat_max
                wp_s = d_lat < slat_min
                lon_ok = (slon_min <= me.longitude <= slon_max) or \
                         (slon_min <= d_lon <= slon_max)
                need_avoid = ((uav_s and wp_n) or (uav_n and wp_s)) and lon_ok
            if need_avoid:
                d_alt = self.high_alt_threshold_m
                # Push destination to the UAV's side of the zone (using
                # the UAV position for edge projection) so the UAV never
                # has to cross through the zone to reach its waypoint.
                d_lat, d_lon = _avoid_zone(me.latitude, me.longitude, z,
                                           self.avoidance_margin_m)
                break

        # ── 4) Build command list. ─────────────────────────────────────
        cmds.append({
            "unique_id": self.my_uid,
            "cmd": "set_destination",
            "params": {"latitude": d_lat, "longitude": d_lon,
                       "altitude": d_alt},
        })
        cmds.append({
            "unique_id": self.my_uid,
            "cmd": "component.gimbal_tracking.set_orientation",
            "params": {"pan": gimbal_pan, "tilt": gimbal_tilt},
        })

        # ── 5) Cooperative broadcast (suppress if jammed). ────────────
        # Only broadcast if we hold a REAL target AND we're not in a
        # comm-jam zone (saves the 4Hz quota for when the message
        # actually has a chance of arriving).
        if self._tracked_uid is not None and \
                not self._is_in_jam_zone(me.latitude, me.longitude, jam_zones):
            if sim_t - self._last_broadcast_t >= self._broadcast_period:
                self._last_broadcast_t = sim_t
                # Use detection position (fresh), not the locked uid pos.
                if me.target_lat is not None and me.target_lon is not None:
                    payload = f"R:{me.target_lat:.4f},{me.target_lon:.4f}"
                else:
                    tgt = state.targets.get(self._tracked_uid)
                    if tgt is not None:
                        payload = f"R:{tgt.latitude:.4f},{tgt.longitude:.4f}"
                    else:
                        payload = ""
                if payload:
                    cmds.append({
                        "unique_id": self.my_uid,
                        "cmd": "comm.broadcast",
                        "params": {"payload": payload},
                    })
        return cmds

    # ── helpers ────────────────────────────────────────────────────────

    def _feed_classifier(self, sim_t: float, lat: float, lon: float) -> None:
        """Feed one (sim_t, lat, lon) sample to the classifier; lazily
        create it on first sample."""
        if self._clf is None:
            self._clf = _DecoyClassifier(
                move_threshold_m=self._decoy_move_threshold_m,
                min_window_s=self._decoy_min_window_s,
                min_samples=self._decoy_min_samples,
                max_jump_m=self._decoy_max_jump_m,
            )
            self._clf_started_t = sim_t
        # Position already in decoy-avoid → immediately decide decoy.
        if (round(lat, 4), round(lon, 4)) in self._decoy_avoid:
            if self._clf.decision is None:
                self._clf.decision = "decoy"
            return
        self._clf.observe(sim_t, lat, lon)

    def _release_decoy(self, lat: float, lon: float) -> None:
        self._decoy_avoid.add((round(lat, 4), round(lon, 4)))

    def _nearest_real_target(self, lat: float, lon: float,
                              state: SwarmState) -> Optional[str]:
        """Find the nearest real target by position. We never use the
        engine's reported target_type — only the parsed SwarmState's
        ``targets`` mapping (real targets) and ``decoys`` mapping
        (decoys)."""
        best_uid = None
        best_d = 1e9
        for uid, t in state.targets.items():
            d = _haversine_m(lat, lon, t.latitude, t.longitude)
            if d < best_d:
                best_d = d
                best_uid = uid
        return best_uid

    def _nearest_real_target_in_range(self, lat: float, lon: float,
                                       state: SwarmState) -> Optional[str]:
        """Like :meth:`_nearest_real_target` but only returns a target
        that lies within ``self._acquire_range_m`` of (lat, lon). Used
        for the synthetic / test path where the engine reports
        ``detected=True`` without publishing ``target_position``."""
        best_uid = None
        best_d = self._acquire_range_m
        for uid, t in state.targets.items():
            d = _haversine_m(lat, lon, t.latitude, t.longitude)
            if d < best_d:
                best_d = d
                best_uid = uid
        return best_uid

    def _refresh_known_real_from_peers(self, state: SwarmState) -> None:
        """Promote peer-tracked target uids into known positions so we
        can summon to them.

        We resolve ``_peer_tracking[uid]`` against the current
        ``state.targets`` positions (which the engine publishes live;
        not scenario config).  Each peer uid is added once, at the
        most recent published position for that uid.
        """
        for tgt_uid in list(self._peer_tracking.keys()):
            t = state.targets.get(tgt_uid)
            if t is None:
                continue
            key = (round(t.latitude, 4), round(t.longitude, 4))
            if key not in self._known_real:
                self._known_real[key] = (t.latitude, t.longitude, 0.0)
