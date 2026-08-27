# Scenario: adversarial_swarm (adversarial swarm search)

10 UAVs search for 10 real cars among 20 decoys while evading a SAM
air-defense zone and comm jamming.

## Entities
- **You control**: 1 of 10 `uav` (each gets its own instance).
- **Environment**: 10 `ground_vehicle` targets + 20 `decoy_vehicle` + threat zones:
  - `air_defense` (SAM): enter + linger 2s → destroyed (hit_probability=1.0).
  - `comm_jam_static`: fixed comm-jam region.
  - `comm_jam_random`: **dynamic** jam regions that spawn/despawn.

## Observation (strict isolation — fog of war)
- `obs.self`: pose/gimbal/detection + `jammed` (dynamic-threat signal) +
  `comm_stats`.
- `obs.briefing.known_threats`: **only static** pre-match-known zones (the
  SAM, the static jam). Dynamic jam regions are NOT here.
- `obs.comm_inbox`: teammate target shares + jam warnings.
- **You cannot see** teammates, targets, or dynamic jam positions directly.

## Threat awareness model ("known = briefing, unknown = sense + share")
| threat | how you know it |
|---|---|
| SAM zone (static) | `briefing.known_threats` — avoid by routing around or climbing above `alt_max` (2500m) |
| static jam (static) | `briefing.known_threats` |
| random jam (dynamic) | `obs.self.jammed` flips true → estimate position → `broadcast("J:lat,lon")` to warn teammates |

## Actions
All UAV commands + `broadcast` / `send_to`. Evasion is just `fly_to(alt=3000)`
above the SAM ceiling — no special command.

## Self-termination
If your UAV is destroyed (`status=="destroyed"`), the runner stops calling
your `decide()`. You don't need to handle it.

## Scoring (overhauled)

Same strike/identify mechanics as coop_decoy, plus mission time and
survivability. K=1: a single UAV continuously tracking for 20s suffices
to destroy/identify a target (no cooperative gate).

| dimension | weight | what it rewards |
|---|---|---|
| kill | 0.35 | fraction of real targets destroyed (10 total) |
| accuracy | 0.25 | targeting RMSE vs D_max=150m |
| mission_time | 0.25 | time to destroy ALL real targets (≤120s full) |
| alive | 0.10 | surviving UAVs / 10 |
| misid_penalty | 0.05 | 1 − (undestroyed-decoy track s)/60s |

- `mission_time`: `T_done ≤ 120s → 100`; linear decay to 0 at `120+120=240s`;
  `0` if not all real targets destroyed.
- **Full marks**: destroy all 10 real targets + RMSE→0 + ≤120s + 10/10 alive
  + no decoy lingering.
- **Pass threshold**: `kill_rate ≥ 0.7` AND `alive_rate ≥ 0.5` AND
  `total_score ≥ 70`.
- Note: completion (kill) rises over time, alive falls — total is
  non-monotonic by design.
