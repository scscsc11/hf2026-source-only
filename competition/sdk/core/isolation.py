"""Information isolation — the single audit point for what players can see.

``build_obs`` projects a full-truth :class:`WorldState` into ONE agent's
:class:`Observation`, keeping ONLY that agent's own SelfView + its comm
inbox + the (static) briefing. No other entity's pose/truth/detection is
ever read, and dynamic zones are never projected.

This is data-layer enforcement, not access control: the forbidden fields
are physically absent from the Observation. In particular this entity's
**own** engine-geometric detection (the gimbal truth source) is NOT placed
into ``obs.self.detection`` — that slot is an empty placeholder. The truth
is extracted via :func:`_extract_truth` and threaded through an internal
runner→resolver→detector channel that players never see. ``tests/test_isolation.py``
asserts this by reflection.

See contracts/isolation.md for the precise allow/deny rules.
"""
from __future__ import annotations

from typing import Tuple

from .observation import (
    AreaSpec, CommStats, Detection, Message, MissionBriefing, Observation,
    SelfView, ZoneSpec,
)
from .world_state import EntityTruth, WorldState


_EMPTY_DETECTION = Detection(detected=False, confidence=0.0)


def _project_detection(me: EntityTruth) -> Detection:
    """Project THIS entity's own camera detection slot as an EMPTY placeholder.

    引擎几何真值（target_position 等）**绝不放入 obs**——否则选手实现
    sensor() 时在识别层覆盖前就能读到真值坐标（spec 029 S-2 泄漏）。
    真值仅通过 ``_extract_truth`` 取出，由 runner 经 resolver→detector 的
    内部通道传给 AccuracySimulator，对选手不可见。

    decide() 最终看到的 obs.self.detection 是 runner 用识别层产出覆盖后的值。
    """
    return _EMPTY_DETECTION


def _extract_truth(me: EntityTruth) -> Detection:
    """Extract THIS entity's own engine-geometric detection as an INTERNAL truth source.

    仅供 runner 内部传给 AccuracySimulator（按 accuracy 概率采样 + 噪声）。
    **绝不放入选手可见的 obs**。诱饵伪装（misid_flag → ground_vehicle）在此
    应用，保持 AccuracySimulator 输出的伪装语义不变。
    """
    gim = me.raw.get("gimbal_tracking", {}) or {}
    det = gim.get("detection", {}) or {}
    tpos = det.get("target_position")
    raw_type = str(det.get("target_type", ""))
    misid_flag = bool(det.get("misid_flag", False))
    # Disguise: a fooled-by-decoy detection masquerades as a real target.
    if det.get("detected") and (misid_flag or raw_type == "decoy_vehicle"):
        player_type = "ground_vehicle"
    else:
        player_type = raw_type if raw_type != "decoy_vehicle" else ""
    return Detection(
        detected=bool(det.get("detected", False)),
        confidence=float(det.get("confidence", 0.0)),
        target_lat=float(tpos.get("latitude")) if tpos else None,
        target_lon=float(tpos.get("longitude")) if tpos else None,
        azimuth_error_deg=det.get("azimuth_error"),
        target_type=player_type,
    )


def _project_comm_stats(me: EntityTruth) -> CommStats:
    """Project THIS entity's own comm statistics (a self-perception signal)."""
    comm = me.raw.get("comm", {}) or {}
    stats = comm.get("stats", {}) or {}
    return CommStats(
        sent=int(stats.get("sent", 0)),
        delivered=int(stats.get("delivered", 0)),
        received=int(stats.get("received", 0)),
        rejected_bytes=int(stats.get("rejected_bytes", 0)),
        rejected_rate=int(stats.get("rejected_rate", 0)),
        rejected_range=int(stats.get("rejected_range", 0)),
        rejected_jam=int(stats.get("rejected_jam", 0)),
    )


def _project_self(me: EntityTruth) -> SelfView:
    """Project THIS entity's own SelfView. Reads only ``me`` — never others."""
    gim = me.raw.get("gimbal_tracking", {}) or {}
    comm = me.raw.get("comm", {}) or {}
    return SelfView(
        uid=me.uid,
        lat=me.lat, lon=me.lon, alt=me.alt,
        heading_deg=me.heading, speed=me.speed,
        gimbal_pan=float(gim.get("pan_angle", 0.0)),
        gimbal_tilt=float(gim.get("tilt_angle", 0.0)),
        gimbal_fov_deg=float(gim.get("fov", gim.get("fov_deg", 30.0))),
        detection=_project_detection(me),
        status=me.status,
        jammed=bool(comm.get("external_jammed", False)),
        comm_stats=_project_comm_stats(me),
    )


def _project_inbox(me: EntityTruth) -> Tuple[Message, ...]:
    """Project THIS entity's own comm inbox — only sender uid + payload.

    The sender's pose is never included (that would leak another entity's
    truth). Players learn teammate positions only via the payload string
    they choose to share.
    """
    comm = me.raw.get("comm", {}) or {}
    inbox = comm.get("inbox", []) or []
    return tuple(
        Message(
            sender_uid=str(e.get("sender", "")),
            payload=str(e.get("payload", "")),
            recv_time=float(e.get("recv_time", 0.0)),
        )
        for e in inbox
    )


def build_obs(world_state: WorldState, entity_uid: str,
              briefing: MissionBriefing) -> Observation:
    """Build one agent's Observation from the full world state.

    Reads ONLY ``world_state.entities[entity_uid]``. Other entities and
    dynamic zones are never accessed — the returned Observation physically
    contains no teammate/target truth.

    Raises KeyError if ``entity_uid`` is not in the world state.
    """
    me = world_state.entities[entity_uid]
    return Observation(
        self=_project_self(me),
        comm_inbox=_project_inbox(me),
        briefing=briefing,
    )
