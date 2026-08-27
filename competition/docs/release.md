# Release Manifest

What to ship when publishing the competition SDK, including the engine's
runtime data (terrain CSVs, presets, schema) and the optional 3D-view assets.

## TL;DR — the release tree

```
opensim-release/                     ← "project root" (engine runs from here)
├── opensim-sim(.exe)                ← the C++ engine binary
├── config/                          ← engine runtime data (REQUIRED, ~790 MB)
│   ├── defaults.json                ← module defaults (REQUIRED)
│   ├── GridDataAll_18.csv           ← A*/navigation grid (~41 MB, REQUIRED)
│   ├── HeightSample.csv             ← terrain elevation (~786 MB, REQUIRED)
│   ├── schema/                      ← JSON schemas (REQUIRED — engine validates)
│   │   ├── defaults.schema.json
│   │   ├── sim-commands.schema.json
│   │   ├── sim-state.schema.json
│   │   └── zones-bucket.schema.json
│   └── models/                      ← entity presets (REQUIRED)
│       ├── uav.json  ground_vehicle.json  decoy_vehicle.json  gimbal.json
├── competition/                     ← the SDK (self-contained Python)
│   ├── sdk/  baselines/  templates/  tests/  docs/
│   └── scenarios/<name>/scenario.json
└── visualization/                   ← OPTIONAL: only for --visualize (~10 MB)
    ├── dist/  (index.html, bundle.js — prebuilt frontend)
    ├── public/ (heightmap.json — fetched by frontend at runtime)
    └── src/bridge/ (ts-node source for the Redis↔WS bridge)
```

That's the whole release. Nothing from `examples/`, `src/`, `external/`, or
`build/` is needed.

## Why each piece

### `opensim-sim` binary + `config/` (REQUIRED — the engine won't run without these)

The engine resolves **all** paths relative to its working directory (the
"project root"), and at startup it:
- loads `config/defaults.json` (module defaults),
- validates it against `config/schema/defaults.schema.json` (**hard error if
  the schema file is missing**),
- loads terrain elevation from `config/HeightSample.csv` (~786 MB, loaded
  once into a height map),
- loads the A*/navigation grid from `config/GridDataAll_18.csv` (~41 MB),
- expands entity presets from `config/models/*.json`.

The SDK spawns the binary with the project root as CWD, so the engine finds
`config/` next to `opensim-sim`. **You must ship the entire `config/` tree.**

> **Size note:** `config/` is ~790 MB, dominated by `HeightSample.csv`
> (786 MB). Both terrain CSVs are required for correct physics/navigation.
> There is currently no way to omit them. If release size is a concern, a
> future task could add a coarser/optional terrain mode.

### `competition/` (REQUIRED — the SDK)

Self-contained: imports only from within itself (runtime helpers are vendored
in `competition/sdk/_vendored/`; scenario data is in
`competition/scenarios/`). It does **not** read from `examples/` at runtime.

### `visualization/` (OPTIONAL — only for `--visualize`)

Needed only if players should watch runs in 3D. The SDK's `--visualize`
flag starts:
1. the Redis↔WebSocket bridge (`ts-node visualization/src/bridge`, needs
   **Node.js** on the player's machine),
2. a static server over `visualization/dist` (the prebuilt frontend) with a
   fallback to `visualization/public` (serves `/heightmap.json`).

Ship the whole `visualization/` folder. Requires Node.js on the player side.
If absent, `--visualize` is silently skipped (graceful degradation).

## Engine binary resolution

The SDK finds `opensim-sim` in this order:
1. `--sim-binary <path>` (explicit).
2. `OPENSIM_SIM_BIN` environment variable.
3. `opensim-sim(.exe)` beside `competition/` (the release layout above).
4. `build/opensim-sim(.exe)` (dev convenience).

With the release layout, **no flags are needed** — the SDK auto-detects the
binary and runs from the project root so `config/` resolves.

## Player prerequisites

- **Redis** at `127.0.0.1:6379` (e.g. `docker run -p 6379:6379 redis`).
- **Python 3.10+** + `redis-py` (`pip install redis`).
- *(optional)* **Node.js** — only for `--visualize`.

## Player workflow

```bash
cd opensim-release                       # the project root
docker run -d -p 6379:6379 redis         # Redis (once)
pip install redis                        # Python client (once)

# smoke-test the release with a baseline
python -m competition run --scenario search_track \
    --agent baselines.search_track_fsm:FsmAgent --duration 60

# with 3D view
python -m competition run --scenario adversarial_swarm \
    --agent baselines.swarm_distributed:SwarmDistributedAgent \
    --duration 60 --visualize

# write your own agent
cp competition/templates/search_track_template.py my_agent.py
python -m competition run --scenario search_track \
    --agent my_agent:MyAgent --duration 60 --seed 7
```

## Minimal vs full release

| Release type | ships | size | 3D view |
|---|---|---|---|
| **Core** (engine + scenarios) | `opensim-sim`, `config/`, `competition/` | ~790 MB | no |
| **Full** (+ 3D) | above + `visualization/` | ~800 MB | yes |

A "core" release fully supports writing, running, and scoring agents across
all three scenarios with seed-based variety — just without the live 3D view.

## Keeping `_vendored/` in sync

`competition/sdk/_vendored/` is a snapshot of `examples/_common/` (+ a
vendored `geometry.py`). When upstream changes, re-copy and test:

```bash
cp examples/_common/<mod>.py competition/sdk/_vendored/<mod>.py
py -3 -m pytest competition/tests/ -q
```
