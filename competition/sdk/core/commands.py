"""Strongly-typed Command constructors.

Players do not build raw command dicts. They call these helpers, which
return ``Command`` objects. The runner publishes each Command, forcing
``unique_id = agent.my_uid`` — a player cannot address another entity
(invariant I-5 in contracts/agent-interface.md).

Verb names match the engine contract in
``config/schema/sim-commands.schema.json`` and ``specs/redis-channel-data.md``.

Capabilities NOT provided (the engine has no such concept):
  * attack / fire / launch   — kills happen only via ThreatArbiter zone
                                adjudication; the player can only evade.
  * deploy_decoy             — decoys are static scenario entities, not
                                something a player drops.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


DEFAULT_MAX_COMM_BYTES = 50


class PayloadTooLarge(ValueError):
    """Raised when a comm payload exceeds the byte cap (50 bytes)."""


@dataclass(frozen=True)
class Command:
    """One control command. The runner injects ``unique_id`` at publish time.

    ``verb`` is the engine command string (e.g. ``set_destination``,
    ``component.gimbal_tracking.set_orientation``, ``comm.broadcast``).
    ``params`` is the verb-specific parameter dict.
    """
    verb: str
    params: Dict[str, Any]


# ── UAV navigation ────────────────────────────────────────────────────────


def fly_to(lat: float, lon: float, alt: Optional[float] = None,
           speed: Optional[float] = None, loiter_radius: float = 200.0,
           turn_direction: str = "right") -> Command:
    """Navigate to a point and loiter (engine verb: ``set_destination``).

    Routes to the entity's KinematicsComponent. The engine REQUIRES all six
    params (lat/lon/alt/speed/loiter_radius/turn_direction); ``speed`` and
    ``alt`` default to the entity's current values if omitted, while
    ``loiter_radius``/``turn_direction`` get sensible defaults so a player
    can call ``fly_to(lat, lon)``.
    """
    params: Dict[str, Any] = {
        "latitude": float(lat),
        "longitude": float(lon),
        "loiter_radius": float(loiter_radius),
        "turn_direction": turn_direction,
    }
    if alt is not None:
        params["altitude"] = float(alt)
    if speed is not None:
        params["speed"] = float(speed)
    return Command(verb="set_destination", params=params)


def set_heading(heading_deg: float) -> Command:
    """Set the entity's heading (engine verb: ``set_heading``)."""
    return Command(verb="set_heading", params={"heading": float(heading_deg)})


def set_speed(speed: float) -> Command:
    """Set the entity's speed (engine verb: ``set_speed``)."""
    return Command(verb="set_speed", params={"speed": float(speed)})


# ── gimbal / camera ───────────────────────────────────────────────────────


def point_gimbal(pan_deg: float, tilt_deg: float) -> Command:
    """Point the gimbal (engine verb: ``component.gimbal_tracking.set_orientation``).

    This is the primary sensing interface: the camera only detects targets
    whose line-of-sight falls inside the FOV cone, so the player must aim
    the gimbal to search/track.
    """
    return Command(
        verb="component.gimbal_tracking.set_orientation",
        params={"pan": float(pan_deg), "tilt": float(tilt_deg)},
    )


def set_gimbal_fov(fov_deg: float) -> Command:
    """Set the camera field of view in degrees (engine verb: ``set_fov``).

    Clamped to [5, 50] by the engine — the 50° upper bound is a competition
    rule across all scenarios. A value above the cap is silently clamped down
    (the command still succeeds); read it back via ``obs.self.gimbal_fov_deg``.
    Wider FOV detects more but at lower confidence; narrower FOV gives higher
    confidence over a smaller area.
    """
    return Command(verb="set_fov", params={"angle": float(fov_deg)})


# ── communication ─────────────────────────────────────────────────────────
#
# Comm commands use the ``comm.*`` verb prefix and are addressed by the
# sender's unique_id (injected by the runner). Payload is capped at 50 bytes
# (UTF-8), matching the engine's CommComponent limit.


def _check_payload(payload: str) -> None:
    n = len(payload.encode("utf-8"))
    if n > DEFAULT_MAX_COMM_BYTES:
        raise PayloadTooLarge(
            f"comm payload is {n} bytes, exceeds cap of {DEFAULT_MAX_COMM_BYTES}"
        )


def broadcast(payload: str) -> Command:
    """Broadcast a message to all teammates (engine verb: ``comm.broadcast``).

    Subject to byte(≤50)/rate(4Hz window)/range(~1000m)/jam checks. A
    dropped message is visible only indirectly via ``SelfView.comm_stats``.
    """
    _check_payload(payload)
    return Command(verb="comm.broadcast", params={"payload": payload})


def send_to(peer_uid: str, payload: str) -> Command:
    """Send a point-to-point message (engine verb: ``comm.send``)."""
    _check_payload(payload)
    return Command(
        verb="comm.send",
        params={"peer_target_unique_id": str(peer_uid), "payload": payload},
    )


# ── target reporting (player target-designation intent) ───────────────────
#
# ``report_target`` is the player's way of designating "I judge a real target
# to be at (lat, lon)". The judge compares it against ground truth to score
# targeting accuracy. ALL scenarios use it: scenario 1 scores *continuous
# designation accuracy* from these 1Hz reports (spec 2026-07-15); scenarios
# 2/3 score per-target RMSE. The runner routes these to the evaluator (verb
# ``agent.report``); the engine never sees them — they are a judge-side
# signal, not an engine command.


def report_target(lat: float, lon: float,
                  target_id: Optional[str] = None) -> Command:
    """Report the player's judged real-target position (targeting info).

    In scenario 1 the judge samples this at 1Hz and scores continuous
    designation accuracy (per-tick soft-hit mean with per-20s-window "drop 2
    lowest"); missed seconds score 0, so players must report every second.
    In scenarios 2/3 the judge scores per-target RMSE. ``target_id``
    optionally labels which target the player believes this is; omit for "the
    current primary target". The judge rate-limits to 1 report/target/second
    and (scenarios 2/3) ignores reports on already-destroyed targets.
    """
    return Command(
        verb="agent.report",
        params={"lat": float(lat), "lon": float(lon),
                "target_id": target_id},
    )
