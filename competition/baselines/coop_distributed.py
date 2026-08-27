"""Cooperative-decoy baseline agent (redesigned).

Pure-comms distributed agent (strict isolation: no teammate poses, no
score_view). Strategy:

  * PARTITION SEARCH: each UAV sweeps one of 3 longitude sub-regions (uid
    hash → int(uid)%n; centers ~1.3km apart > the 200m proximity penalty).
  * EMA DRIFT DECOY ID: the old max-min displacement test is 100% broken
    under σ=50m detection noise (stationary decoys show false movement).
    Instead, track the EMA of a candidate's detection positions: real
    targets (5-12 m/s) have drifting EMA; decoys (speed=0) converge. After
    ~8s, drift magnitude discriminates reliably.
  * FAST TRIAL: VERIFY a candidate for ≤8s; abandon decoys immediately
    (don't waste 20s tracking a decoy that can never be destroyed).
  * CLOSE TRACKING: loiter_radius=100m keeps the target stably in the FOV
    cone so detected=True persists → dwell accumulates → destroy at 20s.
  * HEADING COMPENSATION: pan = abs_bearing(target) − self_heading, so the
    optical axis stays world-fixed on the target despite body rotation.
  * REPORT DENOISE: report the EMA mean position (σ→σ/√N) for accuracy.

  TODO(K=2): 本 baseline 的扇区划分保证各 UAV 盯不同目标（per-agent
  自管 _dwell_time），在 K=2（≥2 架同时盯防才摧毁）下 kill 维度会拿 0
  分。后续需升级协同策略：发现高价值目标后广播召集多机收敛到同一目标。
"""
from __future__ import annotations

import hashlib
import math
from collections import deque
from typing import Deque, List, Optional, Tuple

from competition.sdk.core.commands import (Command, broadcast, fly_to,
                                           point_gimbal, report_target,
                                           set_gimbal_fov)
from competition.sdk.scenarios.coop_decoy import CoopAgent
from competition.sdk.scenarios.coop_decoy.observation import CoopObs

# ── mission geometry ─────────────────────────────────────────────────────
_BBOX: Tuple[Tuple[float, float], Tuple[float, float]] = (
    (26.982, 124.980), (27.025, 125.020))
_SAFEBOX_MARGIN_M = 600.0


def _bbox_inset(bbox, margin_m: float):
    (lat_min, lon_min), (lat_max, lon_max) = bbox
    lat_mid = (lat_min + lat_max) / 2
    dlat = margin_m / 111320.0
    dlon = margin_m / (111320.0 * math.cos(math.radians(lat_mid)))
    return ((lat_min + dlat, lon_min + dlon), (lat_max - dlat, lon_max - dlon))


_SAFEBOX = _bbox_inset(_BBOX, _SAFEBOX_MARGIN_M)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Absolute bearing 0=N clockwise from pt1 to pt2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = (math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _clamp_to_safebox(lat: float, lon: float) -> Tuple[float, float]:
    (lat_min, lon_min), (lat_max, lon_max) = _SAFEBOX
    return (min(max(lat, lat_min), lat_max),
            min(max(lon, lon_min), lon_max))


def _partition_centers(bbox, n: int = 3):
    (lat_min, lon_min), (lat_max, lon_max) = bbox
    lat_mid = (lat_min + lat_max) / 2
    sub_w = (lon_max - lon_min) / n
    return [(lat_mid, lon_min + sub_w * (i + 0.5)) for i in range(n)]


_PARTITION_CENTERS = _partition_centers(_BBOX, 3)


def _uid_phase(uid: str) -> float:
    h = int(hashlib.md5(uid.encode()).hexdigest(), 16)
    return (h % 1000) / 1000.0


def _uid_partition(uid: str) -> Tuple[float, float]:
    """Map uid to a partition center. int(uid)%n for numeric ids (real engine
    ids 20001/2/3 → distinct regions); md5 fallback otherwise."""
    n = len(_PARTITION_CENTERS)
    if uid.isdigit():
        idx = int(uid) % n
    elif "_" in uid:
        tail = uid.rsplit("_", 1)[-1]
        idx = int(tail) % n if tail.isdigit() else (
            int(hashlib.md5(uid.encode()).hexdigest(), 16) % n)
    else:
        idx = int(hashlib.md5(uid.encode()).hexdigest(), 16) % n
    return _PARTITION_CENTERS[idx]


class _EMATracker:
    """EMA of (lat, lon) positions + linear-regression speed estimate.

    append() updates the EMA (smoothed position for tracking/reporting) and
    buffers raw positions. speed_mps() fits a line to the raw lat sequence
    to estimate target speed — this distinguishes moving real targets
    (5-12 m/s) from stationary decoys (≈0) far more reliably than EMA drift
    under σ=50m noise (drift has ~90% false-positive rate in the first 3s;
    regression slope is stable from ~40 samples).
    """

    def __init__(self, alpha: float = 0.3, history: int = 80):
        self._alpha = alpha
        self._lat: Optional[float] = None
        self._lon: Optional[float] = None
        self._raw: Deque[Tuple[float, float]] = deque(maxlen=history)

    def append(self, lat: float, lon: float) -> None:
        if self._lat is None:
            self._lat, self._lon = lat, lon
        else:
            a = self._alpha
            self._lat = self._lat * (1 - a) + lat * a
            self._lon = self._lon * (1 - a) + lon * a
        self._raw.append((lat, lon))

    @property
    def value(self) -> Optional[Tuple[float, float]]:
        if self._lat is None:
            return None
        return (self._lat, self._lon)

    def speed_mps(self, tick_hz: float = 10.0) -> float:
        """Estimated target speed (m/s) via least-squares slope of raw lat.

        Returns 0 if fewer than 10 samples. Uses lat-slope × cos correction
        is unnecessary at this latitude for a speed-magnitude test.
        """
        n = len(self._raw)
        if n < 10:
            return 0.0
        ts = list(range(n))
        lats = [p[0] for p in self._raw]
        ns = float(n)
        sx = sum(ts); sy = sum(lats)
        sxx = sum(t * t for t in ts)
        sxy = sum(t * la for t, la in zip(ts, lats))
        denom = ns * sxx - sx * sx
        if abs(denom) < 1e-20:
            return 0.0
        slope = (ns * sxy - sx * sy) / denom   # deg per tick
        return abs(slope) * 111320.0 * tick_hz  # → m/s

    def reset(self) -> None:
        self._lat = self._lon = None
        self._raw.clear()


# ── agent ────────────────────────────────────────────────────────────────

class CoopDistributedAgent(CoopAgent):
    """Distributed cooperative agent: EMA-drift decoy ID + close tracking."""

    # state machine
    SEARCH = "SEARCH"
    VERIFY = "VERIFY"
    TRACK = "TRACK"

    def configure(self, config) -> None:
        # search geometry
        self._search_alt: float = 200.0
        self._search_radius: float = 700.0
        self._growth: float = 50.0
        self._ang_speed: float = 30.0
        self._sweep_period: float = 4.0
        self._pitch_min: float = -60.0
        self._pitch_max: float = -30.0
        # FOV (competition rule caps camera FOV at 50°)
        self._track_fov: float = 50.0
        self._search_fov: float = 50.0
        # VERIFY (decoy discrimination by regression speed)
        self._verify_timeout: float = 8.0         # total verify budget
        self._verify_warmup: float = 3.0          # need ~30 samples for stable slope
        self._verify_speed_confirm: float = 3.0   # m/s → real target (decoy P95≈4.5 but median 1.8)
        self._verify_speed_reject: float = 2.5    # m/s → decoy (after reject_min_t)
        self._verify_reject_min_t: float = 5.0    # need this long before reject
        self._ema_alpha: float = 0.3
        # TRACK (destroy)
        self._dwell_target: float = 20.0
        self._dwell_grace: float = 2.0
        self._track_timeout: float = 35.0
        self._loiter_close: float = 100.0
        # report
        self._bc_period: int = 8
        # runtime state
        self._t: float = 0.0
        self._tick: int = 0
        self._region = _uid_partition(self.my_uid)
        self._phase: float = _uid_phase(self.my_uid)
        self._state = self.SEARCH
        self._candidate: Optional[Tuple[float, float]] = None
        self._ema = _EMATracker(self._ema_alpha)
        self._verify_t: float = 0.0
        self._dwell_time: float = 0.0
        self._last_det_tick: float = -1e9
        self._track_t: float = 0.0
        self._shared_targets: list = []
        self._last_report_t: float = -1e9
        self._known_decoys: list = []

    def reset(self) -> None:
        self._t = 0.0
        self._tick = 0
        self._state = self.SEARCH
        self._candidate = None
        self._ema = _EMATracker(self._ema_alpha)
        self._verify_t = 0.0
        self._dwell_time = 0.0
        self._last_det_tick = -1e9
        self._track_t = 0.0
        self._shared_targets = []
        self._last_report_t = -1e9
        self._known_decoys = []
        self._region = _uid_partition(self.my_uid)
        self._phase = _uid_phase(self.my_uid)

    # ── helpers ──

    def _ingest_comms(self, comm_inbox) -> list:
        found = []
        for m in comm_inbox:
            p = m.payload
            if p.startswith("T:"):
                try:
                    la, lo = p[2:].split(",")
                    found.append((float(la), float(lo)))
                except Exception:
                    pass
        self._shared_targets = found
        return found

    def _tracking_gimbal(self, self_lat, self_lon, self_heading,
                         tgt_lat, tgt_lon) -> Tuple[float, float]:
        brg = _bearing_deg(self_lat, self_lon, tgt_lat, tgt_lon)
        pan = ((brg - self_heading + 180.0) % 360.0) - 180.0
        ground = max(1.0, _haversine_m(self_lat, self_lon, tgt_lat, tgt_lon))
        tilt = -math.degrees(math.atan2(self._search_alt, ground))
        return pan, tilt

    def _spiral(self) -> Tuple[float, float, float, float]:
        home_lat, home_lon = self._region
        t = self._t + self._phase * 12.0
        bearing = (self._ang_speed * t) % 360.0
        revs = (self._ang_speed * t) / 360.0
        radius = max(1.0, min(self._search_radius, self._growth * revs))
        dlat = (radius * math.cos(math.radians(bearing))) / 111320.0
        dlon = (radius * math.sin(math.radians(bearing))) / \
               (111320.0 * math.cos(math.radians(home_lat)))
        phase = (t % self._sweep_period) / self._sweep_period
        tilt = self._pitch_min + (self._pitch_max - self._pitch_min) * 0.5 * \
               (1 - math.cos(2 * math.pi * phase))
        pan_phase = (t % (self._sweep_period * 2)) / (self._sweep_period * 2)
        pan = -90.0 + 180.0 * 0.5 * (1 - math.cos(2 * math.pi * pan_phase))
        return home_lat + dlat, home_lon + dlon, pan, tilt

    # ── decide ──

    def decide(self, obs: CoopObs, dt: float) -> List[Command]:
        self._tick += 1
        self._t += dt
        sim_t = self._t
        det = obs.self.detection
        cmds: List[Command] = []
        self._ingest_comms(obs.comm_inbox)

        # ── SEARCH ──
        if self._state == self.SEARCH:
            if det.detected and det.target_lat is not None:
                # skip detections near a known decoy (confirmed slow)
                near_decoy = any(
                    _haversine_m(det.target_lat, det.target_lon, d[0], d[1]) < 150.0
                    for d in self._known_decoys) if self._known_decoys else False
                if not near_decoy:
                    # new candidate → start verifying
                    self._state = self.VERIFY
                    self._candidate = (det.target_lat, det.target_lon)
                    self._ema = _EMATracker(self._ema_alpha)
                self._ema.append(det.target_lat, det.target_lon)
                self._verify_t = 0.0
            # NOTE: K=1 → do NOT adopt teammate-shared targets. Converging on
            # the same target puts UAVs <200m apart (proximity penalty, up to
            # −15). Each UAV searches its own partition independently.
            if self._state == self.SEARCH:
                slat, slon, pan, tilt = self._spiral()
                slat, slon = _clamp_to_safebox(slat, slon)
                cmds.append(fly_to(slat, slon, alt=self._search_alt, speed=22.0))
                cmds.append(point_gimbal(pan, tilt))
                cmds.append(set_gimbal_fov(self._search_fov))
                return cmds

        # ── VERIFY (discriminate real vs decoy by regression speed) ──
        if self._state == self.VERIFY:
            self._verify_t += dt
            tgt = self._candidate
            # update tracker if we still see a target near the candidate
            if det.detected and det.target_lat is not None:
                d = _haversine_m(det.target_lat, det.target_lon,
                                 tgt[0], tgt[1])
                if d < 250.0:   # same object
                    self._ema.append(det.target_lat, det.target_lon)
                    self._candidate = self._ema.value  # follow smoothed pos
                    tgt = self._candidate
            # speed estimate via linear regression on raw detections — far
            # more reliable than EMA drift under σ=50m noise.
            speed = self._ema.speed_mps()
            confirmed = (self._verify_t >= self._verify_warmup
                         and speed >= self._verify_speed_confirm)
            rejected = (self._verify_t >= self._verify_reject_min_t
                        and speed < self._verify_speed_reject)
            if confirmed:
                self._state = self.TRACK
                self._dwell_time = 0.0
                self._track_t = 0.0
                self._last_det_tick = sim_t
                # fall through to TRACK this tick
            elif rejected or self._verify_t >= self._verify_timeout:
                # decoy (slow) or uncertain → abandon; remember if confirmed slow
                if rejected and self._ema.value:
                    self._known_decoys.append(self._ema.value)
                self._state = self.SEARCH
                self._candidate = None
                self._ema.reset()
                slat, slon, pan, tilt = self._spiral()
                slat, slon = _clamp_to_safebox(slat, slon)
                cmds.append(fly_to(slat, slon, alt=self._search_alt, speed=22.0))
                cmds.append(point_gimbal(pan, tilt))
                cmds.append(set_gimbal_fov(self._search_fov))
                return cmds
            else:
                # still verifying: fly close + aim gimbal
                tlat, tlon = _clamp_to_safebox(*tgt)
                cmds.append(fly_to(tlat, tlon, alt=self._search_alt, speed=22.0,
                                   loiter_radius=self._loiter_close))
                pan, tilt = self._tracking_gimbal(
                    obs.self.lat, obs.self.lon, obs.self.heading_deg, tgt[0], tgt[1])
                cmds.append(point_gimbal(pan, tilt))
                cmds.append(set_gimbal_fov(self._track_fov))
                return cmds

        # ── TRACK (destroy by 20s continuous lock) ──
        if self._state == self.TRACK:
            self._track_t += dt
            tgt = self._candidate
            # follow moving target via EMA
            if det.detected and det.target_lat is not None:
                d = _haversine_m(det.target_lat, det.target_lon,
                                 tgt[0], tgt[1])
                if d < 250.0:
                    self._ema.append(det.target_lat, det.target_lon)
                    self._candidate = self._ema.value
                    tgt = self._candidate
            # dwell accumulation (mirrors engine: detected → accumulate)
            tracking = det.detected and det.target_lat is not None and \
                _haversine_m(det.target_lat, det.target_lon, tgt[0], tgt[1]) < 250.0
            if tracking:
                gap = sim_t - self._last_det_tick
                if self._dwell_time > 0 and gap <= self._dwell_grace + dt:
                    self._dwell_time += dt
                elif self._dwell_time == 0:
                    self._dwell_time += dt
                else:
                    self._dwell_time = dt
                self._last_det_tick = sim_t
            # broadcast + report
            if self._tick % self._bc_period == 0:
                cmds.append(broadcast(f"T:{tgt[0]:.5f},{tgt[1]:.5f}"))
            # destroy / timeout
            if self._dwell_time >= self._dwell_target or \
               self._track_t >= self._track_timeout:
                # if timed out WITHOUT destroying AND target is slow, it's a decoy
                if (self._dwell_time < self._dwell_target
                        and self._ema.value
                        and self._ema.speed_mps() < 2.5):
                    self._known_decoys.append(self._ema.value)
                self._state = self.SEARCH
                self._candidate = None
                self._ema.reset()
                self._dwell_time = 0.0
                slat, slon, pan, tilt = self._spiral()
                slat, slon = _clamp_to_safebox(slat, slon)
                cmds.append(fly_to(slat, slon, alt=self._search_alt, speed=22.0))
                cmds.append(point_gimbal(pan, tilt))
                cmds.append(set_gimbal_fov(self._search_fov))
                return cmds
            # track commands
            tlat, tlon = _clamp_to_safebox(*tgt)
            cmds.append(fly_to(tlat, tlon, alt=self._search_alt, speed=22.0,
                               loiter_radius=self._loiter_close))
            pan, tilt = self._tracking_gimbal(
                obs.self.lat, obs.self.lon, obs.self.heading_deg, tgt[0], tgt[1])
            cmds.append(point_gimbal(pan, tilt))
            cmds.append(set_gimbal_fov(self._track_fov))
            # report only if target is confirmed moving (speed > 3 m/s) —
            # reporting a decoy's position pollutes RMSE badly (600m+).
            if (sim_t - self._last_report_t >= 1.0
                    and self._ema.value is not None
                    and self._ema.speed_mps() > 3.0):
                self._last_report_t = sim_t
                cmds.append(report_target(self._ema.value[0], self._ema.value[1]))
            return cmds

        # fallback (shouldn't reach)
        slat, slon, pan, tilt = self._spiral()
        cmds.append(fly_to(slat, slon, alt=self._search_alt, speed=22.0))
        cmds.append(point_gimbal(pan, tilt))
        return cmds
