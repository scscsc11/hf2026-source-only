"""Spec 019 — lightweight per-entity view of a sim:state frame.

Reuses the parsing shape from spec 017 (`multi_state.parse_multi_sim_state`)
but exposes a small, focused API for the swarm controller: read UAV
position/HP/comm stats, and read the published `zones` bucket.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UavView:
    uid: str
    name: str
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    destroyed: bool = False
    jammed: bool = False
    comm_sent: int = 0
    comm_delivered: int = 0
    detected: bool = False
    misid_flag: bool = False
    target_type: str = ""
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    confidence: float = 0.0
    target_uid: Optional[str] = None


@dataclass
class GroundView:
    uid: str
    name: str
    latitude: float = 0.0
    longitude: float = 0.0
    is_decoy: bool = False


@dataclass
class ZoneView:
    """One published zone entry — used by the controller for blind avoidance."""
    type: str             # "air_defense" | "comm_jam_static" | "comm_jam_random"
    polygon: list         # [(lat, lon), ...]
    alt_min: float = 0.0
    alt_max: float = 1e9


@dataclass
class SwarmState:
    sim_time: float = 0.0
    status: str = "running"
    uavs: dict[str, UavView] = field(default_factory=dict)
    targets: dict[str, GroundView] = field(default_factory=dict)
    decoys: dict[str, GroundView] = field(default_factory=dict)
    zones: list[ZoneView] = field(default_factory=list)

    @property
    def n_alive(self) -> int:
        return sum(1 for u in self.uavs.values() if not u.destroyed)


def parse_swarm_state(raw: dict) -> SwarmState:
    """Read one sim:state frame into a SwarmState.

    The kernel now publishes a `zones` bucket; we read it through the
    blind-avoidance API.  Per-entity fields mirror the legacy 017 shape.
    """
    st = SwarmState()
    st.sim_time = float(raw.get("sim_time", raw.get("timestamp", 0.0)))
    st.status = str(raw.get("status", "running"))

    # Zones bucket: {air_defense: [...], comm_jam_static: [...], comm_jam_random: [...]}
    zones_obj = raw.get("zones", {}) or {}
    for kind in ("air_defense", "comm_jam_static", "comm_jam_random"):
        for z in zones_obj.get(kind, []) or []:
            st.zones.append(ZoneView(
                type=kind,
                polygon=[(float(p[0]), float(p[1])) for p in z.get("polygon", [])],
                alt_min=float(z.get("alt_min", 0.0)),
                alt_max=float(z.get("alt_max", 1e9)),
            ))

    for uid, ent in raw.items():
        if not isinstance(ent, dict):
            continue
        ent_type = ent.get("type", "")
        plat = ent.get("platform", {}) or {}
        pos = plat.get("position", {}) or {}
        lat = float(pos.get("latitude", 0.0))
        lon = float(pos.get("longitude", 0.0))
        alt = float(pos.get("altitude", 0.0))

        if ent_type == "fixed_wing_uav" or (plat and plat.get("entity_type") == "uav"):
            u = UavView(uid=uid, name=ent.get("name", uid),
                        latitude=lat, longitude=lon, altitude=alt)
            comm = ent.get("comm", {}) or {}
            stats = (comm.get("stats", {}) or {})
            u.comm_sent = int(stats.get("sent", 0))
            u.comm_delivered = int(stats.get("delivered", 0))
            u.jammed = bool(comm.get("external_jammed", False))
            track = ent.get("gimbal_tracking", {}) or {}
            det = track.get("detection", {}) or {}
            u.detected = bool(det.get("detected", False))
            u.misid_flag = bool(det.get("misid_flag", False))
            u.target_type = str(det.get("target_type", ""))
            u.confidence = float(det.get("confidence", 0.0))
            tpos = det.get("target_position")
            if tpos:
                u.target_lat = float(tpos.get("latitude", 0.0))
                u.target_lon = float(tpos.get("longitude", 0.0))
            tgt = track.get("target_entity", "")
            if tgt:
                u.target_uid = str(tgt)
            u.destroyed = (plat.get("status") == "destroyed")
            st.uavs[uid] = u
        elif ent_type in ("ground_vehicle", "decoy_vehicle"):
            g = GroundView(uid=uid, name=ent.get("name", uid),
                           latitude=lat, longitude=lon,
                           is_decoy=(ent_type == "decoy_vehicle"))
            if g.is_decoy:
                st.decoys[uid] = g
            else:
                st.targets[uid] = g
    return st
