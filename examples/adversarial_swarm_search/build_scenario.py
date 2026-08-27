"""Build scenario.json programmatically so it stays readable + complete.

Generates a Spec 019 scenario with:
  * 10 FixedWingUAVs
  * 10 TargetVehicles (real)
  * 20 DecoyVehicles
  * 1 air_defense zone (low-altitude SAM belt)
  * 1 comm_jam_static zone
  * 1 comm_jam_random zone (parametric)
  * Bounds for the random-jam spawn rectangle

Run:
    python examples/adversarial_swarm_search/build_scenario.py \\
        --output examples/adversarial_swarm_search/config/scenario.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def uav(uid: str, name: str, lat: float, lon: float, heading: float) -> dict:
    return {
        "id": uid, "name": name, "type": "FixedWingUAV", "enabled": True,
        "params": {
            "initial_latitude": lat, "initial_longitude": lon,
            "initial_altitude": 600.0, "initial_heading": heading,
        },
        "components": {
            "kinematics": {"type": "KinematicsComponent", "enabled": True,
                           "params": {"min_speed": 15.0, "max_speed": 40.0,
                                      "max_bank_angle_deg": 45.0, "max_climb_rate_ms": 10.0}},
            "gimbal_tracking": {"type": "GimbalTrackingComponent", "enabled": True,
                                "params": {"pan_rate_limit_dps": 60.0, "tilt_rate_limit_dps": 45.0,
                                           "auto_track": False, "fov": 30.0}},
            "comm": {"type": "CommComponent", "enabled": True, "params": {}},
        },
    }


def target(uid: str, name: str, lat: float, lon: float, speed: float, wps: list) -> dict:
    return {
        "id": uid, "name": name, "type": "TargetVehicle", "enabled": True,
        "params": {"initial_latitude": lat, "initial_longitude": lon, "initial_altitude": 0.0},
        "components": {"trajectory": {"type": "TargetTrajectoryComponent", "enabled": True,
                                     "params": {"speed": speed, "waypoints": wps}}},
    }


def decoy(uid: str, name: str, lat: float, lon: float) -> dict:
    return {
        "id": uid, "name": name, "type": "DecoyVehicle", "enabled": True,
        "params": {"initial_latitude": lat, "initial_longitude": lon, "initial_altitude": 0.0},
        "components": {"trajectory": {"type": "TargetTrajectoryComponent", "enabled": True,
                                     "params": {"speed": 0.0}}},
    }


def wp(lat: float, lon: float) -> dict:
    return {"lat": lat, "lon": lon, "alt": 0.0, "t": 0.0}


def build() -> dict:
    """Build a 10-UAV + 10-target + 20-decoy scenario centred on the
    air-defense belt.  UAVs form a 5x2 perimeter ring; targets are on a
    5x2 inner grid; decoys are scattered on a denser 5x4 grid.

    Centre (27, 125) matches the visualization heightmap bounds
    (26.97..27.03, 124.97..125.03) and the other examples
    (uav_search_track_car, multi_uav_coop_decoy).
    """
    # Perimeter UAV ring — outside the SAM belt (alt band 0..2500)
    uavs = []
    for i in range(10):
        col = i % 5
        row = i // 5
        lat = 27.000 + 0.020 * row  # two rows: 27.000 / 27.020
        lon = 124.985 + 0.005 * col  # five cols: 124.985 .. 125.005
        heading = (i * 36.0) % 360.0
        uavs.append(uav(f"200{i+1:02d}", f"uav_{i+1:02d}", lat, lon, heading))

    # Targets — 10 on a 5x2 inner grid straddling the SAM belt
    targets = []
    speeds = [6.0, 5.0, 7.0, 4.0, 8.0, 6.0, 5.0, 6.0, 5.0, 7.0]
    for i in range(10):
        col = i % 5
        row = i // 5
        lat = 27.005 + 0.006 * row
        lon = 124.990 + 0.006 * col
        speed = speeds[i]
        # Closed-loop waypoint box
        wps = [wp(lat, lon), wp(lat - 0.003, lon), wp(lat - 0.003, lon + 0.006),
               wp(lat, lon)]
        targets.append(target(f"100{i+1:02d}", f"target_{i+1:02d}",
                              lat, lon, speed, wps))

    # Decoys — 20 on a 5x4 denser grid
    decoys = []
    for i in range(20):
        col = i % 5
        row = i // 4
        lat = 26.995 + 0.007 * row
        lon = 124.988 + 0.005 * col
        decoys.append(decoy(f"300{i+1:02d}", f"decoy_{i+1:02d}", lat, lon))

    return {
        "_comment": "Spec 019 — generated scenario; 10 UAV + 10 real + 20 decoys + zones.",
        "config_version": "1.0",
        "simulation": {
            "tick_rate_hz": 60.0, "time_scale": 1.0,
            "redis_host": "127.0.0.1", "redis_port": 6379,
            "redis_command_channel": "sim:commands",
            "redis_state_channel": "sim:state",
        },
        "bounds": {
            "lat_min": 26.97, "lat_max": 27.03,
            "lon_min": 124.97, "lon_max": 125.03,
        },
        "zones": [
            {"type": "air_defense",
             "polygon": [[27.008, 124.990], [27.008, 125.000],
                         [27.018, 125.000], [27.018, 124.990]],
             "alt_min": 0.0, "alt_max": 2500.0,
             "hit_delay_s": 2.0, "hit_probability": 1.0},
            {"type": "comm_jam_static",
             "polygon": [[27.020, 125.005], [27.020, 125.015],
                         [27.025, 125.015], [27.025, 125.005]],
             "alt_min": 0.0, "alt_max": 5000.0},
            {"type": "comm_jam_random",
             "max_count": 2, "radius_m": 400.0,
             "lifetime_s": 25.0, "spawn_interval_s": 12.0,
             "alt_min": 0.0, "alt_max": 5000.0,
             "rng_seed": 7.0},
        ],
        "entities": uavs + targets + decoys,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=str, required=True)
    args = p.parse_args()
    scen = build()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[build_scenario] wrote {out} — "
          f"{sum(1 for e in scen['entities'] if e['type'] == 'FixedWingUAV')} UAVs, "
          f"{sum(1 for e in scen['entities'] if e['type'] == 'TargetVehicle')} targets, "
          f"{sum(1 for e in scen['entities'] if e['type'] == 'DecoyVehicle')} decoys, "
          f"{len(scen['zones'])} zones")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
