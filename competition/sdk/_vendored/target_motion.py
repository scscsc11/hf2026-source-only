"""Target motion prediction for spec 030 (kill-driven halt).

Mirrors the C++ TargetTrajectoryComponent::update() motion model
(src/components/target_trajectory_component.cc:115-167) so the runner
can predict where a ground target WILL be when a set_position command
takes effect, eliminating position jitter (no teleport-back).
"""
from __future__ import annotations

from .geometry import bearing_deg, destination, haversine_m


def predict_target_position(truth, horizon_s: float) -> tuple[float, float]:
    """Predict the target's (lat, lon) after ``horizon_s`` seconds.

    Replicates TargetTrajectoryComponent::update(): great-circle motion
    toward waypoints[current_wp_index] at constant speed, snapping to a
    waypoint and continuing to the next when the remaining budget
    crosses it.

    Args:
        truth: an EntityTruth-like object with ``.lat``, ``.lon``,
            ``.speed``, and ``.raw["trajectory"]`` containing
            ``is_navigating`` (bool), ``current_wp_index`` (int),
            ``waypoints`` (list of {lat, lon, alt}).
        horizon_s: seconds to extrapolate (typically one control period).

    Returns:
        (predicted_lat, predicted_lon). If the target is not navigating,
        has no waypoints, or has reached its final waypoint, returns the
        current position unchanged.
    """
    traj = (truth.raw or {}).get("trajectory", {}) or {}
    if not traj.get("is_navigating", False):
        return (truth.lat, truth.lon)

    waypoints = traj.get("waypoints") or []
    if not waypoints:
        return (truth.lat, truth.lon)

    idx = int(traj.get("current_wp_index", 0))
    if idx >= len(waypoints):
        return (truth.lat, truth.lon)

    speed = float(truth.speed)
    budget_m = speed * horizon_s  # total metres to travel in horizon

    lat, lon = float(truth.lat), float(truth.lon)

    while budget_m > 1e-9 and idx < len(waypoints):
        wp = waypoints[idx]
        wp_lat, wp_lon = float(wp["lat"]), float(wp["lon"])
        # Same-altitude nav (matches C++ alt_for_nav = target.alt)
        dist = haversine_m(lat, lon, wp_lat, wp_lon)

        if dist > budget_m:
            # Not reaching this waypoint: step toward it by budget.
            brg = bearing_deg(lat, lon, wp_lat, wp_lon)
            lat, lon = destination(lat, lon, brg, budget_m)
            budget_m = 0.0
        else:
            # Reach this waypoint: snap, consume distance, advance.
            lat, lon = wp_lat, wp_lon
            budget_m -= dist
            idx += 1

    return (lat, lon)
