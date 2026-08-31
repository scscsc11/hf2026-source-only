"""
HF2026 V25 — V24 protocol baseline + longer first-lock rendezvous diagnostics

赛题三第一目标版：
    SEARCH -> ACQUIRE -> RENDEZVOUS -> DWELL

V25 keeps V24's protocol-safe V21-equivalent flight/search/SAM/anchor geometry.
The V24 evaluation proved task re-accept/R-D oscillation was fixed and that clean
3/3 ACK coalitions can form, but no task reached ever_all_visual or DWELL.  V25
therefore changes only the first-lock rendezvous timeout and adds follower-side
diagnostics; it does not change association gates, confirmation hit counts,
search, SAM routing, helper selection, 430 m role geometry, YOLO thresholds,
real/decoy classification, or report_target behaviour.

In V23, released/stale T packets could resurrect an old task.  More seriously,
the owner could receive its own relayed/rebroadcast T packet, adopt that stale
task again, and a follower could repeatedly release/re-accept the same task.
Every re-accept reset the local visual generation and bbox track.  The log
therefore contained TASK_ACCEPT at ~0.1 s intervals and repeated DWELL_ENTER
events for the same owner/seq; incoming stale phase=R packets also downgraded a
live local phase=D task back to R.  That explains why internal 3/3 agreement
could appear while the official evaluator still saw zero coop_ticks.

V25 retains V24 task protocol idempotence:
(1) an owner never adopts its own T packet; only its local task creator may
    create an owner task;
(2) same-task T packets update predictor state without recreating the Task;
(3) task phase is monotonic for one (owner,seq): D may be learned from the
    owner, but stale R packets can never downgrade D;
(4) D packets create a tombstone, so delayed T packets cannot resurrect a task
    that was explicitly released;
(5) ordinary follower timeout does not tombstone the task, allowing legitimate
    reconnection after a communication gap.

No search, SAM routing, helper selection, 430 m role geometry, peer avoidance,
YOLO thresholds, association gates, real/decoy classification, or report_target
behaviour is changed in this version.  The only behavioural change is the
20 s -> 34 s first-lock rendezvous timeout.

The V25 sensing architecture remains deliberately hybrid:
(1) the platform/default detector remains the source of noisy geographic
    detections and therefore preserves the proven TrackFilter / official
    evaluator path;
(2) obs.self.photo is processed asynchronously by the official
    target_vehicle_yolov8s.pt model whenever this UAV is verifying a candidate
    OR is a member of an active task;
(3) tiny vehicles are acquired with local tiled inference, then tracked inside
    one local ROI; the resulting bbox centre corrects the geographic gimbal
    line-of-sight;
(4) ACQUIRE retains 50 -> 35 -> 25 deg coarse-to-fine zoom, but active-task
    RENDEZVOUS / DWELL deliberately use only 50 <-> 35 deg for robustness;
(5) task visual loss immediately falls back to geographic LOS and widens FOV
    rather than aborting the coalition.

sensor() always returns None.  Therefore V25 never replaces the official
player-visible geographic detector with YOLO; YOLO remains an auxiliary
pixel-domain servo only.  No real/decoy hard classifier and no report_target
logic are introduced in this version.

Design summary
--------------
1) SEARCH
   * Preserve the natural 2x5 spawn structure instead of immediately
     reshuffling all 10 fixed-wing UAVs.
   * Each 5-UAV row stays as a ~460 m spaced "comb".  The row sweeps its own
     north/south half of the real terrain, then shifts east and sweeps back.
   * All UAVs remain pure searchers for the first 35 s.
   * Search gimbal uses an 8 s, +/-85 deg triangle-wave pan at -68 deg tilt,
     FOV=50 deg.  The requested pan rate is ~42.5 deg/s, below the 60 deg/s
     platform limit.  Because the optical axis is off-nadir, pan now changes
     the ground footprint instead of merely rotating a nadir image.

2) ACQUIRE / VISUAL SERVO
   * A first geographic hit does not recruit anybody.
   * The search flight path remains unchanged; geographic TrackFilter output
     provides the coarse gimbal line-of-sight.
   * If obs.self.photo exists, a shared asynchronous YOLO worker acquires the
     tiny vehicle with local tiles, then follows one ROI around the last bbox.
   * Bbox-centre error is applied as a bounded correction to geographic LOS.
   * FOV starts at 50 deg.  It may narrow to 35 and then 25 deg only after
     repeated centred visual hits.  Visual loss widens the FOV again.
   * The visual path is optional: no photo / no YOLO / timeout -> V18 fallback.

3) RENDEZVOUS
   * No hard-coded fallback triad.
   * The owner chooses TWO recently-heard, alive/idle peers that are genuinely
     within the radio margin according to fresh heartbeat positions.
   * Both followers must actually ACK.  No two ACKs -> no 3-UAV task.
   * Each member must then report repeated local visual evidence compatible
     with the SAME predicted target before the owner enters DWELL.

4) DWELL
   * The three fixed-wing UAVs use three distinct observation anchors around
     the moving target; ideal anchor separation is ~745 m.
   * Flight-center updates are deliberately slow; gimbal updates remain fast.
     Camera continuity is therefore prioritised over chasing noisy slots.
   * Short detection losses use the target velocity predictor rather than
     freezing the last noisy point.
   * FOV stays at 50 deg for the first K=3 proof.

5) Threats / comms
   * Horizontal avoidance of briefing SAM approximate boxes only.  Altitude
     remains 500 m, matching the current handbook.
   * Dynamic jam never automatically cancels a visual task: once three UAVs
     have the target locally, they can continue visually through comm outages.
   * Broadcast rate is kept below the 4 Hz platform cap.

6) Scoring
   * report_target remains intentionally disabled in V24.
   * First test KPI: any official per-target coop_ticks > 0.
   * Next KPI: dwell_accumulated_s > 0, then n_destroyed >= 1.

Participant path:
competition/user_algorithms/adversarial_swarm/hf2026_v25.py
"""

from __future__ import annotations

import heapq
import itertools
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from competition.sdk.core.commands import (
    Command,
    broadcast,
    fly_to,
    point_gimbal,
    set_gimbal_fov,
)
from competition.sdk.scenarios.adversarial_swarm import SwarmAgent
from competition.sdk.scenarios.adversarial_swarm.observation import SwarmObs


# ---------------------------------------------------------------------------
# Official terrain / platform constants
# ---------------------------------------------------------------------------

LAT_MIN = 26.98180556
LAT_MAX = 27.02500868
LON_MIN = 124.98000000
LON_MAX = 125.02031250

CENTER_LAT = 0.5 * (LAT_MIN + LAT_MAX)
CENTER_LON = 0.5 * (LON_MIN + LON_MAX)

M_PER_DEG_LAT = 111_320.0

SEARCH_ALT_M = 500.0
SEARCH_SPEED_MPS = 38.0
TRACK_SPEED_MPS = 27.0

SEARCH_FOV_DEG = 50.0
TRACK_FOV_DEG = 50.0

SEARCH_WARMUP_S = 8.0

# V18 active-search sensing.  A full -85 -> +85 -> -85 cycle takes 8 s:
# 170 deg / 4 s = 42.5 deg/s, safely below the scenario pan-rate limit.
SEARCH_SCAN_PERIOD_S = 8.0
SEARCH_SCAN_PAN_DEG = 85.0
SEARCH_SCAN_TILT_DEG = -68.0

# Follower same-target confirmation before contributing a V (visual) message.
# With a nominal 10 Hz control loop and ~75% Rain detection probability,
# five compatible hits normally take well under one second, but a one-frame
# neighbour switch cannot create a false three-UAV internal lock.
FOLLOWER_CONFIRM_HITS = 3
FOLLOWER_CONFIRM_SPAN_S = 0.22
FOLLOWER_CONFIRM_GAP_S = 1.25

# ---------------------------------------------------------------------------
# V19 optional camera-side visual servo
# ---------------------------------------------------------------------------
#
# The official model has shown useful responses on selected task-3 UE frames,
# but vehicles at 500 m are often only ~15-20 px wide.  Whole-frame inference
# is therefore not trusted as the only acquisition path.  A candidate with a
# camera gets a low-rate tiled acquisition; once a bbox is found, one local ROI
# is used for the next frame.  All inference runs in ONE shared background
# thread so the ~10 Hz flight-control loop is never blocked by PyTorch.
VISION_ENABLED = True
VISION_MODEL_REL = ("examples", "yolotrack", "target_vehicle_yolov8s.pt")
VISION_IMGSZ = 1024
VISION_CONF = 0.03

VISION_TILE_PX = 320
VISION_TILE_OVERLAP_PX = 80
VISION_ROI_PX = 320

# Do not flood the single shared worker when several camera-equipped UAVs
# simultaneously have candidates.
VISION_SUBMIT_INTERVAL_S = 0.55
VISION_RESULT_FRESH_WALL_S = 1.35
VISION_PRIOR_FRESH_WALL_S = 1.20

# Visual track / zoom logic.
VISION_ASSOC_PX = 210.0
VISION_SERVO_GAIN = 0.55
VISION_SERVO_MAX_PAN_DEG = 16.0
VISION_SERVO_MAX_TILT_DEG = 12.0

VISION_FOV_WIDE = 50.0
VISION_FOV_MID = 35.0
VISION_FOV_NARROW = 25.0
VISION_WIDE_TO_MID_ERR = 0.30
VISION_MID_TO_NARROW_ERR = 0.18
VISION_CENTER_HOLD_S = 0.55
VISION_FOV_CMD_INTERVAL_S = 0.75

# V20 active-task visual policy.  During the official K=3 dwell, robustness
# matters more than appearance detail, so task members never use 25 deg FOV.
VISION_TASK_FOV_WIDE = 50.0
VISION_TASK_FOV_MID = 35.0
VISION_TASK_CENTER_ERR = 0.28
VISION_TASK_WIDEN_ERR = 0.52
VISION_TASK_CENTER_HOLD_S = 0.65
VISION_TASK_LOSS_WIDEN_S = 0.70

# With a fresh prior bbox, first try one cheap local ROI.  Do not immediately
# fall back to a full tiled scan on the same frame; allow tiled reacquisition
# only after a short sequence of ROI misses.
VISION_TILED_REACQUIRE_MISSES = 2

# Real comm cap is ~1000 m.  Keep margin for heartbeat age / motion.
DIRECT_PEER_MAX_M = 970.0
PEER_FRESH_S = 3.0

# V25 stale-packet immunity.  Sequence numbers are per owner; once an explicit
# D message retires (owner,seq), no delayed T for that exact key may recreate
# the task during this window.
TASK_TOMBSTONE_S = 90.0

# V25: V24 logs showed clean 3/3 ACK coalitions whose followers sometimes
# needed just over 20 s to build three geographically compatible local hits.
# Do not dissolve a never-locked coalition at the old 20 s mark; give the
# already-recruited three-aircraft geometry a bounded extra window first.
FIRST_LOCK_RENDEZVOUS_TIMEOUT_S = 34.0

# Sparse follower-side diagnostics only; no control/association behaviour.
FOLLOWER_STATUS_INTERVAL_S = 2.0

# V21 candidate / task confirmation tuning.  The default Rain detector is
# noisy and intermittent; these gates still require temporal evidence but no
# longer waste four-plus seconds before even attempting coalition creation.
CANDIDATE_NO_CAMERA_MIN_S = 1.6
CANDIDATE_CAMERA_MIN_S = 2.6
CANDIDATE_NO_CAMERA_HITS = 4
CANDIDATE_CAMERA_HITS = 5
CANDIDATE_FRESH_S = 1.10
CANDIDATE_MAX_AGE_S = 10.0

# Once a follower has at least one geographically compatible observation, a
# fresh, centred bbox may bridge a short Rain geo dropout.  Visual evidence is
# never allowed to bootstrap a follower from zero geo association.
TASK_VISUAL_BRIDGE_GEO_AGE_S = 2.5
TASK_VISUAL_BRIDGE_CENTER_ERR = 0.42
TASK_VISUAL_BRIDGE_MIN_HITS = 2

# Search / rendezvous deconfliction.  Official proximity penalty starts below
# 200 m; begin a smooth lateral escape substantially earlier.
PEER_AVOID_TRIGGER_M = 310.0
PEER_AVOID_LOOKAHEAD_M = 520.0

# Camera-track association gates.  Player-visible position noise is large in
# Rain, but the task gate is deliberately tighter than V13.
CANDIDATE_GATE_M = 240.0
TASK_GATE_M = 185.0
VISUAL_GATE_M = 190.0

# Tracking geometry: three separate observation anchors around one target.
# A 430 m radius gives ~745 m between ideal anchor centres (well below the
# 1000 m radio cap, but far above the 200 m proximity penalty threshold).
TRACK_ANCHOR_RADIUS_M = 430.0
TRACK_ANCHOR_BEARINGS_DEG = (0.0, 120.0, 240.0)
TRACK_LOITER_RADIUS_M = 165.0

# Static-threat route planning.  The supplied approximate SAM bbox is already
# larger than the true polygon; V15 adds another horizontal buffer because a
# fixed-wing aircraft cuts corners during turns.
SAM_ROUTE_PAD_M = 260.0
SAM_CORNER_CLEARANCE_M = 90.0
ROUTE_SWITCH_M = 185.0

# V17: search-only soft lane preservation in the visibility graph.
# 0.16 means a 500 m lateral deviation costs an extra 80 m of path length:
# enough to break equal-cost "everyone uses the same corner" funnels without
# making an actually much shorter safe detour unattractive.
SEARCH_CORRIDOR_BIAS = 0.16


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LAT * max(0.1, math.cos(math.radians(lat)))


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    ref = 0.5 * (lat1 + lat2)
    east = (lon2 - lon1) * _m_per_deg_lon(ref)
    north = (lat2 - lat1) * M_PER_DEG_LAT
    return math.hypot(east, north)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    ref = 0.5 * (lat1 + lat2)
    east = (lon2 - lon1) * _m_per_deg_lon(ref)
    north = (lat2 - lat1) * M_PER_DEG_LAT
    return (math.degrees(math.atan2(east, north)) + 360.0) % 360.0


def _wrap180(x: float) -> float:
    return (x + 180.0) % 360.0 - 180.0


def _offset(
    lat: float,
    lon: float,
    east_m: float,
    north_m: float,
) -> Tuple[float, float]:
    return (
        lat + north_m / M_PER_DEG_LAT,
        lon + east_m / _m_per_deg_lon(lat),
    )


def _uav_index(uid: str) -> int:
    """Map ids such as 20001 / UAV01 / uav_01 to 1..10."""
    nums = re.findall(r"\d+", str(uid))
    if not nums:
        return 1
    n = int(nums[-1])
    if 20001 <= n <= 20010:
        return n - 20000
    q = n % 100
    if 1 <= q <= 10:
        return q
    q = n % 1000
    if 1 <= q <= 10:
        return q
    if 1 <= n <= 10:
        return n
    return ((n - 1) % 10) + 1


def _row_of(idx: int) -> int:
    return 0 if idx <= 5 else 1


def _col_of(idx: int) -> int:
    return (idx - 1) % 5


def _geo_to_int(lat: float, lon: float) -> Tuple[int, int]:
    return int(round(lat * 100000.0)), int(round(lon * 100000.0))


def _int_to_geo(lat_i: str, lon_i: str) -> Tuple[float, float]:
    return int(lat_i) / 100000.0, int(lon_i) / 100000.0


def _point_in_rect(
    p: Tuple[float, float],
    r: Tuple[float, float, float, float],
) -> bool:
    lat, lon = p
    lat0, lat1, lon0, lon1 = r
    return lat0 <= lat <= lat1 and lon0 <= lon <= lon1


def _dist_point_rect_m(
    lat: float,
    lon: float,
    r: Tuple[float, float, float, float],
) -> float:
    lat0, lat1, lon0, lon1 = r
    qlat = min(lat1, max(lat0, lat))
    qlon = min(lon1, max(lon0, lon))
    return _dist_m(lat, lon, qlat, qlon)


def _segment_intersects_rect(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    r: Tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky segment / axis-aligned rectangle test."""
    if _point_in_rect(p0, r) or _point_in_rect(p1, r):
        return True

    lat0, lon0 = p0
    lat1, lon1 = p1
    rlat0, rlat1, rlon0, rlon1 = r

    x0, y0 = lon0, lat0
    x1, y1 = lon1, lat1
    dx = x1 - x0
    dy = y1 - y0

    p = (-dx, dx, -dy, dy)
    q = (
        x0 - rlon0,
        rlon1 - x0,
        y0 - rlat0,
        rlat1 - y0,
    )

    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) < 1e-15:
            if qi < 0.0:
                return False
            continue

        t = qi / pi
        if pi < 0.0:
            if t > u2:
                return False
            u1 = max(u1, t)
        else:
            if t < u1:
                return False
            u2 = min(u2, t)

    return True


# ---------------------------------------------------------------------------
# State models
# ---------------------------------------------------------------------------

@dataclass
class Peer:
    idx: int
    lat: float
    lon: float
    state: str
    last_seen: float


@dataclass
class TrackFilter:
    lat: float
    lon: float
    last_t: float
    last_seen: float
    v_east: float = 0.0
    v_north: float = 0.0
    hits: int = 1
    history: List[Tuple[float, float, float]] = field(default_factory=list)

    def predict_to(self, t: float) -> None:
        dt = max(0.0, min(2.0, t - self.last_t))
        if dt <= 0.0:
            return
        self.lat += self.v_north * dt / M_PER_DEG_LAT
        self.lon += self.v_east * dt / _m_per_deg_lon(self.lat)
        self.last_t = t

    def correct(self, t: float, lat: float, lon: float, alpha: float = 0.38) -> None:
        self.predict_to(t)

        # Position correction.
        ref = 0.5 * (self.lat + lat)
        err_e = (lon - self.lon) * _m_per_deg_lon(ref)
        err_n = (lat - self.lat) * M_PER_DEG_LAT

        self.lat += alpha * err_n / M_PER_DEG_LAT
        self.lon += alpha * err_e / _m_per_deg_lon(self.lat)

        self.last_seen = t
        self.hits += 1
        self.history.append((t, lat, lon))
        self.history = [s for s in self.history if t - s[0] <= 6.0]

        # Robust-ish least-squares velocity estimate over a multi-second
        # window.  This is much less noisy than differentiating 10 Hz points.
        if len(self.history) >= 5:
            t0 = self.history[0][0]
            span = self.history[-1][0] - t0
            if span >= 1.6:
                mean_t = sum(s[0] - t0 for s in self.history) / len(self.history)
                ref_lat = sum(s[1] for s in self.history) / len(self.history)
                ref_lon = sum(s[2] for s in self.history) / len(self.history)
                m_lon = _m_per_deg_lon(ref_lat)

                num_e = num_n = den = 0.0
                for ts, la, lo in self.history:
                    x = (ts - t0) - mean_t
                    e = (lo - ref_lon) * m_lon
                    n = (la - ref_lat) * M_PER_DEG_LAT
                    num_e += x * e
                    num_n += x * n
                    den += x * x

                if den > 1e-6:
                    est_e = num_e / den
                    est_n = num_n / den
                    speed = math.hypot(est_e, est_n)

                    # Ground vehicles in the supplied scenario are slow.
                    # Clip occasional 70 m noise spikes before blending.
                    if speed > 12.0:
                        k = 12.0 / speed
                        est_e *= k
                        est_n *= k

                    beta = 0.22
                    self.v_east = (1.0 - beta) * self.v_east + beta * est_e
                    self.v_north = (1.0 - beta) * self.v_north + beta * est_n


@dataclass
class Candidate:
    filt: TrackFilter
    started_at: float


@dataclass
class Task:
    owner: int
    seq: int
    members: Tuple[int, int, int]
    filt: TrackFilter
    created_at: float
    last_update: float
    phase: str = "R"  # R=rendezvous, D=dwell
    acks: Dict[int, float] = field(default_factory=dict)
    visuals: Dict[int, float] = field(default_factory=dict)
    all_visual_since: Optional[float] = None
    last_all_visual: float = -999.0
    ever_all_visual: bool = False
    proxy_dwell: float = 0.0
    n_destroyed_start: int = 0
    last_local_seen: float = -999.0

    # V18: local temporal association state (each Agent instance owns its own
    # Task object, so these fields are local to that UAV).
    local_match_hits: int = 0
    local_match_first: float = -999.0
    local_match_last: float = -999.0
    local_visual_seq: int = -1
    local_confirmed: bool = False


@dataclass
class Cooldown:
    lat: float
    lon: float
    expires_at: float


# ---------------------------------------------------------------------------
# Shared asynchronous visual runtime
# ---------------------------------------------------------------------------

@dataclass
class _VisionJob:
    uid: str
    generation: int
    sim_t: float
    fov_deg: float
    photo: bytes
    prior_center: Optional[Tuple[float, float]] = None
    allow_tiled: bool = True


@dataclass
class _VisionResult:
    seq: int
    uid: str
    generation: int
    sim_t: float
    wall_t: float
    fov_deg: float
    found: bool
    image_w: int = 0
    image_h: int = 0
    bbox: Optional[Tuple[float, float, float, float]] = None
    confidence: float = 0.0


@dataclass
class _VisualTrack:
    result_seq: int = -1
    bbox: Optional[Tuple[float, float, float, float]] = None
    confidence: float = 0.0
    image_w: int = 0
    image_h: int = 0
    frame_fov_deg: float = VISION_FOV_WIDE
    last_seen_wall: float = -999.0
    last_result_wall: float = -999.0
    hits: int = 0
    misses: int = 0
    center_error: float = 999.0

    @property
    def center(self) -> Optional[Tuple[float, float]]:
        if self.bbox is None:
            return None
        x1, y1, x2, y2 = self.bbox
        return 0.5 * (x1 + x2), 0.5 * (y1 + y2)


class _SharedVisionRuntime:
    """One lazy YOLO model + one worker thread shared by all ten Agent objects.

    The participant contract gives each Agent its own photo bytes through
    obs.self.photo.  Jobs submitted here contain only those player-visible
    bytes and per-agent local state; this helper never reads Redis or truth.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: Dict[str, _VisionJob] = {}
        self._results: Dict[str, _VisionResult] = {}
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._model = None
        self._cv2 = None
        self._np = None
        self._error: Optional[str] = None
        self._seq = 0

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def available(self) -> bool:
        return self._error is None

    def _model_path(self) -> Path:
        override = os.environ.get("HF2026_YOLO_MODEL", "").strip()
        if override:
            return Path(override).expanduser().resolve()

        here = Path(__file__).resolve()
        # .../competition/user_algorithms/adversarial_swarm/hf2026_v25.py
        repo = here.parents[3]
        return repo.joinpath(*VISION_MODEL_REL)

    def _ensure_started(self) -> None:
        with self._cv:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name="hf2026-v25-yolo",
                daemon=True,
            )
            self._thread.start()

    def submit(self, job: _VisionJob) -> bool:
        if not VISION_ENABLED or self._error is not None:
            return False
        self._ensure_started()
        with self._cv:
            # Keep only the newest unprocessed frame for each UAV.
            self._pending[job.uid] = job
            self._cv.notify()
        return True

    def latest(self, uid: str) -> Optional[_VisionResult]:
        with self._cv:
            return self._results.get(str(uid))

    def _publish(self, r: _VisionResult) -> None:
        with self._cv:
            self._seq += 1
            r.seq = self._seq
            self._results[r.uid] = r

    def _run(self) -> None:
        try:
            import cv2
            import numpy as np
            from ultralytics import YOLO

            model_path = self._model_path()
            if not model_path.exists():
                raise FileNotFoundError(f"YOLO model not found: {model_path}")

            self._cv2 = cv2
            self._np = np
            self._model = YOLO(str(model_path))
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            return

        while not self._stop:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait(timeout=0.5)
                if self._stop:
                    return
                # FIFO-ish across UAVs: dict insertion order, but replacement
                # guarantees stale frames do not build a queue.
                uid = next(iter(self._pending))
                job = self._pending.pop(uid)

            try:
                result = self._infer(job)
            except Exception:
                result = _VisionResult(
                    seq=0,
                    uid=job.uid,
                    generation=job.generation,
                    sim_t=job.sim_t,
                    wall_t=time.monotonic(),
                    fov_deg=job.fov_deg,
                    found=False,
                )
            self._publish(result)

    @staticmethod
    def _starts(length: int, tile: int, overlap: int) -> List[int]:
        if length <= tile:
            return [0]
        step = max(1, tile - overlap)
        xs = list(range(0, max(1, length - tile + 1), step))
        last = length - tile
        if xs[-1] != last:
            xs.append(last)
        return xs

    @staticmethod
    def _clip_roi(
        cx: float,
        cy: float,
        size: int,
        W: int,
        H: int,
    ) -> Tuple[int, int, int, int]:
        size = min(size, W, H)
        x0 = int(round(cx - size / 2))
        y0 = int(round(cy - size / 2))
        x0 = max(0, min(W - size, x0))
        y0 = max(0, min(H - size, y0))
        return x0, y0, x0 + size, y0 + size

    def _infer_batch(
        self,
        crops,
        offsets: List[Tuple[int, int]],
        W: int,
        H: int,
        prior_center: Optional[Tuple[float, float]],
    ) -> Optional[Tuple[Tuple[float, float, float, float], float]]:
        if not crops:
            return None

        results = self._model.predict(
            source=crops,
            imgsz=VISION_IMGSZ,
            conf=VISION_CONF,
            verbose=False,
        )

        candidates = []
        for res, (ox, oy) in zip(results, offsets):
            boxes = getattr(res, "boxes", None)
            if boxes is None:
                continue

            for i in range(len(boxes)):
                b = boxes[i]
                try:
                    cls_id = int(b.cls[0])
                except Exception:
                    cls_id = 0
                # Official weight currently has one TargetVehicle class.
                if cls_id != 0:
                    continue

                x1, y1, x2, y2 = (
                    b.xyxy[0].detach().cpu().numpy().tolist()
                )
                conf = float(b.conf[0])
                gx1 = float(x1 + ox)
                gy1 = float(y1 + oy)
                gx2 = float(x2 + ox)
                gy2 = float(y2 + oy)
                cx = 0.5 * (gx1 + gx2)
                cy = 0.5 * (gy1 + gy2)

                if prior_center is not None:
                    d = math.hypot(
                        cx - prior_center[0],
                        cy - prior_center[1],
                    )
                    continuity = max(0.0, 1.0 - d / 220.0)
                    score = conf + 0.35 * continuity
                else:
                    dx = (cx - W / 2.0) / max(1.0, W / 2.0)
                    dy = (cy - H / 2.0) / max(1.0, H / 2.0)
                    centre = max(0.0, 1.0 - math.hypot(dx, dy) / 1.35)
                    # Confidence dominates, but geographic coarse pointing
                    # makes an image-central detection slightly preferable.
                    score = conf + 0.12 * centre

                candidates.append(
                    (score, conf, (gx1, gy1, gx2, gy2), cx, cy)
                )

        if not candidates:
            return None

        # Suppression is unnecessary for the servo: just take the best
        # temporally/centrally compatible TargetVehicle candidate.
        candidates.sort(key=lambda z: z[0], reverse=True)
        _, conf, bbox, _, _ = candidates[0]
        return bbox, conf

    def _infer(self, job: _VisionJob) -> _VisionResult:
        arr = self._np.frombuffer(job.photo, dtype=self._np.uint8)
        img = self._cv2.imdecode(arr, self._cv2.IMREAD_COLOR)
        if img is None:
            return _VisionResult(
                seq=0, uid=job.uid, generation=job.generation,
                sim_t=job.sim_t, wall_t=time.monotonic(),
                fov_deg=job.fov_deg, found=False,
            )

        H, W = img.shape[:2]
        det = None

        # Fast path: once visually locked, search only one local ROI.
        if job.prior_center is not None:
            x0, y0, x1, y1 = self._clip_roi(
                job.prior_center[0],
                job.prior_center[1],
                VISION_ROI_PX,
                W,
                H,
            )
            crop = img[y0:y1, x0:x1]
            det = self._infer_batch(
                [crop],
                [(x0, y0)],
                W,
                H,
                job.prior_center,
            )

        # Acquisition / reacquisition: tiny-target tiled scan.  This is the
        # online counterpart of the offline test where obvious FOV25 vehicles
        # went from whole-frame misses to strong tiled detections.
        if det is None and job.allow_tiled:
            xs = self._starts(W, VISION_TILE_PX, VISION_TILE_OVERLAP_PX)
            ys = self._starts(H, VISION_TILE_PX, VISION_TILE_OVERLAP_PX)
            crops = []
            offsets = []
            for y0 in ys:
                for x0 in xs:
                    crops.append(
                        img[y0:y0 + VISION_TILE_PX,
                            x0:x0 + VISION_TILE_PX]
                    )
                    offsets.append((x0, y0))
            det = self._infer_batch(
                crops,
                offsets,
                W,
                H,
                job.prior_center,
            )

        if det is None:
            return _VisionResult(
                seq=0, uid=job.uid, generation=job.generation,
                sim_t=job.sim_t, wall_t=time.monotonic(),
                fov_deg=job.fov_deg, found=False,
                image_w=W, image_h=H,
            )

        bbox, conf = det
        return _VisionResult(
            seq=0,
            uid=job.uid,
            generation=job.generation,
            sim_t=job.sim_t,
            wall_t=time.monotonic(),
            fov_deg=job.fov_deg,
            found=True,
            image_w=W,
            image_h=H,
            bbox=bbox,
            confidence=conf,
        )


_VISION_RUNTIME: Optional[_SharedVisionRuntime] = None
_VISION_RUNTIME_LOCK = threading.Lock()


def _get_vision_runtime() -> _SharedVisionRuntime:
    global _VISION_RUNTIME
    with _VISION_RUNTIME_LOCK:
        if _VISION_RUNTIME is None:
            _VISION_RUNTIME = _SharedVisionRuntime()
        return _VISION_RUNTIME


# ---------------------------------------------------------------------------
# V21 Agent
# ---------------------------------------------------------------------------

class HF2026V25Agent(SwarmAgent):
    """V24: V23 control + stale-packet-safe task protocol."""

    SEARCH = "S"
    ACQUIRE = "A"
    RENDEZVOUS = "R"
    DWELL = "D"

    def configure(self, config) -> None:
        self._idx = _uav_index(self.my_uid)
        self._row = _row_of(self._idx)
        self._col = _col_of(self._idx)

        self._t = 0.0
        self._state = self.SEARCH

        self._candidate: Optional[Candidate] = None
        self._task: Optional[Task] = None
        self._task_seq = 0

        self._peers: Dict[int, Peer] = {}
        self._known_tasks: Dict[Tuple[int, int], Tuple[float, float, float]] = {}
        self._cooldowns: List[Cooldown] = []

        self._n_destroyed = 0

        # Search formation / waypoint state.
        self._search_waypoints = self._build_search_waypoints()
        self._search_wp = 0

        # Persistent safe-route state.  V14 recomputed one temporary SAM
        # corner every tick; V15 plans and follows an entire multi-segment
        # route so the airframe cannot oscillate between corners.
        self._route: List[Tuple[float, float]] = []
        self._route_i = 0
        self._route_goal: Optional[Tuple[float, float]] = None
        self._route_mode = ""
        self._route_sig: Tuple = ()
        self._route_planned_t = -999.0

        # Persistent navigation command state.
        self._last_nav_t = -999.0
        self._last_nav_goal: Optional[Tuple[float, float]] = None
        self._last_nav_mode = ""

        # Comms.
        self._outbox: List[Tuple[int, str]] = []  # (priority, payload)
        self._next_tx = 0.0
        self._last_hb = -999.0
        self._last_task_tx = -999.0
        self._last_ack_tx = -999.0
        self._last_visual_tx = -999.0

        # Camera command throttling.
        self._last_fov_t = -999.0

        # V21 local visual-servo state.  The heavy model is shared globally;
        # these fields remain strictly per-UAV.
        self._vision = _VisualTrack()
        self._vision_generation = 0
        self._vision_last_submit_wall = -999.0
        self._vision_last_photo_wall = -999.0
        self._vision_last_photo_sig = None
        self._vision_stage = 0  # 0=50deg, 1=35deg, 2=25deg
        self._vision_center_since: Optional[float] = None
        self._vision_last_stage_t = -999.0

        # Sparse transition diagnostics for short web runs.
        self._last_nohelper_log_t = -999.0
        self._last_local_confirm_log_task: Optional[Tuple[int, int]] = None
        self._last_phase_log_task: Optional[Tuple[int, int, str]] = None

        # V25 protocol state / diagnostics.
        self._last_sam_inside_log_t = -999.0
        self._last_task_status_log_t = -999.0
        self._retired_tasks: Dict[Tuple[int, int], float] = {}
        self._last_stale_task_log_t = -999.0
        self._last_follower_status_log_t = -999.0

    def reset(self) -> None:
        self.configure(None)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def sensor(self, obs: SwarmObs, dt: float):
        """Auxiliary pixel-domain sensing; official geographic sensing stays on.

        The runner calls sensor() before decide().  Returning None deliberately
        requests the platform default detector, while the asynchronous YOLO
        result is stored only in self._vision for gimbal servoing.
        """
        if not VISION_ENABLED:
            return None

        now = time.monotonic()
        photo = getattr(obs.self, "photo", None)
        if isinstance(photo, (bytes, bytearray)) and len(photo) > 100:
            self._vision_last_photo_wall = now

        # Consume any result completed since the previous cycle.
        rt = _get_vision_runtime()
        r = rt.latest(str(self.my_uid))
        if (
            r is not None
            and r.seq != self._vision.result_seq
            and r.generation == self._vision_generation
        ):
            self._accept_vision_result(r)

        # V21 spends pixel-domain compute only while this UAV is verifying a
        # candidate OR is one of the three members of an active task.  This is
        # the key V20 change: visual servo remains alive through RENDEZVOUS and
        # the full K=3 DWELL instead of stopping at coalition creation.
        visual_context_active = (
            self._candidate is not None
            or self._task is not None
        )
        if (
            not visual_context_active
            or not isinstance(photo, (bytes, bytearray))
            or len(photo) <= 100
            or rt.error is not None
        ):
            return None

        if now - self._vision_last_submit_wall < VISION_SUBMIT_INTERVAL_S:
            return None

        # Avoid repeatedly submitting exactly the same cached frame.
        # PNG/JPEG tails change with new camera frames; this tiny signature is
        # far cheaper than hashing a multi-megabyte image.
        b = bytes(photo)
        sig = (
            len(b),
            b[:12],
            b[-24:],
        )
        if sig == self._vision_last_photo_sig:
            return None
        self._vision_last_photo_sig = sig

        prior = None
        if (
            self._vision.center is not None
            and now - self._vision.last_seen_wall <= VISION_PRIOR_FRESH_WALL_S
        ):
            prior = self._vision.center

        # ROI-first policy: with a recent bbox, try one local ROI cheaply.
        # Tiled reacquisition is enabled only after a few ROI misses.  With no
        # prior bbox at all, tiled acquisition is necessary immediately.
        allow_tiled = (
            prior is None
            or self._vision.misses >= VISION_TILED_REACQUIRE_MISSES
        )

        fov = float(
            getattr(obs.self, "gimbal_fov_deg", VISION_FOV_WIDE)
            or VISION_FOV_WIDE
        )
        ok = rt.submit(
            _VisionJob(
                uid=str(self.my_uid),
                generation=self._vision_generation,
                sim_t=self._t + max(0.0, float(dt)),
                fov_deg=fov,
                photo=b,
                prior_center=prior,
                allow_tiled=allow_tiled,
            )
        )
        if ok:
            self._vision_last_submit_wall = now

        # Keep the default AccuracySimulator / platform detector active.
        return None

    def _accept_vision_result(self, r: _VisionResult) -> None:
        v = self._vision
        v.result_seq = r.seq
        v.last_result_wall = r.wall_t

        if not r.found or r.bbox is None:
            v.misses += 1
            return

        new_cx = 0.5 * (r.bbox[0] + r.bbox[2])
        new_cy = 0.5 * (r.bbox[1] + r.bbox[3])

        old = v.center
        if old is not None:
            jump = math.hypot(new_cx - old[0], new_cy - old[1])
        else:
            jump = 0.0

        if old is not None and jump > VISION_ASSOC_PX:
            # A large jump may be another nearby vehicle / false positive.
            # Re-seed cautiously instead of declaring a long visual streak.
            v.hits = 1
        else:
            v.hits += 1

        v.bbox = r.bbox
        v.confidence = r.confidence
        v.image_w = r.image_w
        v.image_h = r.image_h
        v.frame_fov_deg = r.fov_deg
        v.last_seen_wall = r.wall_t
        v.misses = 0

        if r.image_w > 0 and r.image_h > 0:
            dx = (new_cx - r.image_w / 2.0) / max(1.0, r.image_w / 2.0)
            dy = (new_cy - r.image_h / 2.0) / max(1.0, r.image_h / 2.0)
            v.center_error = math.hypot(dx, dy)

    def _visual_fresh(self) -> bool:
        return (
            self._vision.bbox is not None
            and time.monotonic() - self._vision.last_seen_wall
            <= VISION_RESULT_FRESH_WALL_S
        )

    def _camera_recent(self) -> bool:
        return (
            time.monotonic() - self._vision_last_photo_wall
            <= 2.0
        )

    def _reset_visual_context(self) -> None:
        self._vision_generation += 1
        self._vision = _VisualTrack()
        self._vision_last_submit_wall = -999.0
        self._vision_last_photo_sig = None
        self._vision_stage = 0
        self._vision_center_since = None
        self._vision_last_stage_t = self._t

    def decide(self, obs: SwarmObs, dt: float) -> List[Command]:
        dt = max(0.0, float(dt))
        self._t += dt

        self._update_score(obs)
        self._ingest_messages(obs)
        self._prune()

        # Association must be state-aware.  With a multi-detection sensor,
        # choosing the globally highest-confidence vehicle can make a nearby
        # second car steal an established task.  Once a candidate/task exists,
        # select the detection nearest its prediction instead.
        if self._task is not None:
            det = self._best_detection(
                obs,
                (self._task.filt.lat, self._task.filt.lon),
            )
        elif self._candidate is not None:
            det = self._best_detection(
                obs,
                (self._candidate.filt.lat, self._candidate.filt.lon),
            )
        else:
            det = self._best_detection(obs)

        if self._task is not None:
            self._task.filt.predict_to(self._t)
            self._update_task_local(det)

            if self._task.phase == "D":
                self._state = self.DWELL
            else:
                self._state = self.RENDEZVOUS

            cmds = self._track_commands(obs)
            self._log_follower_status(obs, det)
            self._task_periodic(obs)
            self._task_state_machine(obs, dt)

        elif self._candidate is not None:
            self._state = self.ACQUIRE
            self._candidate.filt.predict_to(self._t)
            self._update_candidate(det)

            if self._candidate is not None and self._candidate_ready():
                self._try_start_task(obs)

            if self._task is not None:
                self._state = self.RENDEZVOUS
                cmds = self._track_commands(obs)
                self._task_periodic(obs, force=True)
            elif self._candidate is not None:
                cmds = self._acquire_commands(obs)
            else:
                self._state = self.SEARCH
                cmds = self._search_commands(obs)

        else:
            self._state = self.SEARCH

            if (
                self._t >= SEARCH_WARMUP_S
                and det is not None
                and not self._on_cooldown(det[0], det[1])
                and not self._near_known_task(det[0], det[1])
            ):
                self._new_candidate(det[0], det[1])
                self._state = self.ACQUIRE
                cmds = self._acquire_commands(obs)
            else:
                cmds = self._search_commands(obs)

        self._queue_heartbeat(obs)
        self._flush(cmds, obs)
        return cmds

    # ------------------------------------------------------------------
    # Search: preserve the natural 2x5 row formation
    # ------------------------------------------------------------------

    def _build_search_waypoints(self) -> List[Tuple[float, float]]:
        """Three progressive coverage passes for this 5-UAV row.

        V20 repeated one four-corner rectangle forever.  V21 preserves the
        ~460 m connected comb, but after each west/east pair of vertical
        sweeps the whole comb is shifted laterally.  Across a 600 s run this
        fills the gaps between the first-pass tracks instead of drawing the
        same rectangle again.  Every member of a row uses the same pass phase,
        so radio geometry is preserved.
        """
        width_m = (LON_MAX - LON_MIN) * _m_per_deg_lon(CENTER_LAT)
        west_margin = 260.0
        east_margin = 260.0
        spacing = 460.0

        used = 4.0 * spacing
        shift_east = max(
            900.0,
            width_m - west_margin - east_margin - used,
        )

        base_x0 = west_margin + self._col * spacing
        base_x1 = base_x0 + shift_east

        mid = 0.5 * (LAT_MIN + LAT_MAX)
        edge_m = 240.0
        mid_gap_m = 80.0
        if self._row == 0:
            south = LAT_MIN + edge_m / M_PER_DEG_LAT
            north = mid - mid_gap_m / M_PER_DEG_LAT
        else:
            south = mid + mid_gap_m / M_PER_DEG_LAT
            north = LAT_MAX - edge_m / M_PER_DEG_LAT

        # Pass 0 = original proven comb.  Passes 1/2 shift the two vertical
        # sweep families in opposite directions, filling first-pass gaps while
        # keeping adjacent aircraft exactly one spacing apart.
        pass_shifts = (
            (0.0, 0.0),
            (0.50 * spacing, -0.50 * spacing),
            (-0.25 * spacing, 0.25 * spacing),
        )

        points: List[Tuple[float, float]] = []
        for dx0, dx1 in pass_shifts:
            x0 = min(width_m - east_margin, max(west_margin, base_x0 + dx0))
            x1 = min(width_m - east_margin, max(west_margin, base_x1 + dx1))
            lon0 = LON_MIN + x0 / _m_per_deg_lon(CENTER_LAT)
            lon1 = LON_MIN + x1 / _m_per_deg_lon(CENTER_LAT)

            points.extend([
                (south, lon0),
                (south, lon1),
                (north, lon1),
                (north, lon0),
            ])

        return points

    def _search_goal(self, obs: SwarmObs) -> Tuple[float, float]:
        goal = self._search_waypoints[self._search_wp]

        # Switch before the persistent set_destination can settle into loiter.
        # Lateral 1.6 km shifts are themselves useful nadir search legs.
        threshold = 330.0
        if _dist_m(obs.self.lat, obs.self.lon, goal[0], goal[1]) <= threshold:
            self._search_wp = (self._search_wp + 1) % len(self._search_waypoints)
            goal = self._search_waypoints[self._search_wp]

        return goal

    def _search_commands(self, obs: SwarmObs) -> List[Command]:
        desired = self._search_goal(obs)
        desired = self._deconflict_goal(obs, desired)

        cmds = self._follow_safe_route(
            obs,
            desired,
            mode="search",
            speed=SEARCH_SPEED_MPS,
            final_loiter_radius=240.0,
            refresh_s=4.5,
            moving_goal_replan_m=160.0,
            max_plan_age_s=20.0,
        )

        self._maybe_fov(cmds, SEARCH_FOV_DEG)
        pan, tilt = self._search_scan_gimbal()
        cmds.append(point_gimbal(pan, tilt))
        return cmds

    def _search_scan_gimbal(self) -> Tuple[float, float]:
        """Rate-feasible oblique search scan.

        A nadir camera (tilt=-90) does not move its optical-axis ground
        intersection when pan changes.  V18 therefore scans at -68 deg tilt:
        nadir remains inside the wide 50 deg cone while the cone centre sweeps
        laterally/forward across fresh ground.

        Small per-UAV phase offsets prevent all ten cameras from looking to
        the same side at the same instant.
        """
        phase_offset_s = 0.43 * ((self._idx - 1) % 5) + 0.91 * self._row
        phase = ((self._t + phase_offset_s) % SEARCH_SCAN_PERIOD_S) / SEARCH_SCAN_PERIOD_S
        tri = 2.0 * phase if phase < 0.5 else 2.0 * (1.0 - phase)
        pan = -SEARCH_SCAN_PAN_DEG + 2.0 * SEARCH_SCAN_PAN_DEG * tri
        return pan, SEARCH_SCAN_TILT_DEG

    # ------------------------------------------------------------------
    # Detection / candidate tracking
    # ------------------------------------------------------------------

    def _detections(self, obs: SwarmObs):
        out = []

        d0 = getattr(obs.self, "detection", None)
        if (
            d0 is not None
            and getattr(d0, "detected", False)
            and getattr(d0, "target_lat", None) is not None
            and getattr(d0, "target_lon", None) is not None
        ):
            out.append(d0)

        for d in tuple(getattr(obs.self, "detections", ()) or ()):
            if (
                getattr(d, "detected", False)
                and getattr(d, "target_lat", None) is not None
                and getattr(d, "target_lon", None) is not None
            ):
                # Avoid duplicating the singular default detector result.
                if not any(
                    _dist_m(
                        float(d.target_lat),
                        float(d.target_lon),
                        float(x.target_lat),
                        float(x.target_lon),
                    ) < 2.0
                    for x in out
                ):
                    out.append(d)

        return out

    def _best_detection(
        self,
        obs: SwarmObs,
        ref: Optional[Tuple[float, float]] = None,
    ) -> Optional[Tuple[float, float]]:
        ds = self._detections(obs)
        if not ds:
            return None

        if ref is None:
            d = max(ds, key=lambda x: float(getattr(x, "confidence", 0.0)))
            return float(d.target_lat), float(d.target_lon)

        d = min(
            ds,
            key=lambda x: _dist_m(
                float(x.target_lat),
                float(x.target_lon),
                ref[0],
                ref[1],
            ),
        )
        return float(d.target_lat), float(d.target_lon)

    def _new_candidate(self, lat: float, lon: float) -> None:
        filt = TrackFilter(
            lat=lat,
            lon=lon,
            last_t=self._t,
            last_seen=self._t,
            hits=1,
            history=[(self._t, lat, lon)],
        )
        self._candidate = Candidate(filt=filt, started_at=self._t)
        self._reset_visual_context()

    def _update_candidate(self, det: Optional[Tuple[float, float]]) -> None:
        c = self._candidate
        if c is None:
            return

        pred = (c.filt.lat, c.filt.lon)
        if det is not None and _dist_m(det[0], det[1], pred[0], pred[1]) <= CANDIDATE_GATE_M:
            c.filt.correct(self._t, det[0], det[1], alpha=0.40)
        elif self._t - c.filt.last_seen > 2.4:
            self._candidate = None
            return

        # Do not spend too long shadowing one uncertain contact while the
        # rest of the row is still searching.
        if self._candidate is not None and self._t - c.started_at > CANDIDATE_MAX_AGE_S:
            self._candidate = None

    def _candidate_ready(self) -> bool:
        c = self._candidate
        if c is None:
            return False

        age = self._t - c.started_at
        camera = self._camera_recent()
        min_age = CANDIDATE_CAMERA_MIN_S if camera else CANDIDATE_NO_CAMERA_MIN_S
        min_hits = CANDIDATE_CAMERA_HITS if camera else CANDIDATE_NO_CAMERA_HITS

        # A genuinely centred bbox is useful supporting evidence, but never a
        # hard requirement because only a subset of UAVs may receive camera
        # frames.  It may reduce the geographic-hit requirement by one only.
        if (
            camera
            and self._visual_fresh()
            and self._vision.center_error <= 0.45
            and self._vision.hits >= 2
        ):
            min_hits = max(4, min_hits - 1)

        return (
            age >= min_age
            and c.filt.hits >= min_hits
            and self._t - c.filt.last_seen <= CANDIDATE_FRESH_S
        )

    def _acquire_commands(self, obs: SwarmObs) -> List[Command]:
        # Keep useful search motion while verifying the contact.
        desired = self._search_goal(obs)
        desired = self._deconflict_goal(obs, desired)

        cmds = self._follow_safe_route(
            obs,
            desired,
            mode="search",
            speed=SEARCH_SPEED_MPS,
            final_loiter_radius=240.0,
            refresh_s=4.5,
            moving_goal_replan_m=160.0,
            max_plan_age_s=20.0,
        )

        if self._candidate is None:
            self._vision_set_fov(cmds, obs, VISION_FOV_WIDE)
            cmds.append(point_gimbal(0.0, -90.0))
            return cmds

        geo_pan, geo_tilt = self._gimbal_to(
            obs,
            self._candidate.filt.lat,
            self._candidate.filt.lon,
        )

        desired_fov = self._update_visual_zoom()
        self._vision_set_fov(cmds, obs, desired_fov)

        if self._visual_fresh():
            pan, tilt = self._visual_servo_angles(geo_pan, geo_tilt)
        else:
            pan, tilt = geo_pan, geo_tilt

        cmds.append(point_gimbal(pan, tilt))
        return cmds

    def _update_visual_zoom(self) -> float:
        """Coarse-to-fine zoom with hysteresis and automatic widening."""
        v = self._vision
        now_wall = time.monotonic()

        if not self._visual_fresh():
            self._vision_center_since = None
            lost_for = now_wall - v.last_seen_wall
            if self._vision_stage >= 2 and lost_for > 0.55:
                self._vision_stage = 1
                self._vision_last_stage_t = self._t
            if self._vision_stage >= 1 and lost_for > 1.10:
                self._vision_stage = 0
                self._vision_last_stage_t = self._t
            return (
                VISION_FOV_NARROW if self._vision_stage == 2
                else VISION_FOV_MID if self._vision_stage == 1
                else VISION_FOV_WIDE
            )

        err = v.center_error

        # Large error while zoomed: widen before the target exits the cone.
        if self._vision_stage == 2 and err > 0.48:
            self._vision_stage = 1
            self._vision_center_since = None
            self._vision_last_stage_t = self._t
        elif self._vision_stage == 1 and err > 0.66:
            self._vision_stage = 0
            self._vision_center_since = None
            self._vision_last_stage_t = self._t

        threshold = (
            VISION_WIDE_TO_MID_ERR
            if self._vision_stage == 0
            else VISION_MID_TO_NARROW_ERR
        )

        if self._vision_stage < 2 and err <= threshold and v.hits >= 2:
            if self._vision_center_since is None:
                self._vision_center_since = self._t
            elif (
                self._t - self._vision_center_since >= VISION_CENTER_HOLD_S
                and self._t - self._vision_last_stage_t >= 0.9
            ):
                self._vision_stage += 1
                self._vision_center_since = None
                self._vision_last_stage_t = self._t
        else:
            self._vision_center_since = None

        if self._vision_stage == 2:
            return VISION_FOV_NARROW
        if self._vision_stage == 1:
            return VISION_FOV_MID
        return VISION_FOV_WIDE

    def _visual_servo_angles(
        self,
        geo_pan: float,
        geo_tilt: float,
    ) -> Tuple[float, float]:
        """Add bounded bbox-centre correction to geographic LOS.

        Sign convention follows the official yolotrack controller:
        pan_delta > 0 means target is right of image centre and is ADDED to
        LOS; tilt delta is treated the same way.  This is intentionally not an
        accumulated pixel controller, so one false frame cannot ratchet the
        gimbal away indefinitely.
        """
        v = self._vision
        if (
            v.bbox is None
            or v.image_w <= 0
            or v.image_h <= 0
        ):
            return geo_pan, geo_tilt

        x1, y1, x2, y2 = v.bbox
        cx = 0.5 * (x1 + x2)
        cy = 0.5 * (y1 + y2)

        dx = (cx - v.image_w / 2.0) / max(1.0, v.image_w / 2.0)
        dy = (cy - v.image_h / 2.0) / max(1.0, v.image_h / 2.0)

        hfov = max(5.0, min(50.0, float(v.frame_fov_deg)))
        # Convert horizontal FOV to vertical FOV for the actual 4:3 frame.
        vfov = math.degrees(
            2.0 * math.atan(
                math.tan(math.radians(hfov) / 2.0)
                * (v.image_h / max(1.0, v.image_w))
            )
        )

        pan_delta = dx * (hfov / 2.0)
        tilt_delta = dy * (vfov / 2.0)
        pan_delta = max(
            -VISION_SERVO_MAX_PAN_DEG,
            min(VISION_SERVO_MAX_PAN_DEG, pan_delta),
        )
        tilt_delta = max(
            -VISION_SERVO_MAX_TILT_DEG,
            min(VISION_SERVO_MAX_TILT_DEG, tilt_delta),
        )

        pan = geo_pan + VISION_SERVO_GAIN * pan_delta
        tilt = geo_tilt + VISION_SERVO_GAIN * tilt_delta
        pan = max(-180.0, min(180.0, pan))
        tilt = max(-90.0, min(-5.0, tilt))
        return pan, tilt

    def _vision_set_fov(
        self,
        cmds: List[Command],
        obs: SwarmObs,
        fov: float,
    ) -> None:
        actual = float(
            getattr(obs.self, "gimbal_fov_deg", VISION_FOV_WIDE)
            or VISION_FOV_WIDE
        )
        if (
            abs(actual - fov) >= 1.0
            and self._t - self._last_fov_t >= VISION_FOV_CMD_INTERVAL_S
        ):
            cmds.append(set_gimbal_fov(fov))
            self._last_fov_t = self._t

    # ------------------------------------------------------------------
    # Coalition creation: no fallback members
    # ------------------------------------------------------------------

    def _choose_helpers(
        self,
        obs: SwarmObs,
        target_lat: float,
        target_lon: float,
    ) -> Optional[Tuple[int, int]]:
        candidates: List[Peer] = []

        for p in self._peers.values():
            if p.state not in (self.SEARCH, self.ACQUIRE):
                continue
            if self._t - p.last_seen > PEER_FRESH_S:
                continue

            d_owner = _dist_m(obs.self.lat, obs.self.lon, p.lat, p.lon)
            if d_owner > DIRECT_PEER_MAX_M:
                continue

            candidates.append(p)

        if len(candidates) < 2:
            return None

        best = None
        best_cost = float("inf")

        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                a, b = candidates[i], candidates[j]

                # Prefer one helper on each side of the owner when available.
                sa = -1 if a.lon < obs.self.lon else 1
                sb = -1 if b.lon < obs.self.lon else 1
                same_side_penalty = 420.0 if sa == sb else 0.0

                da = _dist_m(a.lat, a.lon, target_lat, target_lon)
                db = _dist_m(b.lat, b.lon, target_lat, target_lon)

                acquire_penalty = (
                    (260.0 if a.state == self.ACQUIRE else 0.0)
                    + (260.0 if b.state == self.ACQUIRE else 0.0)
                )
                cost = da + db + same_side_penalty + acquire_penalty
                if cost < best_cost:
                    best_cost = cost
                    best = (a.idx, b.idx)

        return best

    def _assign_tracking_roles(
        self,
        obs: SwarmObs,
        helpers: Tuple[int, int],
        target_lat: float,
        target_lon: float,
        seq: int,
    ) -> Tuple[int, int, int]:
        """Assign the three aircraft to three anchor bearings with minimum
        travel cost.  The order of the returned tuple IS the role map.

        V14 sorted member ids, which could make two aircraft cross each other
        on the way to their loiter circles.  Here the owner has fresh helper
        heartbeat positions, so it can choose the minimum-crossing mapping and
        communicate it simply through the member order already present in T.
        """
        ids = (self._idx, helpers[0], helpers[1])
        pos = {
            self._idx: (float(obs.self.lat), float(obs.self.lon)),
            helpers[0]: (self._peers[helpers[0]].lat, self._peers[helpers[0]].lon),
            helpers[1]: (self._peers[helpers[1]].lat, self._peers[helpers[1]].lon),
        }

        phase = self._task_anchor_phase(self._idx, seq)
        anchors = []
        for base_b in TRACK_ANCHOR_BEARINGS_DEG:
            b = math.radians((base_b + phase) % 360.0)
            anchors.append(
                _offset(
                    target_lat,
                    target_lon,
                    TRACK_ANCHOR_RADIUS_M * math.sin(b),
                    TRACK_ANCHOR_RADIUS_M * math.cos(b),
                )
            )

        best = ids
        best_cost = float("inf")
        for perm in itertools.permutations(ids):
            cost = 0.0
            for role, uid in enumerate(perm):
                p = pos[uid]
                a = anchors[role]
                cost += _dist_m(p[0], p[1], a[0], a[1])
            if cost < best_cost:
                best_cost = cost
                best = perm

        return tuple(best)

    @staticmethod
    def _task_anchor_phase(owner: int, seq: int) -> float:
        # Small deterministic rotation avoids repeatedly placing multiple
        # nearby tasks on exactly the same three global bearings.
        return float((owner * 37 + seq * 53) % 120)

    def _try_start_task(self, obs: SwarmObs) -> None:
        c = self._candidate
        if c is None:
            return

        helpers = self._choose_helpers(obs, c.filt.lat, c.filt.lon)
        if helpers is None:
            if self._t - self._last_nohelper_log_t >= 2.0:
                usable = [
                    f"{p.idx}:{p.state}:{_dist_m(obs.self.lat, obs.self.lon, p.lat, p.lon):.0f}m"
                    for p in self._peers.values()
                    if self._t - p.last_seen <= PEER_FRESH_S
                ]
                print(
                    f"[V25][{self.my_uid}] candidate-ready but helpers<2 "
                    f"t={self._t:.1f}s peers={';'.join(sorted(usable))}"
                )
                self._last_nohelper_log_t = self._t
            return

        self._task_seq = (self._task_seq + 1) % 100
        members = self._assign_tracking_roles(
            obs,
            helpers,
            c.filt.lat,
            c.filt.lon,
            self._task_seq,
        )

        filt = TrackFilter(
            lat=c.filt.lat,
            lon=c.filt.lon,
            last_t=self._t,
            last_seen=c.filt.last_seen,
            v_east=c.filt.v_east,
            v_north=c.filt.v_north,
            hits=c.filt.hits,
            history=list(c.filt.history),
        )

        task = Task(
            owner=self._idx,
            seq=self._task_seq,
            members=members,
            filt=filt,
            created_at=self._t,
            last_update=self._t,
            phase="R",
            acks={self._idx: self._t},
            visuals={self._idx: c.filt.last_seen},
            n_destroyed_start=self._n_destroyed,
            last_local_seen=c.filt.last_seen,
        )

        self._task = task
        self._candidate = None

        # New semantic target context.  Discard any in-flight candidate result
        # so an old bbox cannot be consumed by RENDEZVOUS / DWELL.
        self._reset_visual_context()

        self._known_tasks[(task.owner, task.seq)] = (
            self._t,
            task.filt.lat,
            task.filt.lon,
        )

        self._queue_task(task, priority=0)
        self._last_task_tx = self._t
        print(
            f"[V25][{self.my_uid}] TASK_START t={self._t:.1f}s "
            f"seq={task.seq} members={task.members} hits={task.filt.hits}"
        )

    def _accept_task(
        self,
        owner: int,
        seq: int,
        members: Tuple[int, int, int],
        lat: float,
        lon: float,
        ve: float,
        vn: float,
        phase: str,
    ) -> None:
        key = (owner, seq)

        if self._idx not in members:
            return

        # CRITICAL V25 invariant: an owner task is created only by the local
        # candidate->task transition.  A looped-back/rebroadcast T packet may
        # update an existing owner task in _ingest_messages, but can never
        # resurrect one after release.
        if self._idx == owner:
            return

        if self._retired_tasks.get(key, -999.0) > self._t:
            return

        # Never abandon a live task to join a second coalition.
        if self._task is not None:
            return

        filt = TrackFilter(
            lat=lat,
            lon=lon,
            last_t=self._t,
            last_seen=-999.0,
            v_east=ve,
            v_north=vn,
            hits=0,
            history=[],
        )

        self._task = Task(
            owner=owner,
            seq=seq,
            members=members,
            filt=filt,
            created_at=self._t,
            last_update=self._t,
            phase=phase,
            acks={self._idx: self._t},
            visuals={},
            n_destroyed_start=self._n_destroyed,
        )

        self._candidate = None

        # Followers begin with no trusted pixel association.  Their own camera
        # must acquire the task locally; never inherit a previous bbox.
        self._reset_visual_context()

        self._queue_ack(self._task)
        print(
            f"[V25][{self.my_uid}] TASK_ACCEPT t={self._t:.1f}s "
            f"owner={owner} seq={seq} members={members}"
        )

    # ------------------------------------------------------------------
    # Task local visual tracking
    # ------------------------------------------------------------------

    def _update_task_local(self, det: Optional[Tuple[float, float]]) -> None:
        t = self._task
        if t is None:
            return

        pred = (t.filt.lat, t.filt.lon)
        geo_compatible = (
            det is not None
            and _dist_m(det[0], det[1], pred[0], pred[1]) <= TASK_GATE_M
        )

        # Visual support may bridge a SHORT Rain geo dropout, but only after
        # this UAV has already associated the task geographically.  Thus YOLO
        # cannot bootstrap an arbitrary nearby vehicle into the coalition.
        visual_bridge = (
            not geo_compatible
            and t.last_local_seen > -900.0
            and self._t - t.last_local_seen <= TASK_VISUAL_BRIDGE_GEO_AGE_S
            and self._visual_fresh()
            and self._vision.center_error <= TASK_VISUAL_BRIDGE_CENTER_ERR
            and self._vision.hits >= TASK_VISUAL_BRIDGE_MIN_HITS
        )
        new_visual_support = (
            visual_bridge
            and self._vision.result_seq != t.local_visual_seq
        )

        support_hit = geo_compatible or new_visual_support
        if support_hit:
            if (
                t.local_match_hits <= 0
                or self._t - t.local_match_last > FOLLOWER_CONFIRM_GAP_S
            ):
                t.local_match_hits = 0
                t.local_match_first = self._t

            t.local_match_hits += 1
            t.local_match_last = self._t

            if geo_compatible and det is not None:
                t.filt.correct(self._t, det[0], det[1], alpha=0.34)
                t.last_local_seen = self._t
            elif new_visual_support:
                t.local_visual_seq = self._vision.result_seq

            locally_confirmed = (
                self._idx == t.owner
                or (
                    t.local_match_hits >= FOLLOWER_CONFIRM_HITS
                    and self._t - t.local_match_first >= FOLLOWER_CONFIRM_SPAN_S
                )
            )

            if locally_confirmed:
                first_confirm = not t.local_confirmed
                t.local_confirmed = True
                t.visuals[self._idx] = self._t
                if first_confirm:
                    print(
                        f"[V25][{self.my_uid}] LOCAL_CONFIRM t={self._t:.1f}s "
                        f"owner={t.owner} seq={t.seq} hits={t.local_match_hits} "
                        f"geo={'Y' if geo_compatible else 'N'} "
                        f"vision={'Y' if self._visual_fresh() else 'N'}"
                    )

                if self._t - self._last_visual_tx >= 0.68:
                    lat_i, lon_i = _geo_to_int(t.filt.lat, t.filt.lon)
                    self._queue(
                        f"V,{t.owner},{t.seq},{self._idx},{lat_i},{lon_i}",
                        priority=1,
                    )
                    self._last_visual_tx = self._t

        elif visual_bridge and t.local_confirmed:
            # Same fresh bbox result may persist over several control ticks.
            # It may keep an already-established local confirmation alive, but
            # cannot increase the confirmation hit counter repeatedly.
            t.visuals[self._idx] = self._t
            if self._t - self._last_visual_tx >= 0.68:
                lat_i, lon_i = _geo_to_int(t.filt.lat, t.filt.lon)
                self._queue(
                    f"V,{t.owner},{t.seq},{self._idx},{lat_i},{lon_i}",
                    priority=1,
                )
                self._last_visual_tx = self._t

        else:
            if self._t - t.local_match_last > FOLLOWER_CONFIRM_GAP_S:
                t.local_match_hits = 0
                t.local_match_first = -999.0

    def _log_follower_status(
        self,
        obs: SwarmObs,
        det: Optional[Tuple[float, float]],
    ) -> None:
        """Sparse V25 diagnostics for the follower first-lock bottleneck.

        This method is observational only.  It does not mutate the task filter,
        confirmation counters, visual state, route, gimbal, or communication.
        """
        t = self._task
        if t is None or t.owner == self._idx:
            return
        if self._t - self._last_follower_status_log_t < FOLLOWER_STATUS_INTERVAL_S:
            return

        if det is None:
            det_s = "NA"
            geo_ok = False
        else:
            det_d = _dist_m(det[0], det[1], t.filt.lat, t.filt.lon)
            det_s = f"{det_d:.0f}m"
            geo_ok = det_d <= TASK_GATE_M

        geo_age = (
            self._t - t.last_local_seen
            if t.last_local_seen > -900.0
            else 999.0
        )
        visual_fresh = self._visual_fresh()
        visual_err = (
            f"{self._vision.center_error:.2f}"
            if self._vision.center_error < 900.0
            else "NA"
        )

        role = list(t.members).index(self._idx)
        anchor = self._tracking_anchor(obs, t, role)
        target_d = _dist_m(
            obs.self.lat, obs.self.lon, t.filt.lat, t.filt.lon
        )
        anchor_d = _dist_m(
            obs.self.lat, obs.self.lon, anchor[0], anchor[1]
        )

        print(
            f"[V25][{self.my_uid}] FOLLOWER_STATUS t={self._t:.1f}s "
            f"owner={t.owner} seq={t.seq} phase={t.phase} "
            f"age={self._t - t.created_at:.1f}s role={role} "
            f"hits={t.local_match_hits}/{FOLLOWER_CONFIRM_HITS} "
            f"confirmed={'Y' if t.local_confirmed else 'N'} "
            f"det={det_s} geo={'Y' if geo_ok else 'N'} "
            f"geo_age={geo_age:.1f}s "
            f"vision={'Y' if visual_fresh else 'N'} "
            f"vhits={self._vision.hits} verr={visual_err} "
            f"target_d={target_d:.0f}m anchor_d={anchor_d:.0f}m"
        )
        self._last_follower_status_log_t = self._t

    def _deconflict_goal(
        self,
        obs: SwarmObs,
        desired: Tuple[float, float],
    ) -> Tuple[float, float]:
        """Blend the navigation direction away from dangerously close peers.

        Heartbeats are delayed, so this is intentionally a soft look-ahead
        correction rather than a hard collision-avoidance claim.  It starts
        well above the official 200 m penalty boundary and leaves the existing
        SAM visibility-graph planner in charge of final route legality.
        """
        nearest = None
        nearest_d = float("inf")
        for p in self._peers.values():
            if self._t - p.last_seen > PEER_FRESH_S:
                continue
            d = _dist_m(obs.self.lat, obs.self.lon, p.lat, p.lon)
            if d < nearest_d:
                nearest_d = d
                nearest = p

        if nearest is None or nearest_d >= PEER_AVOID_TRIGGER_M:
            return desired

        ref = float(obs.self.lat)
        goal_e = (desired[1] - obs.self.lon) * _m_per_deg_lon(ref)
        goal_n = (desired[0] - obs.self.lat) * M_PER_DEG_LAT
        goal_norm = math.hypot(goal_e, goal_n)
        if goal_norm < 1.0:
            goal_e, goal_n, goal_norm = 0.0, 1.0, 1.0
        goal_e /= goal_norm
        goal_n /= goal_norm

        away_e = (obs.self.lon - nearest.lon) * _m_per_deg_lon(ref)
        away_n = (obs.self.lat - nearest.lat) * M_PER_DEG_LAT
        away_norm = math.hypot(away_e, away_n)
        if away_norm < 1.0:
            # Deterministic tie-break if reported positions nearly coincide.
            ang = math.radians((self._idx * 73 + nearest.idx * 29) % 360)
            away_e, away_n = math.sin(ang), math.cos(ang)
        else:
            away_e /= away_norm
            away_n /= away_norm

        urgency = max(0.0, min(1.0,
            (PEER_AVOID_TRIGGER_M - nearest_d) / PEER_AVOID_TRIGGER_M
        ))
        # Below 220 m, avoidance strongly dominates; near the trigger radius
        # the original mission direction remains dominant.
        weight = 0.65 + 2.6 * urgency
        ve = goal_e + weight * away_e
        vn = goal_n + weight * away_n
        norm = math.hypot(ve, vn)
        if norm < 1e-6:
            ve, vn, norm = away_e, away_n, 1.0

        return self._clamp_map(
            *_offset(
                float(obs.self.lat),
                float(obs.self.lon),
                PEER_AVOID_LOOKAHEAD_M * ve / norm,
                PEER_AVOID_LOOKAHEAD_M * vn / norm,
            ),
            margin_m=180.0,
        )

    def _track_commands(self, obs: SwarmObs) -> List[Command]:
        t = self._task
        if t is None:
            return self._search_commands(obs)

        target = (t.filt.lat, t.filt.lon)
        role = list(t.members).index(self._idx)
        desired = self._tracking_anchor(obs, t, role)
        desired = self._deconflict_goal(obs, desired)

        # Each member flies to a DIFFERENT observation anchor.  This removes
        # V14's dangerous "three aircraft -> one target centre" convergence.
        # The airframe route is slow/persistent; the gimbal still updates every
        # decide() tick to the latest target prediction.
        cmds = self._follow_safe_route(
            obs,
            desired,
            mode=f"track{t.owner}:{t.seq}:{role}",
            speed=TRACK_SPEED_MPS,
            final_loiter_radius=TRACK_LOITER_RADIUS_M,
            refresh_s=3.0,
            moving_goal_replan_m=95.0,
            max_plan_age_s=8.0,
        )

        # V21 keeps a local visual loop alive on EVERY coalition member.
        # Geographic Task.filt remains the coarse/official association source;
        # a fresh local bbox only adds a bounded pixel-domain correction.
        desired_fov = self._update_task_visual_zoom()
        self._vision_set_fov(cmds, obs, desired_fov)

        geo_pan, geo_tilt = self._gimbal_to(obs, target[0], target[1])
        if self._visual_fresh():
            pan, tilt = self._visual_servo_angles(geo_pan, geo_tilt)
        else:
            pan, tilt = geo_pan, geo_tilt

        cmds.append(point_gimbal(pan, tilt))
        return cmds

    def _update_task_visual_zoom(self) -> float:
        """Robust 50 <-> 35 deg zoom for RENDEZVOUS / DWELL.

        Unlike ACQUIRE, active K=3 tracking never enters 25 deg in V21.
        Losing one member's target from a narrow cone is much more expensive
        than the extra appearance detail during the official 20 s dwell.
        """
        v = self._vision
        now_wall = time.monotonic()

        if not self._visual_fresh():
            self._vision_center_since = None

            # After a short stale interval widen immediately.  The bbox is not
            # a task-validity gate; geo LOS continues while visual is stale.
            if now_wall - v.last_seen_wall > VISION_TASK_LOSS_WIDEN_S:
                self._vision_stage = 0
                self._vision_last_stage_t = self._t
            return VISION_TASK_FOV_WIDE

        # A large centre error at 35 deg means the target is approaching the
        # cone edge.  Widen before it exits the official detector's FOV.
        if self._vision_stage >= 1 and v.center_error > VISION_TASK_WIDEN_ERR:
            self._vision_stage = 0
            self._vision_center_since = None
            self._vision_last_stage_t = self._t
            return VISION_TASK_FOV_WIDE

        if self._vision_stage == 0:
            if v.center_error <= VISION_TASK_CENTER_ERR and v.hits >= 2:
                if self._vision_center_since is None:
                    self._vision_center_since = self._t
                elif (
                    self._t - self._vision_center_since >= VISION_TASK_CENTER_HOLD_S
                    and self._t - self._vision_last_stage_t >= 0.9
                ):
                    self._vision_stage = 1
                    self._vision_center_since = None
                    self._vision_last_stage_t = self._t
            else:
                self._vision_center_since = None

        # Clamp legacy stage=2 if a transition happened in ACQUIRE just before
        # task creation.  Task mode is intentionally wide/mid only.
        self._vision_stage = 1 if self._vision_stage >= 1 else 0
        return (
            VISION_TASK_FOV_MID
            if self._vision_stage == 1
            else VISION_TASK_FOV_WIDE
        )

    def _tracking_anchor(
        self,
        obs: SwarmObs,
        t: Task,
        role: int,
    ) -> Tuple[float, float]:
        """Choose a safe deterministic anchor near the role's nominal bearing."""
        phase = self._task_anchor_phase(t.owner, t.seq)
        base = TRACK_ANCHOR_BEARINGS_DEG[role] + phase

        # If the nominal anchor falls in a conservative SAM box or outside the
        # terrain margin, rotate it in deterministic steps.  All members use
        # the same rule, so roles remain separated.
        for delta in (0.0, 30.0, -30.0, 60.0, -60.0, 90.0, -90.0):
            b = math.radians((base + delta) % 360.0)
            p = _offset(
                t.filt.lat,
                t.filt.lon,
                TRACK_ANCHOR_RADIUS_M * math.sin(b),
                TRACK_ANCHOR_RADIUS_M * math.cos(b),
            )
            p = self._clamp_map(p[0], p[1], margin_m=180.0)
            if not any(
                _point_in_rect(p, r)
                for r in self._sam_rects(obs, pad_m=SAM_ROUTE_PAD_M)
            ):
                return p

        # Target was too close to a boxed threat.  Keep the safest clamped
        # nominal anchor; task timeout will eventually release if visual lock
        # cannot be established.
        b = math.radians(base % 360.0)
        p = _offset(
            t.filt.lat,
            t.filt.lon,
            TRACK_ANCHOR_RADIUS_M * math.sin(b),
            TRACK_ANCHOR_RADIUS_M * math.cos(b),
        )
        return self._clamp_map(p[0], p[1], margin_m=180.0)

    def _task_state_machine(self, obs: SwarmObs, dt: float) -> None:
        t = self._task
        if t is None:
            return

        if t.owner != self._idx:
            # Followers stay visually committed through short comm/jam gaps.
            # Give up only when both task updates and local visual are stale.
            if (
                self._t - t.last_update > 11.0
                and self._t - t.last_local_seen > 5.0
            ):
                self._release_local(cooldown_s=6.0)
            return

        age = self._t - t.created_at

        # Real coalition gate: owner + TWO follower ACKs.
        # ACK is a one-time proof that each selected follower really received
        # and adopted this task.  It should not "expire" during a 20 s dwell.
        all_acked = all(m in t.acks for m in t.members)

        if self._t - self._last_task_status_log_t >= 2.0:
            vis_age = ",".join(
                f"{m}:{self._t - t.visuals.get(m, -999.0):.1f}"
                for m in t.members
            )
            print(
                f"[V25][{self.my_uid}] TASK_STATUS t={self._t:.1f}s "
                f"seq={t.seq} phase={t.phase} age={age:.1f}s "
                f"acks={len(t.acks)}/{len(t.members)} vis_age={vis_age} "
                f"ever3={'Y' if t.ever_all_visual else 'N'} "
                f"proxy={t.proxy_dwell:.1f}s"
            )
            self._last_task_status_log_t = self._t

        # If the chosen helpers were not truly reachable, abandon this task
        # rather than pretending that three aircraft exist.
        if age >= 7.5 and not all_acked:
            self._release_owner(code="F", cooldown_s=12.0)
            return

        all_visual = all(
            self._t - t.visuals.get(m, -999.0) <= 1.65
            for m in t.members
        )

        if all_acked and all_visual:
            t.ever_all_visual = True
            t.last_all_visual = self._t
            if t.all_visual_since is None:
                t.all_visual_since = self._t

            # Require sustained evidence before declaring our internal dwell
            # phase.  This is still only a proxy; the official evaluator is
            # the authority.
            if t.phase == "R" and self._t - t.all_visual_since >= 1.4:
                t.phase = "D"
                t.proxy_dwell = 0.0
                self._queue_task(t, priority=0)
                print(
                    f"[V25][{self.my_uid}] DWELL_ENTER t={self._t:.1f}s "
                    f"seq={t.seq} members={t.members}"
                )

            if t.phase == "D":
                t.proxy_dwell += dt
        else:
            t.all_visual_since = None

            if t.phase == "D" and self._t - t.last_all_visual > 2.0:
                # Mirror the official grace/reset concept in our proxy only.
                t.proxy_dwell = 0.0
                t.phase = "R"

        # V25: V24 produced clean 3/3 ACK coalitions, but one follower first
        # reached LOCAL_CONFIRM only just after the old 20 s cutoff.  Keep the
        # same failure semantics, but allow a bounded 34 s first-lock window.
        # This does not relax TASK_GATE_M, hit/span confirmation, or K=3.
        if (
            t.phase == "R"
            and age >= FIRST_LOCK_RENDEZVOUS_TIMEOUT_S
            and not t.ever_all_visual
        ):
            self._release_owner(code="F", cooldown_s=18.0)
            return

        # n_destroyed is global.  Only treat an increase as our success when
        # this task had already accumulated substantial 3-view evidence.
        if (
            self._n_destroyed > t.n_destroyed_start
            and t.proxy_dwell >= 15.0
        ):
            self._release_owner(code="K", cooldown_s=150.0)
            return

        # If all three repeatedly report compatible local visuals for longer
        # than a full official dwell but the judge never destroys anything,
        # this is probably a decoy / wrong association.  Do not waste the row.
        if t.proxy_dwell >= 27.0:
            self._release_owner(code="X", cooldown_s=65.0)
            return

        if age >= (68.0 if t.ever_all_visual else 58.0):
            self._release_owner(code="F", cooldown_s=25.0)

    # ------------------------------------------------------------------
    # Gimbal
    # ------------------------------------------------------------------

    def _gimbal_to(
        self,
        obs: SwarmObs,
        lat: float,
        lon: float,
    ) -> Tuple[float, float]:
        bearing = _bearing_deg(obs.self.lat, obs.self.lon, lat, lon)
        pan = _wrap180(bearing - float(obs.self.heading_deg))

        horizontal = _dist_m(obs.self.lat, obs.self.lon, lat, lon)
        height = max(20.0, float(obs.self.alt))
        tilt = -math.degrees(math.atan2(height, max(1.0, horizontal)))

        # Keep commands inside a conservative physical range.
        pan = max(-180.0, min(180.0, pan))
        tilt = max(-90.0, min(-5.0, tilt))
        return pan, tilt

    def _maybe_fov(self, cmds: List[Command], fov: float) -> None:
        if self._t - self._last_fov_t >= 2.0:
            cmds.append(set_gimbal_fov(fov))
            self._last_fov_t = self._t

    # ------------------------------------------------------------------
    # Navigation / threat avoidance
    # ------------------------------------------------------------------

    def _sam_rects(
        self,
        obs: SwarmObs,
        pad_m: float = 150.0,
    ) -> List[Tuple[float, float, float, float]]:
        rects = []

        for z in tuple(getattr(obs.briefing, "approximate_zones", ()) or ()):
            kind = str(getattr(z, "kind", "")).lower()
            if "air" not in kind and "sam" not in kind:
                continue

            try:
                (lat0, lon0), (lat1, lon1) = z.bbox
            except Exception:
                continue

            dlat = pad_m / M_PER_DEG_LAT
            dlon = pad_m / _m_per_deg_lon(CENTER_LAT)
            rects.append(
                (
                    min(lat0, lat1) - dlat,
                    max(lat0, lat1) + dlat,
                    min(lon0, lon1) - dlon,
                    max(lon0, lon1) + dlon,
                )
            )

        return rects

    def _clamp_map(
        self,
        lat: float,
        lon: float,
        margin_m: float = 170.0,
    ) -> Tuple[float, float]:
        dlat = margin_m / M_PER_DEG_LAT
        dlon = margin_m / _m_per_deg_lon(CENTER_LAT)
        return (
            min(LAT_MAX - dlat, max(LAT_MIN + dlat, lat)),
            min(LON_MAX - dlon, max(LON_MIN + dlon, lon)),
        )

    def _obstacle_signature(
        self,
        rects: List[Tuple[float, float, float, float]],
    ) -> Tuple:
        return tuple(
            tuple(round(v, 7) for v in r)
            for r in rects
        )

    def _segment_clear(
        self,
        a: Tuple[float, float],
        b: Tuple[float, float],
        rects: List[Tuple[float, float, float, float]],
    ) -> bool:
        return not any(_segment_intersects_rect(a, b, r) for r in rects)

    def _plan_safe_path(
        self,
        start: Tuple[float, float],
        goal: Tuple[float, float],
        rects: List[Tuple[float, float, float, float]],
    ) -> List[Tuple[float, float]]:
        """Visibility-graph shortest path around ALL conservative SAM boxes.

        Every accepted edge is checked start-to-end against every rectangle.
        This is the key V15 correction: V14 checked only the detour-corner ->
        final-goal leg and could command an aircraft *through* a SAM box on the
        current-position -> chosen-corner leg.
        """
        start = self._clamp_map(start[0], start[1], margin_m=120.0)
        goal = self._clamp_map(goal[0], goal[1], margin_m=120.0)

        # If replanning begins while the aircraft is already inside our
        # conservative padded box, freezing is worse than moving.  Command the
        # nearest outward escape point first.  This can happen even while the
        # aircraft is still outside the smaller true SAM polygon because our
        # own buffer is deliberately large.
        enclosing = [r for r in rects if _point_in_rect(start, r)]
        if enclosing:
            escape_candidates = []
            step_lat = 80.0 / M_PER_DEG_LAT
            step_lon = 80.0 / _m_per_deg_lon(CENTER_LAT)
            for r in enclosing:
                lat0, lat1, lon0, lon1 = r
                opts = (
                    (lat0 - step_lat, start[1]),
                    (lat1 + step_lat, start[1]),
                    (start[0], lon0 - step_lon),
                    (start[0], lon1 + step_lon),
                )
                for q in opts:
                    q = self._clamp_map(q[0], q[1], margin_m=120.0)
                    if any(_point_in_rect(q, rr) for rr in rects):
                        continue
                    escape_candidates.append(
                        (_dist_m(start[0], start[1], q[0], q[1]), q)
                    )
            if escape_candidates:
                escape_candidates.sort(key=lambda x: x[0])
                return [escape_candidates[0][1]]

        # A requested goal inside a conservative box is not reachable without
        # violating our own safety model.  Project it toward the closest safe
        # side before building the graph.
        for r in rects:
            if _point_in_rect(goal, r):
                lat0, lat1, lon0, lon1 = r
                options = [
                    (lat0 - 60.0 / M_PER_DEG_LAT, goal[1]),
                    (lat1 + 60.0 / M_PER_DEG_LAT, goal[1]),
                    (goal[0], lon0 - 60.0 / _m_per_deg_lon(CENTER_LAT)),
                    (goal[0], lon1 + 60.0 / _m_per_deg_lon(CENTER_LAT)),
                ]
                options = [
                    self._clamp_map(p[0], p[1], margin_m=120.0)
                    for p in options
                ]
                options = [
                    p for p in options
                    if not any(_point_in_rect(p, rr) for rr in rects)
                ]
                if options:
                    goal = min(
                        options,
                        key=lambda p: _dist_m(p[0], p[1], start[0], start[1]),
                    )

        if self._segment_clear(start, goal, rects):
            return [goal]

        nodes: List[Tuple[float, float]] = [start, goal]
        extra_lat = SAM_CORNER_CLEARANCE_M / M_PER_DEG_LAT
        extra_lon = SAM_CORNER_CLEARANCE_M / _m_per_deg_lon(CENTER_LAT)

        for r in rects:
            lat0, lat1, lon0, lon1 = r
            corners = (
                (lat0 - extra_lat, lon0 - extra_lon),
                (lat0 - extra_lat, lon1 + extra_lon),
                (lat1 + extra_lat, lon0 - extra_lon),
                (lat1 + extra_lat, lon1 + extra_lon),
            )
            for p in corners:
                p = self._clamp_map(p[0], p[1], margin_m=120.0)
                if not any(_point_in_rect(p, rr) for rr in rects):
                    nodes.append(p)

        n = len(nodes)
        graph: List[List[Tuple[int, float]]] = [[] for _ in range(n)]

        # Search-leg orientation for V17's soft corridor bias.  On the long
        # north/south comb legs every UAV has its own goal longitude, so this
        # makes adjacent aircraft prefer different safe corners.  On the short
        # east/west shift legs the analogous quantity is latitude.
        search_leg_ns = False
        if self._route_mode.startswith("search"):
            ref_lat = 0.5 * (start[0] + goal[0])
            total_e = (goal[1] - start[1]) * _m_per_deg_lon(ref_lat)
            total_n = (goal[0] - start[0]) * M_PER_DEG_LAT
            search_leg_ns = abs(total_n) >= abs(total_e)

        # Pairwise visibility graph.  The node count is tiny (typically <30),
        # so O(n^2 * zones) is negligible at route-replan frequency.
        for i in range(n):
            for j in range(i + 1, n):
                if not self._segment_clear(nodes[i], nodes[j], rects):
                    continue

                d = _dist_m(
                    nodes[i][0], nodes[i][1],
                    nodes[j][0], nodes[j][1],
                )

                # Mild row-preservation penalty.  It does not make an unsafe
                # edge legal; it only breaks otherwise-similar detours so the
                # two 5-UAV search rows are less likely to merge.
                mid_lat = 0.5 * (nodes[i][0] + nodes[j][0])
                mid = 0.5 * (LAT_MIN + LAT_MAX)
                cross_pen = 0.0
                if self._route_mode.startswith("search"):
                    if self._row == 0 and mid_lat > mid:
                        cross_pen = 0.22 * d
                    elif self._row == 1 and mid_lat < mid:
                        cross_pen = 0.22 * d

                corridor_pen = 0.0
                if self._route_mode.startswith("search"):
                    edge_mid_lat = 0.5 * (nodes[i][0] + nodes[j][0])
                    edge_mid_lon = 0.5 * (nodes[i][1] + nodes[j][1])
                    if search_leg_ns:
                        lane_dev_m = abs(
                            (edge_mid_lon - goal[1])
                            * _m_per_deg_lon(edge_mid_lat)
                        )
                    else:
                        lane_dev_m = abs(
                            (edge_mid_lat - goal[0]) * M_PER_DEG_LAT
                        )
                    corridor_pen = SEARCH_CORRIDOR_BIAS * lane_dev_m

                w = d + cross_pen + corridor_pen
                graph[i].append((j, w))
                graph[j].append((i, w))

        inf = float("inf")
        dist = [inf] * n
        prev = [-1] * n
        dist[0] = 0.0
        pq = [(0.0, 0)]

        while pq:
            du, u = heapq.heappop(pq)
            if du != dist[u]:
                continue
            if u == 1:
                break
            for v, w in graph[u]:
                nd = du + w
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))

        if not math.isfinite(dist[1]):
            # Do NOT fall back to a direct unsafe line.  Choose the nearest
            # visible graph node and hold that safe direction; a later replan
            # can recover when the aircraft position changes.
            visible = []
            for i in range(2, n):
                if self._segment_clear(start, nodes[i], rects):
                    visible.append(
                        (
                            _dist_m(start[0], start[1], nodes[i][0], nodes[i][1]),
                            nodes[i],
                        )
                    )
            if visible:
                visible.sort(key=lambda x: x[0])
                return [visible[0][1]]
            return [start]

        order = []
        cur = 1
        while cur != -1:
            order.append(cur)
            if cur == 0:
                break
            cur = prev[cur]
        order.reverse()

        # Exclude start node.  Keep all intermediate corner waypoints and goal.
        return [nodes[i] for i in order[1:]]

    def _follow_safe_route(
        self,
        obs: SwarmObs,
        desired: Tuple[float, float],
        *,
        mode: str,
        speed: float,
        final_loiter_radius: float,
        refresh_s: float,
        moving_goal_replan_m: float,
        max_plan_age_s: float,
    ) -> List[Command]:
        desired = self._clamp_map(desired[0], desired[1], margin_m=170.0)
        rects = self._sam_rects(obs, pad_m=SAM_ROUTE_PAD_M)
        sig = self._obstacle_signature(rects)
        start = (float(obs.self.lat), float(obs.self.lon))

        # Diagnostic only.  V21 already handles an inside-padded-box start in
        # _plan_safe_path by selecting the nearest outward waypoint.  V23 does
        # NOT add V22's second max-speed override on top of that planner.
        if (
            any(_point_in_rect(start, r) for r in rects)
            and self._t - self._last_sam_inside_log_t >= 2.0
        ):
            print(
                f"[V25][{self.my_uid}] SAM_PADDED_INSIDE "
                f"t={self._t:.2f}s mode={mode}"
            )
            self._last_sam_inside_log_t = self._t

        # Let _plan_safe_path know whether to apply row-preservation bias.
        self._route_mode = mode

        need_plan = (
            not self._route
            or self._route_i >= len(self._route)
            or self._route_goal is None
            or self._route_sig != sig
            or self._last_nav_mode != mode
            or _dist_m(
                self._route_goal[0],
                self._route_goal[1],
                desired[0],
                desired[1],
            ) >= moving_goal_replan_m
            or self._t - self._route_planned_t >= max_plan_age_s
        )

        if need_plan:
            self._route = self._plan_safe_path(start, desired, rects)
            self._route_i = 0
            self._route_goal = desired
            self._route_sig = sig
            self._route_planned_t = self._t

        # Advance intermediate waypoints EARLY.  fly_to() loiters at its
        # destination, so waiting until the exact corner creates the loops
        # visible in V14's blue trails.
        while self._route_i < len(self._route) - 1:
            wp = self._route[self._route_i]
            if _dist_m(start[0], start[1], wp[0], wp[1]) > ROUTE_SWITCH_M:
                break
            self._route_i += 1

        if not self._route:
            return []

        wp = self._route[min(self._route_i, len(self._route) - 1)]
        is_final = self._route_i >= len(self._route) - 1

        # Intermediate corner waypoints are never intended as loiter points.
        # Final tracking anchors do loiter; search finals switch to the next
        # search leg before reaching the loiter threshold in _search_goal().
        loiter = final_loiter_radius if is_final else 110.0

        return self._nav(
            obs,
            wp,
            mode=mode,
            speed=speed,
            loiter_radius=loiter,
            refresh_s=refresh_s,
            turn_direction="right",
        )

    def _nav(
        self,
        obs: SwarmObs,
        goal: Tuple[float, float],
        *,
        mode: str,
        speed: float,
        loiter_radius: float,
        refresh_s: float,
        turn_direction: str = "right",
    ) -> List[Command]:
        cmds: List[Command] = []

        changed = (
            self._last_nav_goal is None
            or _dist_m(
                self._last_nav_goal[0],
                self._last_nav_goal[1],
                goal[0],
                goal[1],
            ) > 35.0
            or self._last_nav_mode != mode
        )

        if changed or self._t - self._last_nav_t >= refresh_s:
            cmds.append(
                fly_to(
                    goal[0],
                    goal[1],
                    alt=SEARCH_ALT_M,
                    speed=speed,
                    loiter_radius=loiter_radius,
                    turn_direction=turn_direction,
                )
            )
            self._last_nav_goal = goal
            self._last_nav_t = self._t
            self._last_nav_mode = mode

        return cmds

    # ------------------------------------------------------------------
    # Communication
    # ------------------------------------------------------------------

    def _queue(self, payload: str, priority: int = 2) -> None:
        if len(payload.encode("utf-8")) > 50:
            return

        # Do not duplicate identical queued control messages.
        if any(p == payload for _, p in self._outbox):
            return

        self._outbox.append((priority, payload))
        self._outbox.sort(key=lambda x: x[0])

        if len(self._outbox) > 14:
            self._outbox = self._outbox[:14]

    def _queue_heartbeat(self, obs: SwarmObs) -> None:
        if bool(getattr(obs.self, "jammed", False)):
            return

        period = 1.35 if self._task is None else 2.1
        if self._t - self._last_hb < period:
            return

        lat_i, lon_i = _geo_to_int(float(obs.self.lat), float(obs.self.lon))
        self._queue(
            f"H,{self._idx},{lat_i},{lon_i},{self._state}",
            priority=3,
        )
        self._last_hb = self._t

    def _queue_task(self, t: Task, priority: int = 0) -> None:
        lat_i, lon_i = _geo_to_int(t.filt.lat, t.filt.lon)
        ve = int(round(max(-12.0, min(12.0, t.filt.v_east))))
        vn = int(round(max(-12.0, min(12.0, t.filt.v_north))))
        member_s = ".".join(str(m) for m in t.members)

        self._queue(
            f"T,{t.owner},{t.seq},{member_s},{lat_i},{lon_i},{ve},{vn},{t.phase}",
            priority=priority,
        )

    def _queue_ack(self, t: Task) -> None:
        self._queue(
            f"A,{t.owner},{t.seq},{self._idx}",
            priority=0,
        )
        self._last_ack_tx = self._t

    def _task_periodic(self, obs: SwarmObs, force: bool = False) -> None:
        t = self._task
        if t is None:
            return

        if t.owner == self._idx:
            if force or self._t - self._last_task_tx >= 0.82:
                self._queue_task(t, priority=0)
                self._last_task_tx = self._t
        else:
            # Repeat ACK briefly until owner updates are clearly flowing.
            if (
                self._t - t.created_at <= 5.0
                and self._t - self._last_ack_tx >= 1.05
            ):
                self._queue_ack(t)

    def _flush(self, cmds: List[Command], obs: SwarmObs) -> None:
        if bool(getattr(obs.self, "jammed", False)):
            return
        if self._t < self._next_tx or not self._outbox:
            return

        _, payload = self._outbox.pop(0)
        cmds.append(broadcast(payload))

        # 0.29 s => <= 3.45 Hz, leaving margin under the 4 Hz cap.
        self._next_tx = self._t + 0.29

    def _ingest_messages(self, obs: SwarmObs) -> None:
        for msg in tuple(getattr(obs, "comm_inbox", ()) or ()):
            try:
                parts = str(msg.payload).split(",")
                kind = parts[0]

                if kind == "H" and len(parts) >= 5:
                    idx = int(parts[1])
                    if idx == self._idx:
                        continue
                    lat, lon = _int_to_geo(parts[2], parts[3])
                    self._peers[idx] = Peer(
                        idx=idx,
                        lat=lat,
                        lon=lon,
                        state=parts[4],
                        last_seen=self._t,
                    )

                elif kind == "T" and len(parts) >= 9:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    members = tuple(int(x) for x in parts[3].split("."))
                    if len(members) != 3:
                        continue
                    lat, lon = _int_to_geo(parts[4], parts[5])
                    ve = float(parts[6])
                    vn = float(parts[7])
                    phase = parts[8] if parts[8] in ("R", "D") else "R"

                    key = (owner, seq)

                    # A retired exact task key must never be resurrected by a
                    # delayed T still circulating through the radio network.
                    if self._retired_tasks.get(key, -999.0) > self._t:
                        if self._t - self._last_stale_task_log_t >= 3.0:
                            print(
                                f"[V25][{self.my_uid}] STALE_T_DROP "
                                f"t={self._t:.1f}s owner={owner} seq={seq}"
                            )
                            self._last_stale_task_log_t = self._t
                        continue

                    # Ignore our own T packet completely.  It can arrive back
                    # through broadcast/relay, but local owner state is the
                    # authoritative source and must not be recreated from radio.
                    if owner == self._idx:
                        continue

                    self._known_tasks[key] = (self._t, lat, lon)

                    # Same target already claimed by another coalition:
                    # do not allow our single-aircraft candidate to start a
                    # duplicate 3-UAV pull.
                    if (
                        self._candidate is not None
                        and _dist_m(
                            self._candidate.filt.lat,
                            self._candidate.filt.lon,
                            lat,
                            lon,
                        ) <= 260.0
                    ):
                        self._candidate = None

                    if self._task is None:
                        if self._idx in members:
                            self._accept_task(
                                owner, seq, members, lat, lon, ve, vn, phase
                            )
                        continue

                    t = self._task
                    if t.owner == owner and t.seq == seq:
                        # Idempotent same-task update: preserve local history,
                        # confirmation counters, visual generation and created_at.
                        t.filt.predict_to(self._t)
                        if _dist_m(t.filt.lat, t.filt.lon, lat, lon) <= 260.0:
                            t.filt.lat = 0.70 * t.filt.lat + 0.30 * lat
                            t.filt.lon = 0.70 * t.filt.lon + 0.30 * lon
                            t.filt.v_east = 0.75 * t.filt.v_east + 0.25 * ve
                            t.filt.v_north = 0.75 * t.filt.v_north + 0.25 * vn
                        t.last_update = self._t

                        # Task phase is monotonic for one owner/seq.  A stale
                        # R packet may never kick a locally/currently D task
                        # back to rendezvous.  D from the owner may promote R.
                        if phase == "D":
                            t.phase = "D"

                elif kind == "A" and len(parts) >= 4:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    member = int(parts[3])

                    t = self._task
                    if (
                        t is not None
                        and t.owner == self._idx == owner
                        and t.seq == seq
                        and member in t.members
                    ):
                        t.acks[member] = self._t

                elif kind == "V" and len(parts) >= 6:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    member = int(parts[3])
                    lat, lon = _int_to_geo(parts[4], parts[5])

                    t = self._task
                    if (
                        t is not None
                        and t.owner == self._idx == owner
                        and t.seq == seq
                        and member in t.members
                        and _dist_m(
                            lat,
                            lon,
                            t.filt.lat,
                            t.filt.lon,
                        ) <= VISUAL_GATE_M
                    ):
                        t.visuals[member] = self._t
                        t.acks[member] = self._t

                        # Very weak multi-view fusion.  The owner remains the
                        # dominant track so three noisy measurements cannot
                        # drag the common target toward adjacent vehicles.
                        t.filt.lat = 0.94 * t.filt.lat + 0.06 * lat
                        t.filt.lon = 0.94 * t.filt.lon + 0.06 * lon

                elif kind == "D" and len(parts) >= 4:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    code = parts[3]
                    key = (owner, seq)

                    self._known_tasks.pop(key, None)
                    self._retired_tasks[key] = max(
                        self._retired_tasks.get(key, -999.0),
                        self._t + TASK_TOMBSTONE_S,
                    )

                    if (
                        self._task is not None
                        and self._task.owner == owner
                        and self._task.seq == seq
                    ):
                        cd = 120.0 if code == "K" else 55.0 if code == "X" else 12.0
                        self._release_local(cooldown_s=cd)

            except (ValueError, TypeError, IndexError):
                continue

    # ------------------------------------------------------------------
    # Task release / bookkeeping
    # ------------------------------------------------------------------

    def _release_owner(self, code: str, cooldown_s: float) -> None:
        t = self._task
        if t is None:
            return

        key = (t.owner, t.seq)
        self._retired_tasks[key] = max(
            self._retired_tasks.get(key, -999.0),
            self._t + TASK_TOMBSTONE_S,
        )

        self._queue(
            f"D,{t.owner},{t.seq},{code}",
            priority=0,
        )

        self._cooldowns.append(
            Cooldown(
                lat=t.filt.lat,
                lon=t.filt.lon,
                expires_at=self._t + cooldown_s,
            )
        )
        self._known_tasks.pop((t.owner, t.seq), None)
        self._task = None
        self._candidate = None
        self._reset_visual_context()
        self._route = []
        self._route_i = 0
        self._route_goal = None
        self._last_nav_mode = ""

    def _release_local(self, cooldown_s: float) -> None:
        t = self._task
        if t is not None:
            self._cooldowns.append(
                Cooldown(
                    lat=t.filt.lat,
                    lon=t.filt.lon,
                    expires_at=self._t + cooldown_s,
                )
            )
            self._known_tasks.pop((t.owner, t.seq), None)

        self._task = None
        self._candidate = None
        self._reset_visual_context()
        self._route = []
        self._route_i = 0
        self._route_goal = None
        self._last_nav_mode = ""

    def _on_cooldown(self, lat: float, lon: float) -> bool:
        return any(
            c.expires_at > self._t
            and _dist_m(lat, lon, c.lat, c.lon) <= 240.0
            for c in self._cooldowns
        )

    def _near_known_task(self, lat: float, lon: float) -> bool:
        for ts, la, lo in self._known_tasks.values():
            if self._t - ts <= 8.0 and _dist_m(lat, lon, la, lo) <= 280.0:
                return True
        return False

    def _update_score(self, obs: SwarmObs) -> None:
        score = getattr(obs.briefing, "score_view", None)
        if score is not None:
            self._n_destroyed = int(
                getattr(score, "n_destroyed", self._n_destroyed)
            )

    def _prune(self) -> None:
        self._peers = {
            k: p for k, p in self._peers.items()
            if self._t - p.last_seen <= 5.0
        }

        self._known_tasks = {
            k: v for k, v in self._known_tasks.items()
            if self._t - v[0] <= 12.0
        }

        self._cooldowns = [
            c for c in self._cooldowns
            if c.expires_at > self._t
        ]

        self._retired_tasks = {
            k: until for k, until in self._retired_tasks.items()
            if until > self._t
        }


__all__ = ["HF2026V25Agent"]
