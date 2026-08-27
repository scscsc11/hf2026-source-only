"""Cooperative multi-UAV search/track controller (017).

Design (research.md D-8 + spec FR-019~022):
  - One CoopController instance PER UAV. Each instance is constructed with
    its own `my_uid` so it can identify "self" in the MultiSimState and
    filter comm inbox messages accordingly.
  - decide() takes a MultiSimState (not the 016 single-UAV SimState) so it
    sees all entities + comm inboxes. This is a DELIBERATE deviation from
    016's Controller.decide(SimState) signature — CoopController does NOT
    inherit from 016's Controller, because the input contract is different.
    It still obeys the same output contract (list of publishable dicts).
  - Per-UAV search/track FSM reuses 016's FsmSearchTrackController logic
    (spiral search + loiter track) via composition: each CoopController
    holds a FsmSearchTrackController for the geometric leg.
  - Cooperation (FR-021/022): when this UAV is tracking a target, it
    broadcasts "TRACKING <target_uid>" so peers can deprioritize that
    target. When this UAV hears a peer is already tracking a target it
    has detected, it yields (keeps searching) instead of duplicating.

Output: list of dicts ready for MultiSimClient.publish_dict(). Mixing
ControlCommand-shaped dicts (set_destination / set_orientation) and
CommCommand-shaped dicts (comm.broadcast / comm.send).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from examples.uav_search_track_car.search_track.commands import (
    CommandTarget, ControlCommand,
)
from examples.uav_search_track_car.search_track.fsm_controller import (
    FsmSearchTrackController,
)
from examples.uav_search_track_car.search_track.geometry import (
    bearing_deg, haversine_m, los_angles,
)
from examples.uav_search_track_car.search_track.search_strategies import (
    sweep_orientation,
)
from examples.uav_search_track_car.search_track.state import SimState

from .comm_adapter import broadcast
from .decoy_classifier import DecoyClassifier
from .multi_state import EntityState, MultiSimState
from .sector_search import SectorSearchParams, destination_point, sector_waypoint


class CoopController:
    """Per-UAV cooperative search/track controller.

    Not a 016 Controller subclass (input contract differs: MultiSimState
    vs SimState). Composes a FsmSearchTrackController for the geometric
    search/track leg and adds communication-driven coordination.
    """

    def __init__(self, my_uid: str = "") -> None:
        self.my_uid = my_uid
        self._fsm = FsmSearchTrackController()
        self._configured = False
        # Targets we know a peer is already tracking (uid -> peer_uid).
        self._peer_tracking: dict[str, str] = {}
        # Last sim_time we broadcast our tracking status.
        self._last_broadcast_t: float = -1e9
        # Broadcast period (seconds) — keep well under max_rate_hz=4.
        self._broadcast_period: float = 1.0
        # Cumulative misid-track detection (FR-015) for metrics.
        self.misid_track_ticks: int = 0
        # ── Sector-divided search (replaces single-centre spiral) ──
        # When True, SEARCH commands are produced by sector_search instead
        # of the composed FSM's spiral, so the fleet fans out instead of
        # all spiralling on top of each other.
        self._use_sector_search: bool = True
        self._sector_params: SectorSearchParams | None = None
        # Fleet position: assigned by run.py so each UAV gets a unique,
        # stable sector. Defaults (0, 1) keep a single-UAV fallback sane.
        self._fleet_index: int = 0
        self._fleet_size: int = 1
        # Gimbal sweep for the sector-search leg (mirrors FSM sweep params
        # so the camera footprint sweeps both sides of the flight path).
        self._sweep_period: float = 4.0
        self._sweep_pitch_min: float = -60.0
        self._sweep_pitch_max: float = -30.0
        # Effective mode: reflects what the UAV is ACTUALLY doing this tick.
        # Differs from _fsm.mode when the cooperation yield overrides TRACK
        # back to SEARCH behaviour. This is what run.py reads for events.
        self._effective_mode: str = "SEARCH"
        # ── Track-anchor (target-identity) tracking ──
        # The C++ gimbal_tracking has auto_track=true, so it autonomously
        # locks the nearest ground/decoy in the world snapshot every tick.
        # When the UAV moves (or a decoy moves closer), the gimbal's
        # "current target" can silently switch from the original lock to a
        # different entity — but `detection.detected` stays True, so the
        # FSM's _consecutive_lost never accumulates and we never publish
        # state.exit_track. To make algorithm-side tracking-loss real, we
        # remember the locked target's last-known position when entering
        # TRACK and treat any large jump in detection.target_position as a
        # lost lock — synthesised as detected=False to feed the FSM.
        self._track_anchor_lat: float | None = None
        self._track_anchor_lon: float | None = None
        # Search-geometry time origin. The sector_search / sweep formulas
        # (sector_radius, sweep_orientation, spiral_next_waypoint) all assume
        # `t` grows from 0, but the engine's absolute sim_time is a large
        # negative epoch-based value (e.g. -28799). Feeding absolute sim_time
        # made sector_radius compute a hugely negative radius (clamped to 1m),
        # collapsing every UAV's waypoint onto the sector centre. We anchor
        # t=0 at the first decide() call and feed the elapsed time instead.
        self._search_t0: float | None = None
        # 80 m: smaller than the decoy grid spacing (~111 m at the scenario's
        # 0.001-deg lat/lon offsets in scenario.json) so jumps to neighbour
        # decoys/targets are caught, larger than per-tick GPS jitter +
        # genuine target motion (≤12 m/s × 0.1 s tick = 1.2 m, ≤12 m/s ×
        # loiter_refresh_period 3 s = 36 m).
        self._track_jump_threshold_m: float = 80.0
        # ── Task 3 cooperative summon ──
        # When True the fleet CONVERGES on real targets to co-track them
        # (K>=2 completion): each UAV classifies what it tracks (rejecting
        # decoys), broadcasts confirmed-real target positions, and any free
        # UAV navigates to a known real target to help. Targets are worked
        # sequentially — after dwelling on one long enough it is marked
        # self-done and the UAV moves to the next. The sector-commitment
        # filter (Task 1 spread) is disabled in this mode so UAVs can cross
        # sectors to converge.
        self._coop_summon: bool = False
        # Sector-commitment filter on/off (separate from coop_summon so the
        # full scenario — whose targets MOVE across sectors — can disable it
        # while still using the sector-wedge search for coverage + the FSM
        # track + classifier).
        self._sector_commitment: bool = True
        # Sector rotation (Task 3): if a UAV finds no REAL target in its
        # assigned wedge within reassign_period_s, it rotates to the next
        # wedge. The full scenario's real targets do NOT sit one-per-sector
        # (and they move across sectors), so a fixed wedge split leaves some
        # UAVs targetless; rotation lets every UAV eventually search a wedge
        # that contains a target. No comms / no ground truth used.
        self._sector_offset: int = 0
        self._last_real_sim_time: float | None = None
        self._reassign_period_s: float = 55.0
        self._search_start_sim_time: float | None = None
        self._dwell_target_s: float = 25.0  # time-on-target before self-done
        self._clf: "DecoyClassifier | None" = None  # lazily built per TRACK
        self._verify_clf: "DecoyClassifier | None" = None  # SEARCH-phase verify
        self._verify_pos: tuple[float, float] | None = None  # target being verified
        self._known_real: dict[str, tuple[float, float, float]] = {}  # key->(lat,lon,first_seen_t)
        self._self_done: set[str] = set()
        self._cur_real_key: str | None = None
        self._dwell_acc: float = 0.0
        self._last_r_broadcast_t: float = -1e9
        # Positions classified as DECOY — avoid re-locking (fly elsewhere).
        self._decoy_avoid: set[str] = set()
        # ── EMA smoothing for target_position ──
        # Prevents gimbal-angle jumps when detection alternates between real
        # and decoy vehicles tick-to-tick (alpha=0.3 ≈ 3–4 tick convergence).
        self._smooth_lat: float | None = None
        self._smooth_lon: float | None = None
        # Last-known gimbal pan/tilt (holdover when detection flickers).
        self._last_pan: float = 0.0
        self._last_tilt: float = -45.0
        self._last_detected_sim_time: float | None = None
        self._verify_hold_s: float = 1.0
        self._decoy_cooldown_until: float = -1e9
        self._decoy_avoid_pan: float | None = None
        self._decoy_cooldown_s: float = 12.0
        self._decoy_cooldown_radius_m: float = 80.0
        self._decoy_avoid_margin_deg: float = 25.0
        self._decoy_cooldowns: list[tuple[float, float, float]] = []  # lat, lon, expire_t
        # Confirmed-real target filter.  After the classifier says REAL,
        # gimbal/loiter commands follow this filtered state instead of raw
        # decoy-contaminated detection.target_position.
        self._real_filter_alpha: float = 0.75
        self._real_filter_beta: float = 0.45
        self._real_gate_m: float = 120.0
        self._real_max_speed_mps: float = 35.0
        self._real_coast_timeout_s: float = 2.5
        self._real_vel_lat_dps: float = 0.0
        self._real_vel_lon_dps: float = 0.0
        self._real_last_meas_t: float | None = None
        self._track_standoff_m: float = 220.0
        self._track_orbit_radius_m: float = 120.0
        # Latched True once the decoy classifier confirms a REAL target
        # in sector-spread mode.  Used to decide whether the search-stage
        # decoy gate should still apply: once we are committed to a real
        # track we do not want a near-cooldown decoy to re-spoof detection.
        self._real_track_active: bool = False
        # Position key of the real target the FSM is currently tracking.
        # While _real_track_active is True, the search-stage gate will only
        # allow detections whose position key matches this one; anything
        # else (e.g. engine switching to a decoy) is spoofed, so the FSM
        # stops accumulating k_acquire/k_lost and toggling TRACK.
        self._real_target_key: str | None = None

    def configure(self, cfg: Any) -> None:
        # Delegate FSM tuning to the composed FsmSearchTrackController.
        if hasattr(self._fsm, "configure"):
            self._fsm.configure(cfg)
        if isinstance(cfg, dict) or hasattr(cfg, "get"):
            self._broadcast_period = float(cfg.get(
                "coop_broadcast_period", self._broadcast_period))
            # Sector-search config. search_radius/altitude come from the
            # top-level config (exposed as AlgorithmConfig attributes);
            # the rest from `advanced`.
            search_radius = float(cfg.get("search_radius", 800.0))
            search_alt = float(cfg.get("search_altitude_agl", 300.0))
            self._use_sector_search = bool(
                cfg.get("use_sector_search", self._use_sector_search))
            # expand_time: prefer explicit key, fall back to
            # search_sweep_time for backward compat.
            expand_time = float(cfg.get("expand_time", 0.0))
            if expand_time <= 0.0:
                expand_time = float(cfg.get("search_sweep_time", 30.0))
            self._sector_params = SectorSearchParams(
                base_lat=float(cfg.get("sector_center_latitude", 0.0) or 0.0),
                base_lon=float(cfg.get("sector_center_longitude", 0.0) or 0.0),
                base_alt=search_alt,
                search_radius=search_radius,
                expand_time=expand_time,
                sector_angular_speed_dps=float(
                    cfg.get("sector_angular_speed_dps", 40.0)),
                initial_radius_frac=float(
                    cfg.get("initial_radius_frac", 0.15)),
                radius_dither_frac=float(
                    cfg.get("radius_dither_frac", 0.08)),
                search_sweep_time=expand_time,  # legacy compat
            )
            self._sweep_period = float(cfg.get("sweep_period", self._sweep_period))
            self._sweep_pitch_min = float(
                cfg.get("sweep_pitch_min", self._sweep_pitch_min))
            self._sweep_pitch_max = float(
                cfg.get("sweep_pitch_max", self._sweep_pitch_max))
            self._track_jump_threshold_m = float(
                cfg.get("track_jump_threshold_m",
                        self._track_jump_threshold_m))
            # Task 3 cooperative summon config.
            self._coop_summon = bool(cfg.get("cooperative_summon",
                                             self._coop_summon))
            self._sector_commitment = bool(cfg.get("sector_commitment",
                                                   self._sector_commitment))
            self._dwell_target_s = float(cfg.get("dwell_target_s",
                                                 self._dwell_target_s))
            self._track_standoff_m = float(cfg.get("track_standoff_m",
                                                  self._track_standoff_m))
            self._track_orbit_radius_m = float(
                cfg.get("track_orbit_radius_m", self._track_orbit_radius_m))
        self._configured = True

    def set_fleet_index(self, index: int, size: int) -> None:
        """Tell this controller which sector (index) of a fleet of ``size``
        it owns. Assigned by run.py after discovering the UAV list."""
        self._fleet_index = max(0, int(index))
        self._fleet_size = max(1, int(size))

    def _effective_fleet_index(self) -> int:
        """Fleet index after sector rotation (Task 3): the wedge this UAV is
        currently searching, = (base index + rotation offset) mod fleet size.
        Rotation lets a targetless UAV move to a wedge that contains a target."""
        if self._fleet_size <= 1:
            return 0
        return (self._fleet_index + self._sector_offset) % self._fleet_size

    def _maybe_rotate_sector(self, state: MultiSimState) -> None:
        """If this UAV has not found a REAL target for reassign_period_s,
        rotate to the next sector wedge so it eventually searches a wedge
        that contains a target (the full scenario's targets are not
        one-per-sector). No-op once a REAL target is being held."""
        if self._cur_real_key is not None:
            return  # holding a real target — do not rotate
        sim_t = state.sim_time
        if self._search_start_sim_time is None:
            self._search_start_sim_time = sim_t
        # "Last real" = the last sim_time the classifier confirmed REAL, or
        # the search start if never confirmed.
        ref = self._last_real_sim_time if self._last_real_sim_time is not None \
            else self._search_start_sim_time
        if sim_t - ref >= self._reassign_period_s:
            self._sector_offset = (self._sector_offset + 1) % max(1, self._fleet_size)
            self._last_real_sim_time = sim_t  # give the new wedge a full period

    def set_sector_center(self, lat: float, lon: float) -> None:
        """Override the search-area centre (e.g. from the first frame's UAV
        centroid when the config leaves it null)."""
        if self._sector_params is not None:
            # SectorSearchParams is a frozen-friendly dataclass; rebuild it.
            self._sector_params = SectorSearchParams(
                base_lat=float(lat), base_lon=float(lon),
                base_alt=self._sector_params.base_alt,
                search_radius=self._sector_params.search_radius,
                expand_time=self._sector_params.expand_time,
                sector_angular_speed_dps=self._sector_params.sector_angular_speed_dps,
                initial_radius_frac=self._sector_params.initial_radius_frac,
                radius_dither_frac=self._sector_params.radius_dither_frac,
                search_sweep_time=self._sector_params.search_sweep_time,
            )

    def reset(self) -> None:
        self._fsm.reset()
        self._peer_tracking.clear()
        self._last_broadcast_t = -1e9
        self.misid_track_ticks = 0
        self._effective_mode = "SEARCH"
        self._track_anchor_lat = None
        self._track_anchor_lon = None
        self._search_t0 = None
        self._clf = None
        self._verify_clf = None
        self._verify_pos = None
        self._sector_offset = 0
        self._last_real_sim_time = None
        self._real_track_active = False
        self._real_target_key = None
        self._search_start_sim_time = None
        self._known_real.clear()
        self._self_done.clear()
        self._decoy_avoid.clear()
        self._cur_real_key = None
        self._dwell_acc = 0.0
        self._last_r_broadcast_t = -1e9
        self._smooth_lat = None
        self._smooth_lon = None
        self._last_pan = 0.0
        self._last_tilt = -45.0
        self._last_detected_sim_time = None
        self._decoy_cooldown_until = -1e9
        self._decoy_avoid_pan = None
        self._decoy_cooldowns.clear()
        self._real_vel_lat_dps = 0.0
        self._real_vel_lon_dps = 0.0
        self._real_last_meas_t = None

    def _elapsed(self, sim_time: float) -> float:
        """Elapsed search time since the first decide() call (>=0).

        The engine's sim_time is a large negative epoch value; the search
        geometry needs a t that grows from 0, so we anchor on the first
        frame we see."""
        if self._search_t0 is None:
            self._search_t0 = sim_time
        return max(0.0, sim_time - self._search_t0)

    @property
    def mode(self) -> str:
        """Effective mode: what the UAV is ACTUALLY doing this tick.

        This may differ from the internal FSM mode when the cooperation
        yield overrides a TRACK decision back to SEARCH behaviour.
        Event publishing (run.py) reads this property.
        """
        return self._effective_mode

    def decide(self, state: MultiSimState, dt: float) -> list[dict[str, Any]]:
        """Decide commands for THIS UAV given the full multi-entity state.

        Returns a list of publishable dicts (mix of control + comm).
        """
        if not self._configured or not self.my_uid:
            return []
        me = state.entities.get(self.my_uid)
        if me is None or me.uav is None:
            return []  # self not present this tick

        # 1) Consume comm inbox: learn which targets peers are tracking.
        self._consume_inbox(me)

        # 1b) Sector rotation: if this UAV has not found a REAL target in a
        #     while, rotate its search wedge so it eventually covers a wedge
        #     that contains a target (Task 3: targets are not one-per-sector).
        if not self._coop_summon:
            self._maybe_rotate_sector(state)

        # 2) Drive the FSM (consecutive-detect counting + SEARCH<->TRACK
        #    transitions) by feeding it a 016-style view of self. We always
        #    call decide() so the mode machine advances; whether we keep its
        #    geometric output depends on whether sector search is enabled.
        #
        #    Track-anchor (target-identity) check: when we are already in
        #    TRACK and have an anchor, a sudden jump in detection.target_position
        #    means the auto-track gimbal silently switched to a different
        #    nearby entity (decoy/target). The auto-track keeps detected=True,
        #    so the FSM's _consecutive_lost would never accumulate. To make
        #    the loss observable to the FSM (and therefore to run.py's
        #    state.exit_track event publication), we synthesise a detected=False
        #    view for THIS tick. The anchor itself is held until k_lost
        #    confirms the loss; if the gimbal slews back to the original
        #    target the next tick, the FSM's hysteresis absorbs it.
        was_tracking_pre = (self._fsm.mode == "TRACK")
        # Note: the track-anchor spoof-loss (_maybe_spoof_loss) is DISABLED.
        # It was added for the auto_track gimbal silently switching targets,
        # but with auto_track OFF the gimbal no longer auto-switches, and the
        # spoof-loss falsely fired on every decoy↔target nearest-switch
        # (decoys within 80 m), destabilising real-target tracking. The
        # decoy classifier now handles identity (release decoys) instead.
        view_me = me
        # Search-stage decoy gate: any detection whose C++-reported target_type
        # is "decoy_vehicle" must not cause the FSM to enter TRACK.  We
        # additionally reject detections inside an active decoy cooldown
        # zone (so a position that was previously classified as a decoy
        # continues to be rejected even if its target_type flips briefly).
        # Once _real_track_active is latched, we trust the live tracking
        # path and stop gating so the in-progress track is not undone.
        det = view_me.detection
        search_decoy_hit = (
            det is not None and det.detected and det.target_position is not None
            and (getattr(det, "target_type", "") == "decoy_vehicle"
                 or self._detection_in_decoy_cooldown(view_me, state.sim_time))
        )
        if not self._real_track_active and search_decoy_hit:
            view_me = self._spoof_detection_lost(view_me)
            # Force the FSM back to SEARCH and reset accumulators so the
            # engine's continuing decoy-zone detections cannot push it into
            # TRACK again.  This breaks the k_acquire/k_lost enter/exit
            # TRACK loop that was visible on the page.
            self._force_search()
        # Real-target identity lock: while we have a confirmed real track,
        # SEARCH-stage detections whose position key does not match the real
        # target key are spoofed.  This stops the FSM from toggling in and
        # out of TRACK when the engine's nearest-target selection switches
        # between the real target and a nearby decoy/vehicle.
        if self._real_track_active and not was_tracking_pre:
            det = view_me.detection
            if det is not None and det.detected and det.target_position is not None:
                key = _pos_key(det.target_position.latitude,
                               det.target_position.longitude)
                if self._real_target_key is not None and key != self._real_target_key:
                    view_me = self._spoof_detection_lost(view_me)
                    self._force_search()
        # Sector-commitment filter (implicit task allocation): confines each
        # UAV to its angular wedge. Disabled in coop-summon mode OR when the
        # scenario's targets move across sectors (sector_commitment=false).
        if self._sector_commitment and not self._coop_summon:
            view_me = self._maybe_sector_filter(view_me, was_tracking_pre,
                                                state.sim_time)
            view_me = self._maybe_track_decoy_gate(view_me, was_tracking_pre,
                                                   state.sim_time)
        else:
            # Coop mode: the FSM must NOT enter TRACK from raw detections
            # (that orbits decoys). We verify targets in flight and commit
            # via cur_real_key; the committed track commands are produced
            # directly (not via the FSM). Feed not-detected so the FSM stays
            # quiescent in SEARCH.
            view_me = self._spoof_not_detected(view_me)
        my_sim_state = self._to_sim_state(state, view_me)
        fsm_cmds = self._fsm.decide(my_sim_state, dt)

        # Manage anchor across the FSM's own state machine transitions.
        self._update_track_anchor(was_tracking_pre, me)

        # 3) Track misid metrics (FR-015): if we're tracking a decoy, count it.
        if (me.detection is not None and me.detection.detected
                and me.detection.misid_flag):
            self.misid_track_ticks += 1

        # 4) Task 3 cooperative summon: classify the tracked target (reject
        #    decoys), record/broadcast confirmed-real targets, self-complete
        #    targets after dwelling, and pick a real target to converge on
        #    when this UAV is free. `converge_target` is (lat, lon) or None.
        converge_target = self._coop_step(state, me, was_tracking_pre, dt)

        # 4b) Non-cooperative (sector-spread) mode: still reject decoys while
        #     in TRACK so each UAV holds its sector's REAL target (not a
        #     decoy) — this is what makes K=1 completion robust in the decoy
        #     scenario without needing inter-UAV convergence.
        if not self._coop_summon:
            self._noncoop_track_reject(state, me, was_tracking_pre)

        # 4c) Maintain a committed real-target lock (both modes): follow a
        #     moving target, and (coop only) self-complete after dwelling.
        if self._coop_summon and self._cur_real_key is not None:
            self._committed_update(state, me, dt)

        # 5) Geometric leg.
        #    Priority when cooperative-summon is on:
        #      a) Committed to a confirmed-real target -> fly to its locked
        #         position + LOS (breaks the decoy trap).
        #      b) TRACK -> FSM loiter + LOS aim.
        #      c) SEARCH + known real target -> converge.
        #      d) SEARCH + sector search -> sector sweep.
        if self._coop_summon and self._cur_real_key is not None:
            # Cooperative-summon mode only: committed to a confirmed real target.
            # The default sector-spread mode keeps using the FSM/LoiterTracker
            # live-detection path so tracking stays responsive to moving targets.
            self._fsm._mode = "TRACK"
            cmds = self._committed_track_commands(state, me)
        elif self._real_track_active and not self._coop_summon:
            # Sector-spread mode with latched real track: the FSM is held in
            # SEARCH above so it cannot toggle on its own.  Drive the TRACK
            # commands directly from the live (post-gate) detection so we
            # follow the real target without going through the FSM's
            # k_acquire/k_lost accumulators.
            self._fsm._mode = "TRACK"
            cmds = self._track_commands_coop(state, view_me, fsm_cmds)
        elif self._coop_summon:
            # SEARCH (FSM held SEARCH by the not-detected spoof): fly the
            # full-circle search waypoint, and hold the gimbal on the
            # detected target (LOS) to verify it in flight — or sweep.
            cmds = self._search_commands_coop(state, me, converge_target)
        elif self._fsm.mode == "TRACK":
            # Use the gated entity view here.  If a detection is a previously
            # identified decoy, view_me has been spoofed as not-detected before
            # the FSM command path sees it, so we do not point the gimbal at
            # that decoy again.
            cmds = self._track_commands_coop(state, view_me, fsm_cmds)
        elif (self._use_sector_search and self._sector_params is not None
                and self._fsm.mode == "SEARCH"):
            cmds = self._search_commands_sector(state.sim_time)
        else:
            cmds = fsm_cmds

        # Update effective mode to match actual behaviour this tick.
        self._effective_mode = self._fsm.mode

        # 6) Build the publishable command list (inject unique_id so the
        #    C++ per-uid command router dispatches to THIS UAV).
        out: list[dict[str, Any]] = []
        for c in cmds:
            d = c.to_publish()
            d["unique_id"] = self.my_uid
            out.append(d)

        # 7) Cooperation broadcast.
        #    Cooperative-summon mode: broadcast the confirmed-real target's
        #    position ("R:<lat>,<lon>") so peers converge to co-track it.
        #    Legacy mode: broadcast "T:?" tracking heartbeat.
        if self._coop_summon and self._cur_real_key is not None:
            rlat, rlon, _ = self._known_real[self._cur_real_key]
            if state.sim_time - self._last_r_broadcast_t >= self._broadcast_period:
                self._last_r_broadcast_t = state.sim_time
                out.append(broadcast(
                    self.my_uid, f"R:{rlat:.4f},{rlon:.4f}"
                ).to_publish())
        elif self._fsm.mode == "TRACK" and not self._coop_summon:
            if state.sim_time - self._last_broadcast_t >= self._broadcast_period:
                self._last_broadcast_t = state.sim_time
                out.append(broadcast(self.my_uid, "T:?").to_publish())
        return out

    def _ema_smooth(self, lat: float, lon: float, alpha: float = 0.3
                    ) -> tuple[float, float]:
        """Apply exponential moving average to (lat, lon) and return the
        smoothed value.  Reduces gimbal-angle jumps when detection's
        target_position alternates between real vehicles and decoys."""
        if self._smooth_lat is None:
            self._smooth_lat = lat
            self._smooth_lon = lon
        else:
            self._smooth_lat += alpha * (lat - self._smooth_lat)
            self._smooth_lon += alpha * (lon - self._smooth_lon)
        return self._smooth_lat, self._smooth_lon

    def _remember_gimbal_from_cmds(self, cmds: list[ControlCommand]) -> None:
        """Remember the actual gimbal command emitted this tick.

        TRACK often delegates to the composed 016 FSM.  The FSM may smooth
        target_position internally, so the last-known hold angle must come
        from the command we actually publish, not from a separately computed
        raw LOS angle.
        """
        for cmd in reversed(cmds):
            if cmd.cmd != "component.gimbal_tracking.set_orientation":
                continue
            if "pan" in cmd.params and "tilt" in cmd.params:
                self._last_pan = float(cmd.params["pan"])
                self._last_tilt = float(cmd.params["tilt"])
                return

    def _spoof_detection_lost(self, me: EntityState) -> EntityState:
        det = me.detection
        if det is None:
            return me
        return replace(me, detection=replace(
            det, detected=False, target_position=None, azimuth_error_deg=None))

    def _is_avoided_decoy_detection(self, me: EntityState,
                                     sim_time: float | None = None) -> bool:
        det = me.detection
        if det is None or not det.detected or det.target_position is None:
            return False
        tp = det.target_position
        if _pos_key(tp.latitude, tp.longitude) in self._decoy_avoid:
            return True
        if sim_time is None:
            return False
        # Spatial cooldown: tolerate small reported-position changes around
        # the same rejected decoy.  Expired zones are pruned lazily.
        active = []
        hit = False
        for lat, lon, expire_t in self._decoy_cooldowns:
            if sim_time > expire_t:
                continue
            active.append((lat, lon, expire_t))
            if haversine_m(lat, lon, tp.latitude, tp.longitude) <= self._decoy_cooldown_radius_m:
                hit = True
        self._decoy_cooldowns = active
        return hit

    def _detection_in_decoy_cooldown(self, me: EntityState,
                                      sim_time: float) -> bool:
        det = me.detection
        if det is None or not det.detected or det.target_position is None:
            return False
        return self._point_in_decoy_cooldown(det.target_position.latitude,
                                             det.target_position.longitude,
                                             sim_time)

    def _point_in_decoy_cooldown(self, lat: float, lon: float,
                                 sim_time: float) -> bool:
        active = []
        hit = False
        for dlat, dlon, expire_t in self._decoy_cooldowns:
            if sim_time > expire_t:
                continue
            active.append((dlat, dlon, expire_t))
            if haversine_m(dlat, dlon, lat, lon) <= self._decoy_cooldown_radius_m:
                hit = True
        self._decoy_cooldowns = active
        return hit

    def _push_point_out_of_decoy_cooldown(self, lat: float, lon: float,
                                          sim_time: float) -> tuple[float, float]:
        for dlat, dlon, expire_t in list(self._decoy_cooldowns):
            if sim_time > expire_t:
                continue
            d = haversine_m(dlat, dlon, lat, lon)
            if d > self._decoy_cooldown_radius_m:
                continue
            bearing = bearing_deg(dlat, dlon, lat, lon) if d > 1.0 else 0.0
            return destination_point(dlat, dlon, bearing,
                                     self._decoy_cooldown_radius_m + 80.0)
        return lat, lon

    def _mark_decoy_released(self, state: MultiSimState,
                             me: EntityState, lat: float, lon: float) -> None:
        """Remember a rejected decoy and its current bearing so SEARCH does
        not immediately sweep back onto it and re-enter TRACK."""
        expire_t = state.sim_time + self._decoy_cooldown_s
        self._decoy_avoid.add(_pos_key(lat, lon))
        self._decoy_cooldowns.append((lat, lon, expire_t))
        self._decoy_cooldown_until = expire_t
        if me.uav is not None:
            self._decoy_avoid_pan, _ = los_angles(
                me.uav.position.latitude, me.uav.position.longitude,
                me.uav.position.altitude, me.uav.attitude.yaw,
                lat, lon, 0.0,
            )

    def _avoid_decoy_pan(self, pan: float) -> float:
        if self._decoy_avoid_pan is None:
            return pan
        delta = pan - self._decoy_avoid_pan
        while delta > 180.0:
            delta -= 360.0
        while delta < -180.0:
            delta += 360.0
        if abs(delta) >= self._decoy_avoid_margin_deg:
            return pan
        step = self._decoy_avoid_margin_deg if delta >= 0.0 else -self._decoy_avoid_margin_deg
        adjusted = self._decoy_avoid_pan + step
        while adjusted > 180.0:
            adjusted -= 360.0
        while adjusted < -180.0:
            adjusted += 360.0
        return adjusted

    def _detection_is_decoy_like(self, det) -> bool:
        return bool(getattr(det, "misid_flag", False) or
                    getattr(det, "target_type", "") == "decoy_vehicle")

    def _real_predicted_pos(self, sim_time: float) -> tuple[float, float] | None:
        key = self._cur_real_key
        if key is None or key not in self._known_real:
            return None
        lat, lon, last_t = self._known_real[key]
        dt = sim_time - last_t
        if dt <= 0.0 or dt > 5.0:
            return lat, lon
        return (lat + self._real_vel_lat_dps * dt,
                lon + self._real_vel_lon_dps * dt)

    def _predict_real_filter(self, sim_time: float) -> None:
        """Advance the confirmed-real filter using its velocity estimate."""
        key = self._cur_real_key
        pred = self._real_predicted_pos(sim_time)
        if key is None or pred is None:
            return
        self._known_real[key] = (pred[0], pred[1], sim_time)

    def _real_coast_expired(self, sim_time: float) -> bool:
        return (self._real_last_meas_t is not None and
                sim_time - self._real_last_meas_t > self._real_coast_timeout_s)

    def _update_real_filter(self, sim_time: float, lat: float, lon: float) -> bool:
        """Initialize/update the confirmed-real target filter.

        Accepted real detections continuously correct position and velocity.
        During short decoy/misid gaps, _predict_real_filter() keeps the target
        moving; after a timeout we release instead of flying to empty space.
        """
        key = self._cur_real_key
        if key is None or key not in self._known_real:
            key = _pos_key(lat, lon)
            self._cur_real_key = key
            self._known_real[key] = (lat, lon, sim_time)
            self._real_vel_lat_dps = 0.0
            self._real_vel_lon_dps = 0.0
            self._real_last_meas_t = sim_time
            return True
        old_lat, old_lon, old_t = self._known_real[key]
        dt = sim_time - old_t
        if dt <= 1e-6 or dt > 5.0:
            self._known_real[key] = (lat, lon, sim_time)
            self._real_vel_lat_dps = 0.0
            self._real_vel_lon_dps = 0.0
            self._real_last_meas_t = sim_time
            return True

        speed = haversine_m(old_lat, old_lon, lat, lon) / max(dt, 1.0)
        if speed > self._real_max_speed_mps:
            return False

        # Alpha-beta update: predict to measurement time, then use the
        # residual to correct both position and velocity.  This follows moving
        # targets with much less steady-state lag than a pure position EMA.
        pred_lat = old_lat + self._real_vel_lat_dps * dt
        pred_lon = old_lon + self._real_vel_lon_dps * dt
        res_lat = lat - pred_lat
        res_lon = lon - pred_lon
        a = self._real_filter_alpha
        b = self._real_filter_beta
        self._known_real[key] = (
            pred_lat + a * res_lat,
            pred_lon + a * res_lon,
            sim_time,
        )
        self._real_vel_lat_dps += b * res_lat / dt
        self._real_vel_lon_dps += b * res_lon / dt
        self._real_last_meas_t = sim_time
        return True

    # ── helpers ────────────────────────────────────────────────────────────

    def _maybe_sector_filter(self, me: EntityState,
                             was_tracking: bool,
                             sim_time: float = 0.0) -> EntityState:
        """While SEARCHING, drop detections of targets outside this UAV's
        sector so the FSM never accumulates k_acquire for (and thus never
        commits to) a target another UAV owns.

        No-op once already in TRACK (a committed target is kept even if it
        drifts across a sector boundary), and no-op when there is no
        detection, no target position, or no sector geometry configured.
        """
        if was_tracking:
            return me
        det = me.detection
        if det is None or not det.detected or det.target_position is None:
            return me
        # Do not let a target we already classified as DECOY immediately
        # reacquire TRACK while the SEARCH sweep still intersects it.
        if self._is_avoided_decoy_detection(me, sim_time):
            return self._spoof_detection_lost(me)
        if self._fleet_size <= 1:
            return me  # single UAV: no sector partition
        if self._sector_params is None:
            return me
        base_lat = self._sector_params.base_lat
        base_lon = self._sector_params.base_lon
        if base_lat == 0.0 and base_lon == 0.0:
            return me  # sector centre not set yet
        eff = self._effective_fleet_index()
        step = 360.0 / self._fleet_size
        lo = (eff % self._fleet_size) * step
        hi = lo + step
        brg = bearing_deg(base_lat, base_lon,
                          det.target_position.latitude,
                          det.target_position.longitude)
        if lo <= brg < hi:
            return me  # target is in my sector — keep the detection
        # Out of sector: spoof not-detected so the FSM keeps searching and
        # does not lock a target a peer will own.
        spoofed_det = replace(det, detected=False, target_position=None,
                              azimuth_error_deg=None)
        return replace(me, detection=spoofed_det)

    def _maybe_track_decoy_gate(self, me: EntityState,
                                was_tracking: bool,
                                sim_time: float = 0.0) -> EntityState:
        """Before feeding TRACK detections to the FSM, suppress obvious decoys.

        The classifier runs later and needs a time window, but the FSM command
        for this tick would already be computed from raw detection.  If the
        engine reports a decoy/misid or a previously rejected decoy while we
        are already tracking, spoof it as not-detected so _track_commands_coop
        holds the last real target instead of steering toward the decoy.
        """
        if not was_tracking:
            return me
        det = me.detection
        if det is None or not det.detected or det.target_position is None:
            return me
        if self._is_avoided_decoy_detection(me, sim_time) or self._detection_is_decoy_like(det):
            return self._spoof_detection_lost(me)
        if self._fsm._tracker is not None:
            last_lat, last_lon = self._fsm._tracker._current_target
            if last_lat != 0.0 or last_lon != 0.0:
                d = haversine_m(last_lat, last_lon,
                                det.target_position.latitude,
                                det.target_position.longitude)
                if d > self._track_jump_threshold_m:
                    return self._spoof_detection_lost(me)
        return me

    def _maybe_spoof_loss(self, me: EntityState,
                          was_tracking: bool) -> EntityState:
        """If the detection's reported target jumped >threshold from the
        anchor we set when entering TRACK, return a view of ``me`` with
        ``detection.detected=False``. Otherwise return ``me`` unchanged.

        Only fires while already in TRACK (was_tracking) and only when
        we have both an anchor and a current detection position to
        compare against — so SEARCH-mode detections, which legitimately
        sweep across many candidates, are never spoofed.
        """
        if not was_tracking:
            return me
        if (self._track_anchor_lat is None or
                self._track_anchor_lon is None):
            return me
        det = me.detection
        if det is None or not det.detected or det.target_position is None:
            return me
        d = haversine_m(
            self._track_anchor_lat, self._track_anchor_lon,
            det.target_position.latitude, det.target_position.longitude,
        )
        if d <= self._track_jump_threshold_m:
            return me
        # Jumped: synthesise a "lost" detection. ExtendedDetection is a
        # frozen dataclass, so use replace() to build a sibling. Drop
        # target_position too — _track_commands_coop falls back to the
        # last known position from _fsm._tracker, which is what we want.
        spoofed_det = replace(
            det, detected=False, target_position=None,
            azimuth_error_deg=None,
        )
        return replace(me, detection=spoofed_det)

    def _update_track_anchor(self, was_tracking: bool,
                             me: EntityState) -> None:
        """Maintain the track anchor across FSM mode transitions.

        - SEARCH→TRACK edge (just entered TRACK this tick, with a real
          detection position): seed anchor.
        - Already in TRACK with a fresh in-tolerance detection: roll the
          anchor forward so a moving target stays anchored to its current
          position (otherwise legitimate target motion would eventually
          exceed the jump threshold).
        - TRACK→SEARCH edge: clear anchor.
        """
        is_tracking = (self._fsm.mode == "TRACK")
        det = me.detection
        det_pos = det.target_position if (det and det.detected) else None
        if not was_tracking and is_tracking and det_pos is not None:
            self._track_anchor_lat = det_pos.latitude
            self._track_anchor_lon = det_pos.longitude
            return
        if was_tracking and not is_tracking:
            self._track_anchor_lat = None
            self._track_anchor_lon = None
            return
        if (was_tracking and is_tracking and det_pos is not None
                and self._track_anchor_lat is not None
                and self._track_anchor_lon is not None):
            d = haversine_m(
                self._track_anchor_lat, self._track_anchor_lon,
                det_pos.latitude, det_pos.longitude,
            )
            if d <= self._track_jump_threshold_m:
                # Roll anchor with the moving target.
                self._track_anchor_lat = det_pos.latitude
                self._track_anchor_lon = det_pos.longitude

    def _track_commands_coop(self, state: MultiSimState, me: EntityState,
                             fsm_cmds: list[ControlCommand]
                             ) -> list[ControlCommand]:
        """TRACK commands for the cooperative controller.

        Overrides the FSM's _track_commands to fix two issues:
        1. When detection is lost mid-track, the FSM only emits a gimbal
           command (no set_destination), so the UAV stops moving. We keep
           flying toward the last known target position so the UAV can
           reacquire the target.
        2. The LoiterTracker.reset() is called with the FSM's base point
           (takeoff position) instead of the target position, so the
           initial loiter orbit is wrong. We fix this by re-initializing
           the tracker with the actual target position on first detection.
        """
        det = me.detection
        if det is not None and det.detected and det.target_position is not None:
            # Have detection — use FSM commands (which include loiter +
            # gimbal LOS). But fix the tracker's initial center if this is
            # the first detection since entering TRACK.
            if self._fsm._tracker is not None:
                tpos = det.target_position
                # If the tracker was never refreshed (base still at
                # takeoff point), seed it with the actual target position.
                if (self._fsm._tracker._last_refresh is None
                        and self._fsm._tracker.base_lat == self._fsm._base_lat
                        and self._fsm._tracker.base_lon == self._fsm._base_lon):
                    self._fsm._tracker.reset(tpos.latitude, tpos.longitude)
            self._remember_gimbal_from_cmds(fsm_cmds)
            return fsm_cmds
        # Lost detection — FSM only emits gimbal hold. We MUST also keep
        # the UAV flying toward the last known target position so it can
        # reacquire. Without set_destination the UAV just hovers in place.
        if self._fsm._tracker is not None:
            last_tgt = self._fsm._tracker._current_target
            if last_tgt != (0.0, 0.0):
                if self._point_in_decoy_cooldown(last_tgt[0], last_tgt[1],
                                                 state.sim_time):
                    if self._use_sector_search and self._sector_params is not None:
                        return self._search_commands_sector(state.sim_time)
                cmds = [
                    ControlCommand(
                        target=CommandTarget.UAV,
                        cmd="set_destination",
                        params={
                            "latitude": last_tgt[0],
                            "longitude": last_tgt[1],
                            "altitude": me.uav.position.altitude,
                            "loiter_radius": self._fsm._loiter_radius,
                        },
                    ),
                    ControlCommand(
                        target=CommandTarget.UAV,
                        cmd="component.gimbal_tracking.set_orientation",
                        params={"pan": self._last_pan, "tilt": self._last_tilt},
                    ),
                ]
                self._remember_gimbal_from_cmds(cmds)
                return cmds
        # No tracker info — fall back to FSM commands, but still remember the
        # actual gimbal command so the next flicker can hold it.
        self._remember_gimbal_from_cmds(fsm_cmds)
        return fsm_cmds

    def _search_commands_sector(self, sim_time: float) -> list[ControlCommand]:
        """Sector-divided SEARCH commands for THIS UAV.

        Produces a set_destination toward this UAV's sector waypoint plus a
        gimbal sweep (mirrors the FSM's sweep so the camera footprint
        covers both sides of the flight path). Replaces the FSM's
        single-centre spiral which left the whole fleet circling on top of
        each other.
        """
        assert self._sector_params is not None
        t = self._elapsed(sim_time)
        lat, lon, alt = sector_waypoint(
            t, self._sector_params,
            self._effective_fleet_index(), self._fleet_size,
        )
        lat, lon = self._push_point_out_of_decoy_cooldown(lat, lon, sim_time)
        pan, tilt = sweep_orientation(
            t,
            period=self._sweep_period,
            pitch_min=self._sweep_pitch_min,
            pitch_max=self._sweep_pitch_max,
        )
        if sim_time <= self._decoy_cooldown_until:
            if self._decoy_avoid_pan is not None:
                # During decoy cooldown, do not keep sweeping across the same
                # bearing.  Hold the camera on the opposite side until the
                # cooldown expires so the page does not repeatedly point at
                # the rejected decoy.
                pan = self._decoy_avoid_pan + 180.0
                while pan > 180.0:
                    pan -= 360.0
                while pan < -180.0:
                    pan += 360.0
                tilt = (self._sweep_pitch_min + self._sweep_pitch_max) * 0.5
            else:
                pan = self._avoid_decoy_pan(pan)
        return [
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="set_destination",
                params={"latitude": lat, "longitude": lon, "altitude": alt},
            ),
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="component.gimbal_tracking.set_orientation",
                params={"pan": pan, "tilt": tilt},
            ),
        ]

    def _noncoop_track_reject(self, state: MultiSimState, me: EntityState,
                              was_tracking_pre: bool) -> None:
        """Sector-spread mode: while in TRACK, classify the target and release
        a decoy so the UAV keeps searching for a REAL target. A REAL target
        (which moves) is confirmed quickly; a decoy whose reported position
        JUMPS between neighbours (shifting nearest) would otherwise fake a
        large span and read as REAL, so we reset the classifier on a >120 m
        jump and time out (release) anything not confirmed REAL within 3 s."""
        if self._fsm.mode != "TRACK":
            return
        det = me.detection
        if (det is None or not det.detected or det.target_position is None):
            return
        tp = det.target_position
        if self._coop_summon and self._cur_real_key is not None:
            # Cooperative-summon committed track: raw detection no longer feeds
            # the classifier/release path. _committed_update() owns it.
            return
        # Reset the classifier if the detection jumped to a different entity.
        if (self._clf is not None and self._clf.samples
                and self._clf.started_at is not None):
            last_lat = self._clf.samples[-1][1]
            last_lon = self._clf.samples[-1][2]
            if haversine_m(last_lat, last_lon, tp.latitude, tp.longitude) > 120.0:
                self._clf = None
        if not was_tracking_pre or self._clf is None:
            self._clf = DecoyClassifier(move_threshold_m=5.0,
                                        min_window_s=1.5, min_samples=10)
        decision = self._clf.observe(state.sim_time, tp.latitude, tp.longitude)
        if decision == "decoy":
            self._mark_decoy_released(state, me, tp.latitude, tp.longitude)
            self._clf = None
            self._force_search()
            return
        if decision == "real":
            # Sector-spread (default) mode keeps using the composed FSM's
            # live-detection tracking path.  A REAL decision only records that
            # this UAV is not targetless (for sector rotation); it must not
            # switch into the committed filter/standoff controller, which was
            # less responsive to moving targets.
            self._last_real_sim_time = state.sim_time
            self._real_track_active = True
            self._real_target_key = _pos_key(tp.latitude, tp.longitude)
            if self._coop_summon:
                self._update_real_filter(state.sim_time, tp.latitude, tp.longitude)
        # Timeout: a moving REAL target is confirmed fast. If we've been
        # verifying >3 s with no REAL decision, this is a decoy the classifier
        # can't settle (jumping) — release it and keep searching.
        if (decision is None and self._clf.started_at is not None
                and state.sim_time - self._clf.started_at > 3.0):
            self._mark_decoy_released(state, me, tp.latitude, tp.longitude)
            self._clf = None
            self._force_search()

    def _spoof_not_detected(self, me: EntityState) -> EntityState:
        """Return a view of ``me`` with detection.detected=False (and no
        target_position). Used in coop mode so the FSM never enters TRACK
        from a raw (decoy-contaminated) detection."""
        det = me.detection
        if det is None:
            return me
        return replace(me, detection=replace(
            det, detected=False, target_position=None, azimuth_error_deg=None))

    def _force_search(self) -> None:
        """Revert the FSM to SEARCH (release the current target)."""
        self._fsm._mode = "SEARCH"
        self._fsm._consecutive_detected = 0
        self._fsm._tracker = None
        self._track_anchor_lat = None
        self._track_anchor_lon = None
        self._cur_real_key = None
        self._real_vel_lat_dps = 0.0
        self._real_vel_lon_dps = 0.0
        self._real_track_active = False
        self._real_target_key = None

    def _committed_update(self, state: MultiSimState, me: EntityState,
                          dt: float) -> None:
        """Maintain the committed real-target lock (both modes): refresh the
        locked position with an in-range live detection so the lock follows a
        moving target (never drifting onto a decoy >200 m away). In
        cooperative mode also self-complete after dwelling so the fleet works
        targets sequentially; in sector-spread mode the UAV just holds the
        target (K=1 completion is permanent)."""
        if self._cur_real_key is None:
            return
        sim_t = state.sim_time
        det = me.detection
        updated = False
        if det is not None and det.detected and det.target_position is not None:
            tp = det.target_position
            pred = self._real_predicted_pos(sim_t)
            rlat, rlon = pred if pred is not None else self._known_real[self._cur_real_key][:2]
            d = haversine_m(rlat, rlon, tp.latitude, tp.longitude)
            if self._detection_is_decoy_like(det) or d > self._real_gate_m:
                self._mark_decoy_released(state, me, tp.latitude, tp.longitude)
            else:
                updated = self._update_real_filter(sim_t, tp.latitude, tp.longitude)
                if not updated:
                    self._mark_decoy_released(state, me, tp.latitude, tp.longitude)
        if not updated:
            if self._real_coast_expired(sim_t):
                self._force_search()
                return
            self._predict_real_filter(sim_t)
        if self._coop_summon:
            self._dwell_acc += dt
            if self._dwell_acc >= self._dwell_target_s:
                self._self_done.add(self._cur_real_key)
                self._cur_real_key = None
                self._dwell_acc = 0.0

    def _coop_step(self, state: MultiSimState, me: EntityState,
                   was_tracking_pre: bool, dt: float):
        """Task 3 cooperative-summon logic. Returns ``(lat, lon)`` to converge
        to when this UAV is free and a known real target exists, else None.

        Two phases:

        SEARCH (not committed): verify a detected target *in flight* — the
        gimbal is held on it (LOS) while the UAV keeps flying its search
        waypoint, so the fleet never *orbits* a decoy (that trap stranded
        every UAV on the centre decoys). A motion-confirmed REAL target is
        committed; a DECOY is rejected and the verify classifier resets.

        COMMITTED (cur_real_key set): the geometric leg flies to the locked
        position and keeps the gimbal on it; here we refresh the lock with
        in-range live detections (following a moving target), accumulate
        dwell, and self-complete after the dwell target so the fleet works
        targets sequentially.
        """
        if not self._coop_summon:
            return None
        # COMMITTED handling (lock follow + self-complete) is done by
        # _committed_update for both modes; here we only handle SEARCH.
        if self._cur_real_key is not None:
            return None
        sim_t = state.sim_time
        det = me.detection
        det_pos = None
        if det is not None and det.detected and det.target_position is not None:
            det_pos = (det.target_position.latitude, det.target_position.longitude)

        # SEARCH: verify the detected target in flight.
        if det_pos is not None:
            # Reset the verify classifier if the detection jumped to a
            # different target (>120 m away) so we measure ONE target's
            # motion, not a blend of two.
            if self._verify_clf is not None and self._verify_pos is not None \
                    and haversine_m(self._verify_pos[0], self._verify_pos[1],
                                    det_pos[0], det_pos[1]) > 120.0:
                self._verify_clf = None
            if self._verify_clf is None:
                self._verify_clf = DecoyClassifier(move_threshold_m=5.0,
                                                   min_window_s=1.5,
                                                   min_samples=10)
                self._verify_pos = det_pos
            decision = self._verify_clf.observe(sim_t, det_pos[0], det_pos[1])
            self._verify_pos = det_pos
            if decision == "real":
                key = _pos_key(det_pos[0], det_pos[1])
                if key in self._known_real:
                    # Already claimed by this UAV or a peer -> skip, keep
                    # searching so the fleet spreads across distinct targets.
                    self._verify_clf = None
                    self._verify_pos = None
                else:
                    # Claim the unclaimed real target and commit to it.
                    self._known_real[key] = (det_pos[0], det_pos[1], sim_t)
                    self._cur_real_key = key
                    self._dwell_acc = 0.0
                    self._verify_clf = None
                    self._verify_pos = None
            elif decision == "decoy":
                self._mark_decoy_released(state, me, det_pos[0], det_pos[1])
                self._verify_clf = None
                self._verify_pos = None
        else:
            self._verify_clf = None
            self._verify_pos = None

        # K=1 spread: do NOT converge (convergence clusters UAVs on one
        # target, which is for K>=2 co-tracking). Each UAV independently
        # claims an unclaimed real target via the dedup above, so the fleet
        # spreads across all real targets wherever they sit.
        return None

    def _active_real_target(self):
        """Earliest-seen known real target not yet self-completed, or None."""
        best = None
        best_t = None
        for key, (lat, lon, seen_t) in self._known_real.items():
            if key in self._self_done:
                continue
            if best_t is None or seen_t < best_t:
                best_t = seen_t
                best = (lat, lon)
        return best

    def _search_commands_converge(self, tgt_lat: float, tgt_lon: float,
                                  sim_time: float) -> list[ControlCommand]:
        """Fly toward a known real target to co-track it: set_destination to
        its position (loiter) + gimbal sweep so the camera reacquires it on
        arrival and the FSM re-enters TRACK."""
        t = self._elapsed(sim_time)
        pan, tilt = sweep_orientation(
            t, period=self._sweep_period,
            pitch_min=self._sweep_pitch_min, pitch_max=self._sweep_pitch_max,
        )
        base_alt = (self._sector_params.base_alt
                    if self._sector_params is not None else 300.0)
        return [
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="set_destination",
                params={
                    "latitude": tgt_lat,
                    "longitude": tgt_lon,
                    "altitude": base_alt,
                    "loiter_radius": self._fsm._loiter_radius,
                },
            ),
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="component.gimbal_tracking.set_orientation",
                params={"pan": pan, "tilt": tilt},
            ),
        ]

    def _search_commands_coop(self, state: MultiSimState, me: EntityState,
                              converge_target):
        """Coop SEARCH: fly the search (or converge) waypoint while holding
        the gimbal on a detected target (LOS — to verify it in flight) or
        sweeping to find one. Never orbit a raw detection (the FSM is held
        in SEARCH), so the fleet is not stranded orbiting centre decoys."""
        t = self._elapsed(state.sim_time)
        base_alt = (self._sector_params.base_alt
                    if self._sector_params is not None else 300.0)
        if converge_target is not None:
            wlat, wlon = converge_target
            loiter = self._fsm._loiter_radius
        elif self._sector_params is not None:
            # Full-circle expanding sweep (cooperative mode): every UAV covers
            # ALL bearings over time (phase-offset per UAV) so the fleet is
            # not confined to a fixed wedge — targets rarely sit one-per-
            # sector, and a wedge split would leave some UAVs targetless.
            # Combined with claim-and-dedup this spreads the fleet across
            # the real targets wherever they are.
            p = self._sector_params
            t = self._elapsed(state.sim_time)
            brng = (t * p.sector_angular_speed_dps
                    + self._fleet_index * (360.0 / max(1, self._fleet_size))
                    ) % 360.0
            r_min = p.search_radius * p.initial_radius_frac
            frac = min(1.0, t / max(1.0, p.expand_time))
            r = r_min + (p.search_radius - r_min) * frac
            if r < 1.0:
                r = 1.0
            wlat, wlon = destination_point(p.base_lat, p.base_lon, brng, r)
            loiter = 0
        else:
            wlat, wlon = me.uav.position.latitude, me.uav.position.longitude
            loiter = 0
        det = me.detection
        if det is not None and det.detected and det.target_position is not None:
            tp = det.target_position
            # EMA-smooth the detection position so decoy↔real jumps produce
            # gradual gimbal motion rather than a hard snap.
            slat, slon = self._ema_smooth(tp.latitude, tp.longitude)
            pan, tilt = los_angles(
                me.uav.position.latitude, me.uav.position.longitude,
                me.uav.position.altitude, me.uav.attitude.yaw,
                slat, slon, tp.altitude)
            self._last_pan = pan
            self._last_tilt = tilt
            self._last_detected_sim_time = state.sim_time
        else:
            # During SEARCH verification, detection can flicker when a decoy
            # misid roll alternates.  Hold the last LOS for a short grace
            # window instead of immediately snapping back to sweep.
            if (self._last_detected_sim_time is not None and
                    state.sim_time - self._last_detected_sim_time <= self._verify_hold_s):
                pan, tilt = self._last_pan, self._last_tilt
            else:
                pan, tilt = sweep_orientation(
                    t, period=self._sweep_period,
                    pitch_min=self._sweep_pitch_min,
                    pitch_max=self._sweep_pitch_max)
                if state.sim_time <= self._decoy_cooldown_until:
                    pan = self._avoid_decoy_pan(pan)
        params: dict[str, Any] = {
            "latitude": wlat, "longitude": wlon, "altitude": base_alt,
        }
        if loiter:
            params["loiter_radius"] = loiter
        return [
            ControlCommand(
                target=CommandTarget.UAV, cmd="set_destination", params=params),
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="component.gimbal_tracking.set_orientation",
                params={"pan": pan, "tilt": tilt},
            ),
        ]

    def _committed_track_commands(self, state: MultiSimState,
                                  me: EntityState) -> list[ControlCommand]:
        """Track the confirmed-real filter state instead of raw detection.

        Accepted real detections continuously update the filter, so the UAV
        follows moving targets.  Decoy/misid detections are rejected before
        they can pull the gimbal away from the filtered real target.
        """
        # Orbit the filtered real-target position. Using the raw live
        # detection would let a nearer decoy steal the orbit; the filter stays
        # on the confirmed real target and only accepts in-gate real updates.
        self._predict_real_filter(state.sim_time)
        rlat, rlon, _ = self._known_real[self._cur_real_key]
        uav = me.uav
        pan, tilt = los_angles(
            uav.position.latitude, uav.position.longitude,
            uav.position.altitude, uav.attitude.yaw,
            rlat, rlon, 0.0,
        )
        self._last_pan = pan
        self._last_tilt = tilt
        base_alt = (self._sector_params.base_alt
                    if self._sector_params is not None else uav.position.altitude)
        # Do not send the fixed-wing directly to the target centre: once it
        # overflies the point the camera must look almost straight down and the
        # target can leave the FOV.  Instead, keep a standoff orbit on the
        # current UAV side of the target while the gimbal points at target
        # centre.
        away_bearing = bearing_deg(rlat, rlon,
                                   uav.position.latitude, uav.position.longitude)
        wlat, wlon = destination_point(rlat, rlon, away_bearing,
                                       self._track_standoff_m)
        return [
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="set_destination",
                params={
                    "latitude": wlat,
                    "longitude": wlon,
                    "altitude": base_alt,
                    "loiter_radius": self._track_orbit_radius_m,
                },
            ),
            ControlCommand(
                target=CommandTarget.UAV,
                cmd="component.gimbal_tracking.set_orientation",
                params={"pan": pan, "tilt": tilt},
            ),
        ]

    def _consume_inbox(self, me: EntityState) -> None:
        """Parse incoming comm messages.

        Two payload formats:
          'T:<uid>'  — legacy: peer is tracking <uid> (best-effort).
          'R:<lat>,<lon>' — Task 3: peer confirmed a REAL target near
            (lat, lon). We record it so free UAVs can converge to co-track.
        """
        if me.comm is None:
            return
        for entry in me.comm.inbox:
            if entry.sender == self.my_uid:
                continue
            payload = entry.payload.strip()
            if payload.startswith("T:"):
                tgt = payload[2:].strip()
                if tgt:
                    self._peer_tracking[tgt] = entry.sender
            elif payload.startswith("R:") and self._coop_summon:
                rest = payload[2:].strip()
                parts = rest.split(",")
                if len(parts) == 2:
                    try:
                        lat = float(parts[0])
                        lon = float(parts[1])
                        key = _pos_key(lat, lon)
                        if key not in self._known_real:
                            self._known_real[key] = (lat, lon, entry.recv_time)
                    except ValueError:
                        pass

    def _tracked_target_uid(self, me: EntityState) -> str | None:
        """Best-effort: which target uid am I currently tracking?

        detection only gives position + type, not uid. We approximate by
        matching detection.target_position to the nearest vehicle's truth.
        Returns None if no match.
        """
        return None  # populated by run.py via set_known_targets if needed

    def _to_sim_state(self, state: MultiSimState,
                      me: EntityState) -> SimState:
        """Project this UAV's entity view into a 016 SimState for the FSM."""
        from examples.uav_search_track_car.search_track.state import (
            Detection, TargetState,
        )
        assert me.uav is not None and me.gimbal is not None
        # detection: use the extended detection's base fields.
        det = me.detection
        base_det = Detection(
            detected=det.detected if det else False,
            confidence=det.confidence if det else 0.0,
            target_position=det.target_position if det else None,
            azimuth_error_deg=det.azimuth_error_deg if det else None,
        )
        return SimState(
            sim_time=state.sim_time,
            timestamp=state.timestamp,
            status=state.status,
            uav=me.uav,
            gimbal=me.gimbal,
            detection=base_det,
            target_truth=None,  # FR-007: controllers never see ground truth
        )

def _pos_key(lat: float, lon: float) -> str:
    """Discretise a position to a ~11 m grid key (0.0001 deg) so the same
    target reported by multiple UAVs / ticks maps to one key."""
    return f"{round(lat, 4)},{round(lon, 4)}"


def tracked_uid_is_likely_shared(
        me: EntityState, peer_tracking: dict[str, str]) -> bool:
    """Heuristic: is the target I am detecting likely already tracked by a peer?

    Without a target uid in the detection, and without peer target positions
    in the legacy 'T:' protocol, this stays False. The cooperative-summon
    mode does not rely on it."""
    return False
