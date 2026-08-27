# Getting Started

This guide gets a player running an agent in 5 minutes. You write **one
class** with **one method** (`decide`); the SDK handles the engine, Redis,
control loop, and scoring.

## Prerequisites

- The `opensim-sim(.exe)` engine binary — already included in the release
  package root directory. No need to build from source.
- Redis on `127.0.0.1:6379` — the release package includes a bundled
  `redis-server.exe` in `bin/`. Run `.\setup.ps1` (Windows) or `./setup.sh`
  (Linux) to verify dependencies, then `.\start.ps1` / `./start.sh` to start
  Redis + bridge + frontend automatically.
- Python 3.10+ with `redis` and `pyyaml` packages (`setup.ps1` / `setup.sh`
  will install them automatically).

## 1. Pick a scenario

Three scenarios, increasing complexity:

| Scenario | You control | Key challenge |
|---|---|---|
| `search_track` | 1 UAV | find & track 1 moving car |
| `coop_decoy` | 1 of 3 UAVs | co-track 3 real cars among 15 decoys, via comms |
| `adversarial_swarm` | 1 of 10 UAVs | same + evade a SAM zone & comm jamming |

Start with `search_track`.

## 2. Copy a template

```bash
cp competition/templates/search_track_template.py my_agent.py
```

Edit `decide()`:

```python
class MyAgent(SearchTrackAgent):
    def decide(self, obs, dt):
        if obs.self.detection.detected:
            return [point_gimbal(0.0, -45.0),
                    fly_to(obs.self.detection.target_lat,
                           obs.self.detection.target_lon)]
        return [fly_to(obs.self.lat + 0.001, obs.self.lon),
                point_gimbal(0.0, -45.0)]
```

## 3. Run

```bash
python -m competition run --scenario search_track \
    --agent my_agent:MyAgent --duration 60
```

The SDK starts the engine, runs the loop at ~10 Hz, scores, and writes
`output/<run>.evaluation.json`.

## What you can see (the whole contract)

Every `decide(obs, dt)` call gives you exactly:

```python
obs.self         # YOUR UAV's pose, gimbal, camera detection, jammed/status
obs.comm_inbox   # messages teammates sent you (their payload, not their pose)
obs.briefing     # pre-match static info (mission area, known static threats)
```

**You cannot see** teammate poses, target ground-truth, or dynamic threat
positions. That's enforced in the data — those fields are physically absent
from `obs`. Coordinate via `broadcast()`/`send_to()` and parse
`obs.comm_inbox`.

> **Decoys masquerade**: when the camera is fooled by a decoy (~50% chance),
> the detection reports `target_type="ground_vehicle"` — you can't tell it's
> a decoy from the type. Decoys are *stationary*; real targets move. Reject
> decoys by checking that a detection's position actually changes over time.

## Train across many scenes with `--seed`

Pass `--seed N` to run the same scenario with a different scene (target
routes, threat zones, start positions all derived from N):

```bash
for s in 1 2 3 4 5; do
  python -m competition run --scenario adversarial_swarm \
      --agent my_agent:MyAgent --seed $s --duration 60
done
```

Same seed → same scene (reproducible). In `search_track`, the UAV and the
target move together so their relative distance stays constant (consistent
difficulty).

## Watch the run in 3D with `--visualize`

Add `--visualize` to open a live 3D view of the scenario in your browser:

```bash
python -m competition run --scenario adversarial_swarm \
    --agent my_agent:MyAgent --duration 60 --visualize
```

The SDK then starts the Redis↔WebSocket bridge and serves the prebuilt 3D
frontend, and opens `http://127.0.0.1:3000/` automatically. The view shows
your UAVs, detections, threat zones, and trails in real time.

- The visualization is a **bystander** — it only reads Redis and never
  affects the control loop or scoring.
- The release package bundles **Node.js** in `bin/node.exe` (Windows) or
  `bin/node` (Linux), so no separate Node.js installation is needed.
- `--no-browser` starts the view but doesn't auto-open the browser.

> Release note: to ship 3D viewing, copy the repo's `visualization/` folder
> alongside `competition/`. See [release.md](release.md).

## Commands available

`fly_to`, `set_heading`, `set_speed`, `point_gimbal`, `set_gimbal_fov`
(all scenarios); `broadcast`, `send_to` (coop_decoy, adversarial_swarm).
See [api-reference.md](api-reference.md) for signatures.

## Next steps

- [api-reference.md](api-reference.md) — full interface reference.
- [scenarios/](scenarios/) — per-scenario rules, scoring, pass thresholds.
- Run the baselines to see a working reference: `--agent
  baselines.search_track_fsm:FsmAgent` (or `coop_distributed` /
  `swarm_distributed`).
