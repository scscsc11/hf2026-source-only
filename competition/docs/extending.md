# Extending the SDK (for developers)

This guide is for **maintainers** who need to: add a new scenario, modify an
existing scenario's entities/zones/scoring, add a command, or support
heterogeneous entities. The authoritative contracts are in
`specs/026-competition-agent-sdk/contracts/`; this is the worked, copy-paste
how-to with full file contents and a verification checklist.

> **Read this first — the mental model.** The SDK is a thin Python layer over
> the C++ engine. The engine reads a `scenario.json` (entities + zones) and
> runs; the SDK spawns it, drives one player agent per controllable entity,
> projects isolated observations, and scores. So "a scenario" = a
> `scenario.json` + a small Python runner that knows how to score it. You
> rarely touch `core/`.

---

## Table of contents
1. [Core stability promises (do not break)](#1-core-stability-promises)
2. [How to MODIFY an existing scenario](#2-modify-a-scenario)
3. [How to ADD a new scenario (full walkthrough)](#3-add-a-new-scenario)
4. [How to ADD a command](#4-add-a-command)
5. [How to ADD a briefing field](#5-add-a-briefing-field)
6. [Heterogeneous entities (reserved interface)](#6-heterogeneous-entities)
7. [Quick checklist index](#7-checklist-index)

---

## 1. Core stability promises

These guarantees keep existing scenarios working when you extend. Do not
break them without bumping the spec major version.

1. `Observation` top-level is fixed: `self` / `comm_inbox` / `briefing`.
   Never add or remove a top-level field.
2. `MissionBriefing` fields are append-only; **every new field MUST have a
   default value**.
3. `Agent.configure` / `reset` / `decide` signatures are frozen.
4. Existing `Command` constructors only gain optional params.
5. `RunnerBase` hooks (`make_agent_for` / `build_obs` / `build_briefing` /
   `build_scoring` / `inject_startup`) keep their signatures.

---

## 2. Modify a scenario

"Modify" = change the entities, zones, target behavior, or scoring of an
existing scenario. **You usually don't write any Python** — only edit
`scenario.json` (and optionally tweak the runner's scoring profile).

### 2.1 Change entities / their start positions / target routes

Edit the scenario's `config/scenario.json`. Entity structure:

```jsonc
{
  "id": 20001, "name": "uav_alpha", "type": "FixedWingUAV",
  "params": {
    "initial_latitude": 27.0, "initial_longitude": 125.0,
    "initial_altitude": 500.0, "initial_heading": 0.0
  },
  "components": {
    "kinematics": { "params": { "min_speed": 15, "max_speed": 40, ... } },
    "gimbal_tracking": { "params": { ... } }
  }
}
```

- **Move a UAV/target/decoy**: change `params.initial_latitude/longitude/altitude`.
- **Change a target's route**: edit its `components.trajectory.params.waypoints`
  (list of `{lat, lon, alt}`) and `speed`.
- **Add/remove a decoy**: add/remove a `DecoyVehicle` entity in the list.
  Decoys are stationary; the camera misidentifies them with ~50% probability.
- **Point the runner at your edited file**: `--scenario-json path/to/it.json`.

> The SDK's `--seed N` already does this procedurally for training variety.
  Editing by hand is for permanent scenario changes.

### 2.2 Change threat zones (adversarial_swarm)

Zones live in the top-level `zones` array of `scenario.json`:

```jsonc
{ "type": "air_defense", "polygon": [[lat,lon],...],
  "alt_min": 0, "alt_max": 2500, "hit_delay_s": 2.0, "hit_probability": 1.0 }
{ "type": "comm_jam_static", "polygon": [...], "alt_min": 0, "alt_max": 5000 }
{ "type": "comm_jam_random", "max_count": 2, "radius_m": 400,
  "lifetime_s": 25, "spawn_interval_s": 12, "rng_seed": 7 }
```

- Static zones (`air_defense`, `comm_jam_static`) are **pre-match-known** →
  they appear in `obs.briefing.known_threats`.
- `comm_jam_random` is dynamic → players sense it via `obs.self.jammed`,
  never in the briefing.

To add a SAM site, append an `air_defense` entry. To make the SAM deadlier,
raise `hit_probability` or lower `hit_delay_s`.

### 2.3 Change scoring / pass thresholds

Scoring is driven by `ScoringProfile` (from
`competition/sdk/_vendored/coop_eval.py`), selected in the scenario's
`scoring.py`.
Edit the scenario's `runner.py`'s `build_scoring()` to pass a different
profile (K, dwell_target_s, weights). The pass threshold is a profile field.

### 2.4 Verify the change
```bash
python -m competition run --scenario <name> \
    --agent baselines.<name>:<Cls> --scenario-json your/edited.json --duration 60
```
Confirm the run completes and `evaluation.json` reflects your changes.

---

## 3. Add a new scenario

A worked walkthrough: add a fictional "capture" scenario. **Five files +
one registration line.** Copy this structure and fill in.

### Step 1 — Create the scenario.json

Put it under `competition/scenarios/<name>/scenario.json` (the SDK ships its
own scenario data there — self-contained for release) OR any path you pass
via `--scenario-json`. Minimal example:

```jsonc
{
  "simulation": { "tick_rate_hz": 60, "control_rate_hz": 10 },
  "entities": [
    { "id": 20001, "name": "uav_1", "type": "FixedWingUAV",
      "params": { "initial_latitude": 27.0, "initial_longitude": 125.0,
                  "initial_altitude": 500.0, "initial_heading": 0.0 },
      "components": { "kinematics": { "params": {} },
                      "gimbal_tracking": { "params": {} } } },
    { "id": 10001, "name": "target_1", "type": "TargetVehicle",
      "params": { "initial_latitude": 27.005, "initial_longitude": 125.005,
                  "initial_altitude": 0.0 },
      "components": { "trajectory": { "params": { "speed": 6.0,
          "waypoints": [{"lat":27.005,"lon":125.01,"alt":0}] } } } }
  ]
}
```

Entity `type` values: `FixedWingUAV`, `TargetVehicle`, `DecoyVehicle` (each
maps to a preset in `config/models/`). `FixedWingUAV` carries a
`gimbal_tracking` component automatically (the camera).

### Step 2 — Create the package dir `competition/sdk/scenarios/<name>/`

```
competition/sdk/scenarios/capture/
├── __init__.py        # exports
├── observation.py     # CaptureObs (inherits Observation)
├── agent.py           # CaptureAgent(Agent)
├── runner.py          # CaptureRunner(RunnerBase) + run()
└── scoring.py         # (optional; can inline in runner)
```

### Step 3 — `__init__.py`
```python
from pathlib import Path
from .agent import CaptureAgent
from .observation import CaptureObs
from .runner import CaptureRunner, run

SCENARIO_DIR = Path(__file__).resolve().parents[3] / "scenarios" / "capture"
DEFAULT_SCENARIO_JSON = str(SCENARIO_DIR / "config" / "scenario.json")

__all__ = ["CaptureAgent", "CaptureObs", "CaptureRunner", "run",
           "DEFAULT_SCENARIO_JSON"]
```

### Step 4 — `observation.py`
```python
from ...core.observation import Observation

class CaptureObs(Observation):
    """Top-level stays self/comm_inbox/briefing. Add THIS entity's own
    extra fields here if needed — never another entity's truth."""
    pass
```

### Step 5 — `agent.py`
```python
from typing import List
from ...core.agent import Agent
from ...core.commands import Command
from .observation import CaptureObs

class CaptureAgent(Agent):
    def decide(self, obs: CaptureObs, dt: float) -> List[Command]:
        raise NotImplementedError
```

### Step 6 — `runner.py` (the only non-trivial file)
```python
import json
from pathlib import Path
from typing import Tuple
from ...core.observation import MissionBriefing
from ...core.runner import RunnerBase, ScenarioConfig
from ...core.scoring import ScoringProfile, profile_uav_search_track_car
from ...core.world_state import WorldState

class CaptureRunner(RunnerBase):
    scenario_name = "capture"
    controllable_types = {"uav"}

    def __init__(self, cfg, agent_cls, log=print):
        super().__init__(cfg, log)
        self.agent_cls = agent_cls
        self._scenario_cfg = self._load(cfg.scenario_path)

    # REQUIRED: build the static briefing for one entity
    def build_briefing(self, world_state, entity_uid) -> MissionBriefing:
        return MissionBriefing(self_uid=entity_uid,
                               fleet_size=len(world_state.uavs))

    # REQUIRED: (ScoringProfile, set of true-target uids)
    def build_scoring(self, world_state):
        return (profile_uav_search_track_car(duration_s=self.cfg.duration_s),
                set(world_state.targets.keys()))

    # OPTIONAL: per-tick scoring inputs (search_time, alive_rate, ...)
    def score_extras(self, world_state, destroyed_uids):
        return {}

    # OPTIONAL: start target routes / any one-shot setup
    def inject_startup(self, client, first):
        for ent in self._scenario_cfg.get("entities", []):
            if ent.get("type") in ("TargetVehicle", "ground_vehicle"):
                uid = str(ent.get("id"))
                tp = ent.get("components",{}).get("trajectory",{}).get("params",{})
                if tp.get("speed") is not None:
                    client.publish_raw({"unique_id": uid, "cmd": "set_speed",
                                        "params": {"speed": float(tp["speed"])}})
                if tp.get("waypoints"):
                    client.publish_raw({"unique_id": uid, "cmd": "set_trajectory",
                                        "params": {"waypoints": tp["waypoints"]}})

    @staticmethod
    def _load(path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return {}

def run(agent_cls, *, duration=60.0, scenario=None, start_sim=True,
        output_dir="output", host="127.0.0.1", port=6379, dry_run=False,
        quiet=False, sim_binary=None, seed=0):
    from . import DEFAULT_SCENARIO_JSON
    cfg = ScenarioConfig(scenario_name="capture",
                         scenario_path=scenario or DEFAULT_SCENARIO_JSON,
                         duration_s=duration, redis_host=host, redis_port=port,
                         output_dir=output_dir, sim_binary=sim_binary,
                         start_sim_flag=start_sim, dry_run=dry_run,
                         quiet=quiet, seed=seed)
    return CaptureRunner(cfg, agent_cls).run()
```

### Step 7 — Register in the CLI
Edit `competition/sdk/cli.py`:
```python
run_p.add_argument("--scenario", required=True,
    choices=["search_track", "coop_decoy", "adversarial_swarm", "capture"])
...
elif args.scenario == "capture":
    from competition.sdk.scenarios.capture import run
    run(agent_cls, duration=args.duration or 60.0, seed=args.seed, **common)
```

### Step 8 — Register a randomization policy (optional)
If you want `--seed` to vary this scenario, add to
`competition/sdk/core/scenario_randomizer.py`:
```python
_POLICIES = {
    ...
    "capture": RandomizePolicy,   # or a custom policy subclass
}
```

### Step 9 — Add an isolation test + a baseline
- In `competition/tests/test_isolation.py`, add a check that your scenario's
  obs contains no truth leak (copy an existing assertion).
- Add `competition/baselines/capture_<x>.py` with an agent that runs.

### Step 10 — Verify
```bash
python -m competition run --scenario capture \
    --agent baselines.capture_x:X --duration 60
```

### Common mistakes
- ❌ Reading `world_state.entities[other_uid]` in `build_obs` → isolation leak.
  Leave `build_obs` as the default (inherited isolated projection).
- ❌ Adding a top-level field to `CaptureObs` besides self/comm_inbox/briefing.
- ❌ Reimplementing the control loop — always subclass `RunnerBase`.

---

## 4. Add a command

In `competition/sdk/core/commands.py`:
```python
def replan_path(lat, lon, avoid=None) -> Command:
    """A* re-plan. Verify the verb against config/schema/sim-commands.schema.json."""
    return Command(verb="uav.replan_path",
                   params={"latitude": float(lat), "longitude": float(lon),
                           "avoid_waypoints": avoid or []})
```
Rules:
- Verify the verb against `config/schema/sim-commands.schema.json`.
- Never hardcode `unique_id` — the runner injects `my_uid`.
- Update `docs/api-reference.md` with the param table.

---

## 5. Add a briefing field

In `competition/sdk/core/observation.py`:
```python
@dataclass(frozen=True)
class MissionBriefing:
    ...
    no_fly_zones: tuple[ZoneSpec, ...] = ()   # NEW — default REQUIRED
```
Fill it in the scenario's `build_briefing`. Existing scenarios ignore it.

---

## 6. Heterogeneous entities (reserved interface)

To dispatch different agent classes by entity type, override
`make_agent_for` in your runner:
```python
def make_agent_for(self, entity_type, entity_uid, world_state) -> Agent:
    return {"recon_uav": ReconAgent, "strike_uav": StrikeAgent}[entity_type](
        my_uid=entity_uid)
```
Each agent still sees only its own `SelfView`; cross-type coordination goes
through comms. **Never** build a central agent that sees all entities' truth.

---

## 7. Checklist index

| task | files touched | core changes? |
|---|---|---|
| Modify scenario entities/zones | `competition/scenarios/<name>/scenario.json` | no |
| Modify scoring | scenario `runner.py` `build_scoring` | no |
| Add scenario | `scenarios/<name>/` (5 files) + `cli.py` | no |
| Add command | `core/commands.py` + `docs/api-reference.md` | add function |
| Add briefing field | `core/observation.py` | add field (defaulted) |
| Heterogeneous | scenario `runner.py` `make_agent_for` | no |
