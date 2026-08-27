"""Target-trajectory load + inject for Feature 007 auto-static (T11).

Two examples (adversarial_swarm_search, multi_uav_coop_decoy) carried
verbatim copies of ``_load_target_trajectories`` and a near-identical
``_inject_target_trajectories``. The only difference between the two
inject copies was *how* a command is published:

  * adversarial: ``redis_conn.publish("sim:commands", json.dumps(cmd))``
  * multi:       ``client.publish_dict(cmd)``

We factor the common logic here and let the caller pass a
``publish_fn(cmd: dict) -> None`` callback, so neither example's publish
path changes. The "is this uid in the current state?" membership check
is also delegated to a caller-supplied predicate (the two examples used
different state shapes — ``SwarmState.uavs/targets/decoys`` vs
``MultiSimState.entities``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional


def load_target_trajectories(scenario_path: str) -> dict[str, dict]:
    """Read declared target trajectories from scenario.json.

    Returns ``{target_uid: {"speed": float, "waypoints": [...]}}`` for
    every ``TargetVehicle`` whose trajectory component declares
    waypoints. These are only *declarations*: the sim keeps ground
    targets stationary until a ``set_trajectory`` command flips their
    ``is_navigating`` flag (Feature 007 auto-static). :func:`inject_target_trajectories`
    activates them at startup.
    """
    sp = Path(scenario_path)
    with sp.open("r", encoding="utf-8-sig") as f:
        scen = json.load(f)
    out: dict[str, dict] = {}
    for ent in scen.get("entities", []):
        if ent.get("type") != "TargetVehicle":
            continue
        uid = str(ent.get("id", ""))
        traj = (ent.get("components") or {}).get("trajectory", {}) or {}
        params = traj.get("params", {}) or {}
        wps = params.get("waypoints")
        if uid and isinstance(wps, list) and wps:
            out[uid] = {
                "speed": float(params.get("speed", 8.0)),
                "waypoints": [
                    {"lat": float(w["lat"]), "lon": float(w["lon"]),
                     "alt": float(w.get("alt", 0.0)),
                     "t": float(w.get("t", 0.0))}
                    for w in wps
                ],
            }
    return out


def inject_target_trajectories(
    state,
    trajectories: dict,
    *,
    dry_run: bool,
    log,
    is_known_uid: Callable[[str], bool],
    publish_fn: Optional[Callable[[dict], None]] = None,
) -> int:
    """Activate each declared target trajectory via set_speed + set_trajectory.

    Ground targets are auto-static on init (``is_navigating=false``); the
    ``set_trajectory`` command flips ``is_navigating=true`` so the
    trajectory component drives motion. We also ``set_speed`` explicitly
    because init overrides the scenario speed with
    ``auto_static_speed=0``.

    Parameters
    ----------
    state:
        Current sim state — only inspected via ``is_known_uid``.
    trajectories:
        Output of :func:`load_target_trajectories`.
    dry_run:
        When True, no commands are published; activation is only logged.
    log:
        Logger callable (quiet-aware).
    is_known_uid:
        ``(uid) -> bool``: membership check against the live state. The
        adversarial runner checks ``uavs | targets | decoys``; the multi
        runner checks ``entities``.
    publish_fn:
        ``(cmd: dict) -> None``: how to actually emit a command. For the
        adversarial runner this wraps ``redis.publish("sim:commands",
        json.dumps(cmd))``; for multi it wraps
        ``client.publish_dict(cmd)``. Required when ``dry_run`` is False.

    Returns the number of targets activated.
    """
    if not trajectories:
        log("[run] no target trajectories declared; targets stay static")
        return 0
    activated = 0
    for uid, traj in trajectories.items():
        if not is_known_uid(uid):
            log(f"[run] WARN: target {uid} not in state; skip trajectory")
            continue
        cmds = [
            {"unique_id": uid, "cmd": "set_speed",
             "params": {"speed": traj["speed"]}},
            {"unique_id": uid, "cmd": "set_trajectory",
             "params": {"waypoints": traj["waypoints"]}},
        ]
        if dry_run:
            log(f"[run] dry-run: would activate {uid} "
                f"({len(traj['waypoints'])} wps, {traj['speed']} m/s)")
        else:
            if publish_fn is None:
                raise ValueError(
                    "publish_fn is required when dry_run is False"
                )
            for cmd in cmds:
                publish_fn(cmd)
        activated += 1
    return activated
