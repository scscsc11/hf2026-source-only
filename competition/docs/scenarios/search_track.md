# Scenario: search_track (UAV search-and-track a car)

The simplest scenario. 1 UAV must find and continuously track 1 moving car.

## Entities
- **You control**: 1 `uav` (FixedWingUAV).
- **Environment**: 1 `ground_vehicle` target (moves once its trajectory is
  activated by the runner at startup).

## Observation
- `obs.self`: your pose, gimbal, `detection`.
- `obs.comm_inbox`: empty (no teammates).
- `obs.briefing`: `fleet_size=1`, `mission_area`, no threats.

## Actions
`fly_to`, `set_heading`, `set_speed`, `point_gimbal`, `set_gimbal_fov`,
`report_target`.

## Strategy hints
- **SEARCH**: fly an expanding pattern (spiral/lawnmower) around your home
  point; sweep the gimbal tilt to cover ground.
- **TRACK**: when `detection.detected` is stable, aim the gimbal and loiter
  near the detection so the target stays in the FOV.
- **REPORT**: call `report_target(lat, lon)` **every second** with your best
  estimate of the car's position — scoring depends on this continuous
  designation stream, not on the camera `detected` flag.

## Scoring (spec 2026-07-15: continuous designation accuracy)

Single dimension — **accuracy** (weight 1.0): continuous designation accuracy
of the player's 1Hz target-coordinate reports.

| dimension | weight | what it rewards |
|---|---|---|
| accuracy | 1.0 | per-tick soft-hit mean of 1Hz reports vs ground truth |

- The judge samples `report_target` at 1Hz. Each second `D_t` = haversine
  distance from the report to the nearest live true target; per-tick score
  `p_t = clamp(1 − D_t/D_max, 0, 1)` with `D_max = 30 m`.
- **Missed reports score `p_t = 0`** — you must report every second.
- Within each fixed 20s window the **2 lowest `p_t` are dropped** (judging-
  style "discard two lowest scores") as fault tolerance for brief
  occlusion/jitter.
- `accuracy = 100 × (Σ kept p_t / number of kept samples)`.
- **Full marks**: accurate reports every second of the run.
- **Pass threshold**: `total_score ≥ 60` (no separate completion gate —
  persistence is already encoded via p_t=0 for missed reports).

> The UAV has **no strike capability** — it only tracks and designates. Run
> with `--duration 60` (or longer) to evaluate.
