# API Reference

The player-facing surface is small. Everything lives under
`competition.sdk`. This document is the authoritative reference for the
agent contract, the observation model, and **every command** (name,
purpose, parameters with meaning and valid ranges).

## `competition.sdk.core.agent.Agent` (base class)

You never instantiate `Agent` directly; you subclass a scenario base
(`SearchTrackAgent` / `CoopAgent` / `SwarmAgent`).

```python
class Agent(ABC):
    def __init__(self, my_uid: str): ...        # set by the runner
    def configure(self, config) -> None: ...     # optional; static params
    def reset(self) -> None: ...                 # optional; per-run reset
    def decide(self, obs, dt: float) -> list[Command]: ...  # REQUIRED
    @property
    def name(self) -> str: ...
```

| member | when called | what to do |
|---|---|---|
| `__init__(my_uid)` | once, at startup | store it; it's the entity you control |
| `configure(config)` | once, before the run | optional; read static task params |
| `reset()` | before the first `decide()` of each run | optional; clear internal state |
| `decide(obs, dt)` | each decision cycle (~10 Hz) | **required**; return commands for `my_uid` |

- Commands you return are addressed to `my_uid` **forced by the runner** —
  you cannot control another entity.
- Return an empty list `[]` to issue no commands this cycle.
- `decide` must be pure aside from `self`-internal state; no Redis/file/net.

## Observation model (`competition.sdk.core.observation`)

### `Observation` (top-level — fixed shape, never changes)
```python
obs.self: SelfView
obs.comm_inbox: tuple[Message, ...]
obs.briefing: MissionBriefing
```

### `SelfView` — your own physically-perceivable state
| field | type | meaning |
|---|---|---|
| `uid` | str | your unique_id |
| `lat`, `lon`, `alt` | float | your position (WGS84) |
| `heading_deg` | float | your heading, degrees |
| `speed` | float | your speed, m/s |
| `gimbal_pan`, `gimbal_tilt` | float | camera orientation, degrees |
| `gimbal_fov_deg` | float | camera field of view, degrees |
| `detection` | `Detection` | camera detection result (this tick) |
| `status` | str | `"active"` / `"destroyed"` |
| `jammed` | bool | TRUE while you're comm-jammed (dynamic-threat signal) |
| `comm_stats` | `CommStats` | your radio statistics |

### `Detection` — this tick's camera result
| field | type | meaning |
|---|---|---|
| `detected` | bool | something is inside the camera FOV |
| `confidence` | float | [0,1]; 1 = centered in FOV |
| `target_lat`, `target_lon` | float\|None | **detected** position (may be a decoy — NOT truth) |
| `azimuth_error_deg` | float\|None | signed target offset from boresight, ° |
| `target_type` | str | `"ground_vehicle"` / `""` |

> **Decoy masquerade**: the camera can be fooled by a decoy with ~50%
> probability. When fooled, the detection masquerades as a real target
> (`target_type="ground_vehicle"`) — you **cannot** distinguish a decoy by
> the type field. Decoys are *stationary*; real targets move. Reject decoys
> by checking that a detection's position actually moves over time.

### `Message` — one teammate message received this tick
| field | type | meaning |
|---|---|---|
| `sender_uid` | str | who sent it |
| `payload` | str | content, ≤50 bytes; you define the format |
| `recv_time` | float | sim_time of receipt |

### `MissionBriefing` — static, whole-run, extensible
| field | type | meaning |
|---|---|---|
| `self_uid` | str | your uid |
| `fleet_size` | int | # controllable entities |
| `mission_area` | `AreaSpec`\|None | mission boundary |
| `known_threats` | tuple[`ZoneSpec`] | **deprecated** — now always `()`; use `approximate_zones` |
| `params` | dict | curated whitelist of scenario/algorithm params (never the full scenario.json) |
| `target_initial_pos` | tuple[float,float]\|None | target's initial (lat,lon). **Challenge 1 only**; challenges 2/3 are `None` |
| `target_count` | int\|None | number of real targets. **Challenges 2/3 only**; challenge 1 is `None` (=1 implied) |
| `approximate_zones` | tuple[`ApproxZoneSpec`] | approximate threat zones (bbox + area + kind + alt band), bbox expanded ~20%. **Challenge 3 only**; no exact polygons |
| `score_view` | `ScoreView`\|None | per-tick realtime score snapshot (updated each tick; `None` on tick 1) |

### Per-challenge information exposure

| info | challenge 1 | challenge 2 | challenge 3 |
|---|---|---|---|
| target **initial position** | given (`target_initial_pos`) | not given | not given |
| target **in-flight position / route** | not given (sense via camera) | not given | not given |
| target **count** | (=1 implied) | given (`target_count`) | given (`target_count`) |
| kill/static-jam zone **exact polygon** | n/a (no zones) | n/a (no zones) | not given |
| kill/static-jam zone **approximate (bbox+area)** | n/a | n/a | given (`approximate_zones`) |
| dynamic jam zone **position** | not given | not given | not given (sense via `obs.self.jammed`) |
| dynamic jam zone **statistics** (count/radius/lifetime/interval) | not given | not given | given (`params`, no positions) |

`briefing.params` is a **whitelist** curated per scenario — it never contains `entities`, target coordinates, waypoints, or exact zone polygons.

## Commands (`competition.sdk.core.commands`)

Each constructor returns a `Command`; the runner publishes it to `my_uid`.
Ranges below come from the engine's clamping behavior — out-of-range values
are silently clamped, not rejected.

### UAV navigation

#### `fly_to(lat, lon, alt=None, speed=None, loiter_radius=200.0, turn_direction="right")`
Navigate to a point and loiter (engine verb: `set_destination`). This is the
primary movement command.

| param | type | meaning | valid range |
|---|---|---|---|
| `lat` | float | destination latitude | any valid WGS84 |
| `lon` | float | destination longitude | any valid WGS84 |
| `alt` | float | destination altitude, m | ≥0 (omit = hold current) |
| `speed` | float | cruise speed, m/s | **15–40** (clamped; omit = hold current) |
| `loiter_radius` | float | loiter circle radius, m | >0 (default 200) |
| `turn_direction` | str | loiter turn sense | `"right"` / `"left"` |

The UAV flies toward `(lat,lon,alt)` at `speed`; on arrival it orbits the
point at `loiter_radius`, turning `turn_direction`. Re-issuing each tick is
fine if the target moves smoothly.

#### `set_heading(heading_deg)`
Set the UAV's heading (engine verb: `set_heading`).

| param | type | meaning | valid range |
|---|---|---|---|
| `heading_deg` | float | desired heading | any degrees (normalized) |

#### `set_speed(speed)`
Set the UAV's speed (engine verb: `set_speed`).

| param | type | meaning | valid range |
|---|---|---|---|
| `speed` | float | desired speed, m/s | **15–40** (clamped) |

### Gimbal / camera

#### `point_gimbal(pan_deg, tilt_deg)`
Aim the gimbal (engine verb: `component.gimbal_tracking.set_orientation`).
**This is the primary sensing interface** — the camera only detects targets
whose line-of-sight falls inside the FOV, so you must aim to search/track.

| param | type | meaning | valid range |
|---|---|---|---|
| `pan_deg` | float | azimuth (left/right), ° | **−180 to 180** (clamped) |
| `tilt_deg` | float | elevation (down=negative), ° | **−90 to 90** (clamped) |

> `pan=0, tilt=-45` aims forward-and-down — a good default for ground search.

#### `set_gimbal_fov(fov_deg)`
Set the camera field of view (engine verb: `set_fov`).

| param | type | meaning | valid range |
|---|---|---|---|
| `fov_deg` | float | field of view, ° | **5–120** (clamped) |

Trade-off: wider FOV covers more area at lower confidence; narrower FOV
gives higher confidence over a smaller area.

### Communication (coop_decoy, adversarial_swarm only)

#### `broadcast(payload)`
Send a message to all teammates (engine verb: `comm.broadcast`).

| param | type | meaning | valid range |
|---|---|---|---|
| `payload` | str | message content | **≤50 bytes** (UTF-8); raises `PayloadTooLarge` if exceeded |

Subject to byte/rate/range/jam checks. A dropped message is invisible
except via `SelfView.comm_stats.rejected_*`.

#### `send_to(peer_uid, payload)`
Send a point-to-point message (engine verb: `comm.send`).

| param | type | meaning | valid range |
|---|---|---|---|
| `peer_uid` | str | recipient unique_id | any teammate uid |
| `payload` | str | message content | **≤50 bytes** (UTF-8) |

### Target reporting (coop_decoy, adversarial_swarm only)

#### `report_target(lat, lon, target_id=None)`
Report the player's judged real-target position (targeting info). The judge
compares it against ground truth to score targeting accuracy (RMSE/CEP).

| param | type | meaning | valid range |
|---|---|---|---|
| `lat` | float | reported target latitude | any valid WGS84 |
| `lon` | float | reported target longitude | any valid WGS84 |
| `target_id` | str\|None | optional audit label | any string, or None |

- Rate-limited to 1 report per true-target per second (extra reports
  ignored). The target is the one the *judge* resolves by nearest-neighbour
  on the reported `(lat, lon)`; `target_id` is only an audit label and does
  **not** affect rate limiting or scoring.
- Accuracy is scored **per true target**: for each true target, RMSE of the
  reports resolved to it is computed, then `acc_t = 100*(1 − RMSE_t/D_max)`.
  The final accuracy dimension is the arithmetic mean of `acc_t` over **all**
  true targets (including destroyed ones). A target that was never reported
  scores 0.
- Reports whose nearest neighbour is a destroyed target (closer than the
  nearest live target) are dropped — they do not count toward any target.
- Unreported ticks are not penalized; only reported ones count toward RMSE.

**约束（spec 032）**:
1. **每次仅上报一个目标**: `report_target` 每次调用只接受一个 `(lat, lon)`
   坐标对。多目标场景需多次调用，但同一 UAV 同一帧仅最后一条 report 生效
   （多余 report 被丢弃并记 debug 日志）。
2. **不需要上报 id 号**: `target_id` 参数仅用于审计日志，不参与评分匹配
   或限频。评分完全依赖上报坐标的最近邻自动匹配。
3. **仿真内部自动匹配**: 评分器用最近邻 haversine 距离将上报坐标匹配到
   最近的**存活**真实目标作为评分依据。上报已摧毁目标（"尸体"）的 report
   会被丢弃，不污染任何存活目标的评分。

### Commands that do NOT exist
The engine has no such capability, so the SDK does not provide them:
- **`attack` / `fire`** — kills happen only via the SAM zone arbiter; you
  evade, you don't shoot.
- **`deploy_decoy`** — decoys are static scenario entities, not something
  you drop.

## Scenario base classes
```python
from competition.sdk.scenarios.search_track import SearchTrackAgent, run
from competition.sdk.scenarios.coop_decoy import CoopAgent, run
from competition.sdk.scenarios.adversarial_swarm import SwarmAgent, run
```
Each `run(...)` starts the engine + loop + scoring. Common kwargs:
`start_sim=False` (connect to a running engine), `seed=N` (randomize the
scene), `dry_run=True`, `output_dir=`, `quiet=`.
