"""Spec 019 (game-adversarial-swarm-search) example package.

Adversarial 10-UAV swarm-search scenario over a 6km x 6km area with:
  * 10 real targets (ground vehicles) dispersed on a grid
  * 20 decoys (decoy vehicles) intermingled
  * 1 air-defense kill zone (low-altitude SAM belt)
  * 2 comm-jam zones (one static, one random-spawning)

The example demonstrates:
  * Kernel-level threat arbitration (ThreatArbiter) — UAVs entering the
    SAM belt are killed after `hit_delay_s`.
  * Engine-arbitrated comm jamming — UAVs inside a jam zone lose their
    `comm.stats.delivered` path even though they're within range.
  * Search-track algorithm with threat-intel / blind avoidance (the
    controller avoids published air-defense zones by detouring around
    their polygon).

This file is intentionally minimal — it's a marker module so Python can
import the package. The actual orchestration lives in `run.py`.
"""
