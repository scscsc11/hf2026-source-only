# Scenario: coop_decoy (multi-UAV cooperative decoy)

3 UAVs cooperate to continuously co-track 3 real cars among 15 stationary
decoys. Coordination is **only** via the constrained comm channel.

## Entities
- **You control**: 1 of 3 `uav` (each player-agent gets its own instance).
- **Environment**: 3 `ground_vehicle` targets (moving) + 15 `decoy_vehicle`
  (stationary).

## Observation (strict isolation)
- `obs.self`: your pose/gimbal/detection + your own `comm_stats`.
- `obs.comm_inbox`: messages from teammates (their payload, **not** their pose).
- `obs.briefing`: `fleet_size=3`, `mission_area`, `params.coop_k`,
  `params.sector_center_*`.
- **You cannot see** teammate positions or which detection is a decoy.

## Actions
All UAV commands + `broadcast` / `send_to` (≤50 bytes, rate/range/jam limited).

## Strategy hints
- **Sector split**: deterministically claim a search sector from your uid
  (e.g. `hash(uid) % fleet_size`) — no comms needed for assignment.
- **Decoy rejection**: a real target moves; a decoy is stationary. Confirm a
  detection over several ticks with consistent position before trusting it.
- **Target share**: on a confirmed detection, `broadcast("T:lat,lon")`;
  teammates parse it and converge to track it down (2 UAVs simultaneously for 20s suffices).
- Agree on a payload format with teammates (e.g. `T:` = target, `R:` = rendezvous).

## Scoring (overhauled)

The UAV **has strike capability**: continuously tracking a real target with
≥2 UAVs simultaneously for 20s **destroys** it. Decoys tracked 20s are "identified" and stop
counting as misid. Report target coordinates via `report_target()`.

| dimension | weight | what it rewards |
|---|---|---|
| kill | 0.50 | fraction of real targets destroyed (3 total) |
| accuracy | 0.30 | targeting RMSE vs D_max=120m (from `report_target`) |
| misid_penalty | 0.20 | 1 − (undestroyed-decoy track s)/30s |

- `kill = 100 × (destroyed_real / 3)`
- `accuracy = 100 × max(0, 1 − RMSE/120)`; RMSE over rate-limited reports.
- `misid_penalty = 100 × max(0, 1 − misid_s/30)`; only unidentified decoys.
- **Full marks**: destroy all 3 real targets + RMSE→0 + no decoy lingering.
- **Pass threshold**: `kill_rate ≥ 2/3` AND `total_score ≥ 70`.
- Reports on already-destroyed targets are ignored (switch targets).
