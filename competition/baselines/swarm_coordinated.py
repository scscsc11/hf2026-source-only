"""SwarmCoordinatedAgent v2 — 赛题三 agent（修复 v1 搜索/锁定死循环）。

v1 实测 0 分根因：detected 依赖云台主动瞄准目标进 FOV（引擎语义
``detected = offset_deg < half_fov``），而 v1 的 SEARCH 用扫描云台导致
detected 断续，VERIFY 永不确认 → 永不进 TRACK → 0 分。

v2 用新的 ACQUIRE 态打破死循环：SEARCH 中 detected 任意一帧即停止扫描，
云台钉住检测方位 → detected 连续 → 能判速 → 进 TRACK。SEARCH/ACQUIRE/TRACK
完整逻辑由后续 task 填充；本文件是骨架：常量/几何/状态名/SEARCH→ACQUIRE 转换。

严格遵守数据隔离：只读 ``obs.self`` / ``obs.comm_inbox`` / ``obs.briefing``。
设计见 docs/superpowers/specs/2026-07-15-swarm-coordinated-redesign.md
"""
from __future__ import annotations

import hashlib
import math
from collections import deque
from typing import Deque, List, Optional, Tuple

from competition.sdk.core.commands import (Command, broadcast, fly_to,
                                           point_gimbal, report_target,
                                           set_gimbal_fov)
from competition.sdk.scenarios.adversarial_swarm import SwarmAgent
from competition.sdk.scenarios.adversarial_swarm.observation import SwarmObs

# ── mission geometry (real scenario.json — v1 had these WRONG) ─────────────
# UAV altitude = 500 m, camera FOV = 30° (not 60), auto_track=false.
# Terrain bbox mirrors the scenario boundary (pre-match constants, not truth).
_BBOX: Tuple[Tuple[float, float], Tuple[float, float]] = (
    (26.982, 124.980), (27.025, 125.020))
_SAFEBOX_MARGIN_M = 600.0          # > penalty_boundary_margin_m (500)
_SEARCH_ALT = 500.0                # real UAV altitude
_TRACK_LOITER = 330.0              # K=3 环半径，两两 330·√3≈572m > 200 近罚线
_PROXIMITY_FLOOR_M = 200.0         # penalty threshold

# scoring-rule constants (mirrors profile_adversarial_swarm_search; read-only)
_K = 3                             # ≥K simultaneous effective trackers to destroy
_DWELL_TARGET_S = 20.0             # sustained dwell to destroy


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Absolute bearing 0=N, clockwise, from pt1 → pt2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = (math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _bbox_inset(bbox, margin_m: float):
    (lat_min, lon_min), (lat_max, lon_max) = bbox
    lat_mid = (lat_min + lat_max) / 2
    dlat = margin_m / 111320.0
    dlon = margin_m / (111320.0 * math.cos(math.radians(lat_mid)))
    return ((lat_min + dlat, lon_min + dlon), (lat_max - dlat, lon_max - dlon))


_SAFEBOX = _bbox_inset(_BBOX, _SAFEBOX_MARGIN_M)


def _clamp_to_safebox(lat: float, lon: float) -> Tuple[float, float]:
    (lat_min, lon_min), (lat_max, lon_max) = _SAFEBOX
    return (min(max(lat, lat_min), lat_max),
            min(max(lon, lon_min), lon_max))


def _uid_rank(uid: str) -> int:
    """Stable, *collision-light* integer rank for a uid.

    All UAVs compute the SAME ranking → identical team consensus with no
    central coordinator. We prefer a natural-order index when one is
    recoverable (numeric ids like the engine's ``20001..20010``, or
    ``uav_6``-style ids) so the fleet fans out in order; otherwise we hash.
    """
    if uid.isdigit():
        return int(uid)
    # foo_6 / uav-6 / uav 6 → trailing integer
    tail = uid.rsplit("_", 1)[-1] if "_" in uid else \
        uid.rsplit("-", 1)[-1] if "-" in uid else uid.rsplit(" ", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return int(hashlib.md5(uid.encode()).hexdigest(), 16)


def _grid_cells(bbox, rows: int = 2, cols: int = 5):
    """Split the mission bbox into a rows×cols grid; return cell centres."""
    (lat_min, lon_min), (lat_max, lon_max) = bbox
    dlat = (lat_max - lat_min) / rows
    dlon = (lon_max - lon_min) / cols
    cells = []
    for r in range(rows):
        for c in range(cols):
            cells.append((lat_min + dlat * (r + 0.5),
                          lon_min + dlon * (c + 0.5)))
    return cells


_GRID = _grid_cells(_BBOX, rows=2, cols=5)   # 10 cells, one per UAV


def _uid_cell(uid: str, n_cells: int = 10) -> Tuple[float, float]:
    """Deterministic uid → search cell so the fleet fans out across the
    whole area on tick 0 (no comm needed for the initial sweep)."""
    return _GRID[_uid_rank(uid) % n_cells]


def _in_any_approx_zone(lat, lon, briefing) -> Optional[object]:
    """Is (lat,lon) inside any briefing approx-zone bbox? Returns the zone
    (to read alt_max) or None. The bbox is pre-expanded ~20% larger than the
    true region, so entering it is a conservative threat trigger."""
    for z in getattr(briefing, "approximate_zones", ()) or ():
        (lat_min, lon_min), (lat_max, lon_max) = z.bbox
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return z
    return None


def _safe_alt_for(lat, lon, briefing, base_alt: float) -> float:
    """If the point is inside a static-threat bbox, climb above its alt_max;
    otherwise stay at base_alt. SAM alt_max is 2500 → 3000 clears it."""
    zone = _in_any_approx_zone(lat, lon, briefing)
    if zone is not None:
        return max(3000.0, getattr(zone, "alt_max", 2500.0) + 200.0)
    return base_alt


class _EMATracker:
    """EMA-smoothed (lat,lon) of one candidate + a least-squares speed fit.

    ``value`` is the denoised position (used for tracking & reporting:
    the EMA cuts σ=50m noise by ~1/√(α/2)). ``speed_mps`` fits a line to the
    SMOOTHED latitude series — never the raw samples, whose per-sample noise
    (σ=50m) dwarfs the 4-8 m/s real-target motion (only 24-64m over an 8s
    window) and would otherwise make every static decoy look fast.

    NOTE on the discrimination limit: even on the smoothed series, σ=50m
    noise means a static decoy's smoothed trajectory still wanders enough
    that its apparent speed overlaps the low end of a slow (4-5 m/s) real
    target's within an 8s ACQUIRE window. So speed discrimination is
    reliable in the DETERMINISTIC/low-noise regime and a useful prior in
    the noisy regime, but under full σ=50m noise a slow target and a noisy
    decoy are not cleanly separable in 8s (see spec §7 risk note). The
    8s ACQUIRE timeout bounds the worst case so a misjudged decoy is
    re-acquired later rather than locking the agent up.
    """

    def __init__(self, alpha: float = 0.3, history: int = 80):
        self._alpha = alpha
        self._lat: Optional[float] = None
        self._lon: Optional[float] = None
        # smoothed-position snapshots (lat only — speed is a latitude slope)
        self._smooth: Deque[float] = deque(maxlen=history)

    def append(self, lat: float, lon: float) -> None:
        if self._lat is None:
            self._lat, self._lon = lat, lon
        else:
            a = self._alpha
            self._lat = self._lat * (1 - a) + lat * a
            self._lon = self._lon * (1 - a) + lon * a
        self._smooth.append(self._lat)

    @property
    def value(self) -> Optional[Tuple[float, float]]:
        if self._lat is None:
            return None
        return (self._lat, self._lon)

    def speed_mps(self, tick_hz: float = 10.0) -> float:
        """Least-squares slope of the SMOOTHED latitude series → |m/s|.

        Uses the EMA-filtered positions, whose noise std is ~σ·√(α/(2−α))
        (≈0.42·σ for α=0.3), so a genuine 4-8 m/s trend dominates the
        residual wander once enough samples accrue. Returns 0 until the
        series is long enough for a stable fit.
        """
        n = len(self._smooth)
        if n < 10:
            return 0.0
        ts = list(range(n))
        ns = float(n)
        sx = sum(ts)
        sy = sum(self._smooth)
        sxx = sum(t * t for t in ts)
        sxy = sum(t * la for t, la in zip(ts, self._smooth))
        denom = ns * sxx - sx * sx
        if abs(denom) < 1e-20:
            return 0.0
        slope = (ns * sxy - sx * sy) / denom   # deg per tick
        return abs(slope) * 111320.0 * tick_hz  # → m/s

    def reset(self) -> None:
        self._lat = self._lon = None
        self._smooth.clear()


class SwarmCoordinatedAgent(SwarmAgent):
    """Distributed, comm-only coordinated swarm agent (v2).

    Per-UAV state machine:  SEARCH → ACQUIRE → TRACK.

    The defining change from v1 is the **ACQUIRE** state. In the real engine
    ``detected`` is a geometric FOV check that only fires where the gimbal
    aims, so v1's scanning SEARCH produced intermittent detections that the
    VERIFY state could never confirm. ACQUIRE locks the gimbal on the first
    detected frame, making ``detected`` continuous so target speed can be
    measured and the agent can advance to TRACK.

    Coordination is emergent: every UAV runs the same deterministic
    selection over the shared confirmed-target set (built from the ≤50-byte
    comm channel), so the K lowest-rank free UAVs self-select onto the
    chosen target with no leader election and no shared memory.

    NOTE: SEARCH/ACQUIRE/TRACK + greedy allocation + threat/jam evasion are
    fully implemented here. Verified by the unit tests (logic/geometry) and
    the perception-pipeline integration test (closed-loop SEARCH→ACQUIRE→TRACK
    with mock detection that faithfully replicates the engine FOV geometry).
    """

    # state machine
    SEARCH = "SEARCH"
    ACQUIRE = "ACQUIRE"
    TRACK = "TRACK"

    def configure(self, config) -> None:
        # search
        self._search_alt: float = _SEARCH_ALT
        self._search_fov: float = 30.0          # real camera FOV
        self._search_speed: float = 24.0
        self._sweep_period: float = 4.0
        self._sweep_deg: float = 25.0           # ±pan sweep around cell bearing
        # ACQUIRE (decoy discrimination by regression speed)
        self._acquire_timeout: float = 8.0
        self._acquire_warmup: float = 3.0       # need ~30 samples for stable slope
        self._acquire_speed_confirm: float = 3.0   # m/s → real target
        self._acquire_speed_reject: float = 2.0    # m/s → decoy
        self._ema_alpha: float = 0.3
        # TRACK
        self._track_fov: float = 30.0
        self._track_speed: float = 26.0
        self._track_loiter: float = _TRACK_LOITER
        self._track_timeout: float = 45.0       # abandon a target that won't die
        # comms / report
        self._bc_period: int = 8            # broadcast cadence (≤4Hz window safe)
        self._report_period: int = 12       # ~1Hz judge rate-limit friendly
        # runtime state
        self._t: float = 0.0
        self._tick: int = 0
        self._home_lat: float = 0.0
        self._home_lon: float = 0.0
        self._cell: Tuple[float, float] = _uid_cell(self.my_uid)
        self._phase: float = (_uid_rank(self.my_uid) % 100) / 100.0
        self._state = self.SEARCH
        self._candidate: Optional[Tuple[float, float]] = None
        self._ema = _EMATracker(self._ema_alpha)
        self._acquire_t: float = 0.0
        self._track_t: float = 0.0
        # shared knowledge built from comms (each UAV maintains its own copy)
        self._confirmed: List[Tuple[float, float]] = []   # real targets seen
        self._known_decoys: List[Tuple[float, float]] = []
        self._claims: dict = {}        # tgt_idx → set of claiming uav ranks
        self._last_report_t: float = -1e9

    def reset(self) -> None:
        self._t = 0.0
        self._tick = 0
        self._home_lat = 0.0
        self._home_lon = 0.0
        self._cell = _uid_cell(self.my_uid)
        self._phase = (_uid_rank(self.my_uid) % 100) / 100.0
        self._state = self.SEARCH
        self._candidate = None
        self._ema = _EMATracker(self._ema_alpha)
        self._acquire_t = 0.0
        self._track_t = 0.0
        self._confirmed = []
        self._known_decoys = []
        self._claims = {}
        self._last_report_t = -1e9

    # ── comm protocol ────────────────────────────────────────────────────
    # "T:lat,lon"      — confirmed real target share
    # "D:lat,lon"      — confirmed decoy location share (so teammates skip it)
    # "A:tgtidx,rank"  — tracking claim on target #tgtidx by uav `rank`
    # "J:lat,lon"      — dynamic-jam self-warning (teammates detour)
    def _ingest_comms(self, comm_inbox) -> None:
        for m in comm_inbox:
            p = m.payload
            try:
                if p.startswith("T:"):
                    la, lo = p[2:].split(",")
                    pos = (float(la), float(lo))
                    if not self._near_any(pos, self._confirmed, 200.0):
                        self._confirmed.append(pos)
                elif p.startswith("D:"):
                    la, lo = p[2:].split(",")
                    pos = (float(la), float(lo))
                    if not self._near_any(pos, self._known_decoys, 150.0):
                        self._known_decoys.append(pos)
                elif p.startswith("A:"):
                    idx, rank = p[2:].split(",")
                    self._claims.setdefault(int(idx), set()).add(int(rank))
            except Exception:
                pass   # malformed payload — ignore, never crash

    @staticmethod
    def _near_any(pos, lst, thr_m: float) -> bool:
        return any(_haversine_m(pos[0], pos[1], p[0], p[1]) < thr_m
                   for p in lst) if lst else False

    def _confirm_real(self, pos: Tuple[float, float]) -> None:
        if not self._near_any(pos, self._confirmed, 200.0):
            self._confirmed.append(pos)

    def _confirm_decoy(self, pos: Tuple[float, float]) -> None:
        if not self._near_any(pos, self._known_decoys, 150.0):
            self._known_decoys.append(pos)

    # ── deterministic team assignment (emergent consensus) ───────────────
    # Greedy self-selection (spec §3.4): every UAV runs the SAME deterministic
    # rule over the shared confirmed-target set, so the K lowest-rank free
    # UAVs self-select onto each target with no leader election. Targets are
    # ranked by distance-to-self; a target is "open" if it has <K announced
    # claimants. The slot is this UAV's ordinal position among the target's
    # claimants (lower rank claims earlier slots).
    def _tgt_index(self, tgt: Tuple[float, float]) -> int:
        """Stable index of a confirmed target (its position in the sorted
        confirmed list). All UAVs that ingest the same T: messages build the
        same list ordering, so they agree on indices."""
        for i, p in enumerate(self._confirmed):
            if _haversine_m(p[0], p[1], tgt[0], tgt[1]) < 200.0:
                return i
        return -1

    def _claim_count(self, tgt_idx: int) -> int:
        return len(self._claims.get(tgt_idx, ()))

    def _record_own_claim(self, tgt: Tuple[float, float],
                          fleet_size: int) -> None:
        """Record THIS UAV's own claim on a target in ``_claims`` (I2).

        Without this, ``_claim_count`` under-counts by 1: a UAV that has
        selected/tracked a target has not yet announced (or its inbound A:
        hasn't arrived), so its own slot is missing from ``_claims``. Greedy
        self-selection then thinks the target has one fewer claimant than it
        really does, and over-assigns. Recording the own claim locally keeps
        ``_claim_count`` consistent with this UAV's actual commitment.
        """
        idx = self._tgt_index(tgt)
        if idx >= 0:
            self._claims.setdefault(idx, set()).add(
                _uid_rank(self.my_uid) % max(1, fleet_size))

    def _select_target(self, self_pos: Tuple[float, float]
                       ) -> Optional[Tuple[float, float]]:
        """Greedy: pick the NEAREST confirmed target that has <K claimants.
        Falls back to nearest overall if every target is saturated (better to
        over-claim a near target than fly to a far one)."""
        if not self._confirmed:
            return None
        open_tgts = [p for p in self._confirmed
                     if self._claim_count(self._tgt_index(p)) < _K]
        pool = open_tgts if open_tgts else self._confirmed
        return min(pool,
                   key=lambda p: _haversine_m(self_pos[0], self_pos[1],
                                              p[0], p[1]))

    def _slot_for_target(self, tgt: Tuple[float, float],
                         fleet_size: int) -> int:
        """This UAV's azimuth slot for a target. The slot is its ordinal
        position among the target's announced claimants, ordered by rank
        (lower rank → earlier slot). Claimants include this UAV's own rank
        so a freshly-acquired target (no inbound A: yet) still yields slot 0.
        Slot is taken mod K so extra (saturated-target) claimants spread."""
        if fleet_size <= 0:
            fleet_size = 10
        idx = self._tgt_index(tgt)
        claimants = set(self._claims.get(idx, ()))
        my_rank = _uid_rank(self.my_uid) % fleet_size
        claimants.add(my_rank)
        ordered = sorted(claimants)
        slot = ordered.index(my_rank) if my_rank in ordered else len(ordered)
        return slot % _K

    def _team_aim_point(self, tgt: Tuple[float, float], slot: int
                        ) -> Tuple[float, float]:
        """Place this UAV on a distinct azimuth of the loiter ring so a 3-UAV
        team spreads out (pairwise ≈ R·√3 ≈ 572m > 200m at R=330). The sector
        base is a FIXED bearing from the target to the scene centre (same for
        every UAV) so all three agree on the sector layout."""
        (c_lat_min, c_lon_min), (c_lat_max, c_lon_max) = _BBOX
        scene_c_lat = (c_lat_min + c_lat_max) / 2.0
        scene_c_lon = (c_lon_min + c_lon_max) / 2.0
        sector_base = _bearing_deg(tgt[0], tgt[1], scene_c_lat, scene_c_lon)
        approach_brg = (sector_base + slot * (360.0 / _K)) % 360.0
        dlat = (self._track_loiter * math.cos(math.radians(approach_brg))) / 111320.0
        dlon = (self._track_loiter * math.sin(math.radians(approach_brg))) / \
               (111320.0 * math.cos(math.radians(tgt[0])))
        return _clamp_to_safebox(tgt[0] + dlat, tgt[1] + dlon)

    # ── gimbal / search geometry ─────────────────────────────────────────
    def _jam_evasion(self, obs: SwarmObs) -> List[Command]:
        """Dynamic-jam evasion (spec §3.5): when this UAV senses it is jammed,
        broadcast a J: warning to teammates and fly 600m away from the current
        position (toward the scene centre, which is outside any local jam
        region) to break out of the jam footprint. Uses a fixed retreat
        bearing (current pos → home/scene centre) so every UAV retreats the
        same direction and stays in formation."""
        cmds: List[Command] = []
        slat, slon = obs.self.lat, obs.self.lon
        # retreat toward home if known, else toward the bbox centre
        tgt_lat = self._home_lat if self._home_lat != 0.0 else \
            (_BBOX[0][0] + _BBOX[1][0]) / 2.0
        tgt_lon = self._home_lon if self._home_lon != 0.0 else \
            (_BBOX[0][1] + _BBOX[1][1]) / 2.0
        brg = _bearing_deg(slat, slon, tgt_lat, tgt_lon)
        dlat = (600.0 * math.cos(math.radians(brg))) / 111320.0
        dlon = (600.0 * math.sin(math.radians(brg))) / \
            (111320.0 * math.cos(math.radians(slat)))
        rlat, rlon = _clamp_to_safebox(slat + dlat, slon + dlon)
        alt = _safe_alt_for(rlat, rlon, obs.briefing, self._search_alt)
        cmds.append(broadcast(f"J:{slat:.5f},{slon:.5f}"))
        cmds.append(fly_to(rlat, rlon, alt=alt, speed=self._track_speed))
        cmds.append(set_gimbal_fov(self._search_fov))
        return cmds

    def _tracking_gimbal(self, self_lat, self_lon, self_heading,
                         tgt_lat, tgt_lon) -> Tuple[float, float]:
        """Body-frame (pan, tilt) to point the optical axis at a target.

        pan is relative to heading (body frame) so the gimbal stays on the
        target despite body rotation; tilt is the negative look-down angle.
        """
        brg = _bearing_deg(self_lat, self_lon, tgt_lat, tgt_lon)
        pan = ((brg - self_heading + 180.0) % 360.0) - 180.0
        ground = max(1.0, _haversine_m(self_lat, self_lon, tgt_lat, tgt_lon))
        tilt = -math.degrees(math.atan2(self._search_alt, ground))
        return pan, tilt

    def _search_geometry(self, obs: SwarmObs
                         ) -> Tuple[float, float, float, float]:
        """Loiter over my uid-claimed cell while sweeping the gimbal.
        Returns ``(aim_lat, aim_lon, pan, tilt)``."""
        clat, clon = self._cell
        slat, slon = obs.self.lat, obs.self.lon
        # sweep pan ±_sweep_deg around the bearing to my cell centre
        cell_brg = _bearing_deg(slat, slon, clat, clon)
        rel_brg = ((cell_brg - obs.self.heading_deg + 180.0) % 360.0) - 180.0
        sweep = self._sweep_deg * math.sin(2 * math.pi * self._t / self._sweep_period)
        pan = rel_brg + sweep
        ground = max(1.0, _haversine_m(slat, slon, clat, clon))
        tilt = -math.degrees(math.atan2(self._search_alt, ground))
        return clat, clon, pan, tilt

    # ── decide ───────────────────────────────────────────────────────────
    def decide(self, obs: SwarmObs, dt: float) -> List[Command]:
        self._tick += 1
        self._t += dt
        if self._home_lat == 0.0:
            self._home_lat = obs.self.lat
            self._home_lon = obs.self.lon

        det = obs.self.detection
        cmds: List[Command] = []
        self._ingest_comms(obs.comm_inbox)
        fleet = getattr(obs.briefing, "fleet_size", 10) or 10

        # ── dynamic-jam evasion (cross-state, spec §3.5) ──
        # Sensing jammed overrides the state machine: warn teammates and fly
        # out of the jam footprint. Detection comms are useless while jammed.
        if getattr(obs.self, "jammed", False):
            return self._jam_evasion(obs)

        # ── SEARCH: loiter my cell + sweep until the FIRST detection ──
        # The defining v2 fix: any single detected frame → ACQUIRE, so the
        # gimbal stops sweeping and detection becomes continuous.
        if self._state == self.SEARCH:
            if (det.detected and det.target_lat is not None
                    and det.target_lon is not None
                    and not self._near_any(
                        (det.target_lat, det.target_lon),
                        self._known_decoys, 150.0)):
                self._state = self.ACQUIRE
                self._candidate = (det.target_lat, det.target_lon)
                self._ema = _EMATracker(self._ema_alpha)
                self._ema.append(det.target_lat, det.target_lon)
                self._acquire_t = 0.0
                # Issue the first ACQUIRE gimbal lock now but do NOT re-feed
                # this same detection sample (already appended above) — a
                # second append double-counts the transition tick into the
                # EMA. Return here, mirroring the `else: return` branch.
                tgt = self._candidate
                tlat, tlon = tgt
                alt = _safe_alt_for(tlat, tlon, obs.briefing, self._search_alt)
                cmds.append(fly_to(tlat, tlon, alt=alt, speed=self._track_speed,
                                   loiter_radius=150.0))
                pan, tilt = self._tracking_gimbal(
                    obs.self.lat, obs.self.lon, obs.self.heading_deg, tlat, tlon)
                cmds.append(point_gimbal(pan, tilt))
                cmds.append(set_gimbal_fov(self._track_fov))
                return cmds
            else:
                # ── SEARCH→TRACK on a received T: (spec §3.0/§3.4, I1) ──
                # No local detection, but a teammate may have shared a
                # confirmed target via T:. If greedy self-selection says this
                # UAV should take one (there's an open / nearest confirmed
                # target), jump STRAIGHT to TRACK (skipping ACQUIRE): the
                # teammate already confirmed it real, so the fleet converges
                # K=3 trackers without every UAV re-confirming locally.
                # Local detection → ACQUIRE (above) always takes priority
                # since this UAV has eyes on its own contact.
                tgt = self._select_target((obs.self.lat, obs.self.lon))
                if tgt is not None:
                    self._confirm_real(tgt)        # idempotent; ensures index
                    self._record_own_claim(tgt, fleet)   # I2: count ourselves
                    self._candidate = tgt
                    self._ema = _EMATracker(self._ema_alpha)
                    self._track_t = 0.0
                    self._state = self.TRACK
                    # Fall through to the TRACK branch below so the first
                    # TRACK gimbal/fly command is issued THIS tick (the
                    # candidate is now set; TRACK re-selects/adopts it).
                else:
                    slat, slon, pan, tilt = self._search_geometry(obs)
                    slat, slon = _clamp_to_safebox(slat, slon)
                    alt = _safe_alt_for(slat, slon, obs.briefing,
                                        self._search_alt)
                    cmds.append(fly_to(slat, slon, alt=alt,
                                       speed=self._search_speed,
                                       loiter_radius=400.0))
                    cmds.append(point_gimbal(pan, tilt))
                    cmds.append(set_gimbal_fov(self._search_fov))
                    return cmds

        # ── ACQUIRE: lock the gimbal + discriminate by regression speed ──
        # The defining v2 fix: STOP sweeping, lock the gimbal on the candidate
        # so detection stays continuous, then fit a regression speed to the
        # SMOOTHED latitude series. In the deterministic/low-noise regime this
        # separates moving targets (4-8 m/s) from static decoys (0 m/s); under
        # full σ=50m noise a slow target and a noisy decoy overlap within the
        # 8s window (spec §7), so the 8s timeout bounds any misjudgement.
        if self._state == self.ACQUIRE:
            self._acquire_t += dt
            tgt = self._candidate or (obs.self.lat, obs.self.lon)
            # adopt fresh detections that track the same object
            if (det.detected and det.target_lat is not None
                    and _haversine_m(det.target_lat, det.target_lon,
                                     tgt[0], tgt[1]) < 250.0):
                self._ema.append(det.target_lat, det.target_lon)
                if self._ema.value is not None:
                    self._candidate = self._ema.value
                    tgt = self._ema.value
            tlat, tlon = tgt
            alt = _safe_alt_for(tlat, tlon, obs.briefing, self._search_alt)
            cmds.append(fly_to(tlat, tlon, alt=alt, speed=self._track_speed,
                               loiter_radius=150.0))
            pan, tilt = self._tracking_gimbal(
                obs.self.lat, obs.self.lon, obs.self.heading_deg, tlat, tlon)
            cmds.append(point_gimbal(pan, tilt))
            cmds.append(set_gimbal_fov(self._track_fov))

            # ── regression-speed discrimination (after warmup) ──
            spd = self._ema.speed_mps(tick_hz=1.0 / dt) if dt > 0 else 0.0
            if self._acquire_t >= self._acquire_warmup:
                if spd >= self._acquire_speed_confirm:
                    # REAL target: confirm, broadcast T:, advance to TRACK
                    self._confirm_real(tgt)
                    cmds.append(broadcast(f"T:{tlat:.5f},{tlon:.5f}"))
                    self._state = self.TRACK
                    self._track_t = 0.0
                    return cmds
                if (spd <= self._acquire_speed_reject
                        and self._acquire_t >= 5.0):
                    # DECOY: record, share D:, back to SEARCH
                    self._confirm_decoy(tgt)
                    cmds.append(broadcast(f"D:{tlat:.5f},{tlon:.5f}"))
                    self._state = self.SEARCH
                    self._candidate = None
                    self._ema.reset()
                    return cmds
            # timeout: indeterminate (noisy/lost detection) → back to SEARCH
            if self._acquire_t > self._acquire_timeout:
                self._state = self.SEARCH
                self._candidate = None
                self._ema.reset()
            return cmds

        # ── TRACK: K=3 spread loiter + report + abandon ──
        # Greedy-select a confirmed target, fly to the 330m ring at this UAV's
        # 120° slot, re-aim the gimbal every frame so detection stays continuous
        # and dwell accumulates. report_target ~1Hz; broadcast T: ~1.25Hz.
        if self._state == self.TRACK:
            self._track_t += dt
            # Greedy self-selection over the confirmed set (drops a destroyed
            # or bad candidate for the next-nearest open target).
            tgt = self._select_target((obs.self.lat, obs.self.lon))
            if tgt is None:
                tgt = self._candidate or (obs.self.lat, obs.self.lon)
            self._candidate = tgt
            # I2: keep our OWN claim counted locally so _claim_count (and thus
            # greedy self-selection fleet-wide) is accurate even before our
            # A: broadcast lands or is re-ingested by teammates.
            self._record_own_claim(tgt, fleet)
            # feed fresh detections into the EMA (keeps the report denoised)
            if (det.detected and det.target_lat is not None
                    and _haversine_m(det.target_lat, det.target_lon,
                                     tgt[0], tgt[1]) < 250.0):
                self._ema.append(det.target_lat, det.target_lon)
                if self._ema.value is not None:
                    tgt = self._ema.value
                    self._candidate = tgt
            tlat, tlon = tgt

            # Threat abandonment (spec §3.5): target inside a static-threat
            # bbox → don't dive into a SAM zone (would tilt the gimbal shallow
            # and risk destruction); abandon and pick the next target.
            if _in_any_approx_zone(tlat, tlon, obs.briefing) is not None:
                self._confirm_decoy(tgt)   # tag so we don't re-acquire it
                self._state = self.SEARCH
                self._candidate = None
                self._ema.reset()
                self._track_t = 0.0
                return cmds

            slot = self._slot_for_target(tgt, fleet)
            alat, alon = self._team_aim_point(tgt, slot)
            alt = _safe_alt_for(alat, alon, obs.briefing, self._search_alt)
            cmds.append(fly_to(alat, alon, alt=alt, speed=self._track_speed,
                               loiter_radius=self._track_loiter))
            pan, tilt = self._tracking_gimbal(
                obs.self.lat, obs.self.lon, obs.self.heading_deg, tlat, tlon)
            cmds.append(point_gimbal(pan, tilt))
            cmds.append(set_gimbal_fov(self._track_fov))
            if self._tick % self._report_period == 0:
                cmds.append(report_target(tlat, tlon))
            if self._tick % self._bc_period == 0:
                cmds.append(broadcast(f"T:{tlat:.5f},{tlon:.5f}"))
            # announce our claim so teammates know the slot is filling
            idx = self._tgt_index(tgt)
            if idx >= 0 and self._tick % self._bc_period == 0:
                cmds.append(broadcast(
                    f"A:{idx},{_uid_rank(self.my_uid) % fleet}"))
            # abandon a target that won't die (can't assemble K trackers)
            if self._track_t > self._track_timeout:
                self._state = self.SEARCH
                self._candidate = None
                self._ema.reset()
                self._track_t = 0.0
            return cmds

        # safety net — should not reach here
        slat, slon, pan, tilt = self._search_geometry(obs)
        slat, slon = _clamp_to_safebox(slat, slon)
        alt = _safe_alt_for(slat, slon, obs.briefing, self._search_alt)
        cmds.append(fly_to(slat, slon, alt=alt, speed=self._search_speed))
        cmds.append(point_gimbal(pan, tilt))
        return cmds
