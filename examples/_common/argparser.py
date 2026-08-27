"""Standard argument parser + path bootstrap for example runners (T11).

The four example ``run.py`` files each carried a near-identical argparse
block plus a fragile ``REPO_ROOT`` resolution. This module centralises:

  * the common flag set (--scenario / --duration / --redis-host /
    --redis-port / --dry-run / --quiet / --start-sim / --sim-binary /
    --output), each overridable per-example;
  * the ``DEFAULT_SIM_BINARY`` computation (``build/<exe>``);
  * a :func:`bootstrap_paths` helper that resolves the example dir and
    repo root and inserts them into ``sys.path`` — **using a consistent
    parents depth**, fixing the historical ``parents[2]`` inconsistency
    in ``uav_search_track_car/run.py``.

Example layout (identical for all four examples)::

    examples/<name>/run.py   ← one level under examples/, two under repo
                                 root ⇒ REPO_ROOT = HERE.parents[1].

``uav_search_track_car/run.py`` previously used ``HERE.parents[2]`` which
is wrong (it points *above* the repo root); this module always derives
the repo root from the package location rather than the caller's file
depth, so the inconsistency is gone for good.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

# This file lives at examples/_common/argparser.py → repo root is two
# parents up from `examples/`.
_COMMON_DIR = Path(__file__).resolve().parent          # examples/_common
_EXAMPLES_DIR = _COMMON_DIR.parent                     # examples
REPO_ROOT = _EXAMPLES_DIR.parent                       # repo root


def default_sim_binary() -> Path:
    """Path to the opensim-sim executable under ``build/`` (platform-aware)."""
    return REPO_ROOT / "build" / (
        "opensim-sim.exe" if sys.platform == "win32" else "opensim-sim"
    )


def bootstrap_paths(example_dir: Path) -> Path:
    """Insert ``example_dir`` and the repo root into ``sys.path``.

    Returns the repo root (so callers can keep a module-level ``REPO_ROOT``
    constant if they like). Idempotent.
    """
    example_dir = Path(example_dir).resolve()
    for p in (str(example_dir), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return REPO_ROOT


def build_standard_parser(
    *,
    description: str,
    example_dir: Path,
    default_duration: float = 60.0,
    extra: Optional[list] = None,
) -> argparse.ArgumentParser:
    """Build the common argparse parser.

    Parameters
    ----------
    description:
        Top-level program description.
    example_dir:
        Example directory — used to compute default --scenario/--output.
    default_duration:
        Default ``--duration`` value (seconds). Varies per example.
    extra:
        Optional list of ``(args, kwargs)`` tuples; each is forwarded to
        ``add_argument``. Lets an example add its own flags (--controller,
        --road, --batch, ...) without re-declaring the common set.

    The returned parser exposes: --scenario, --duration, --redis-host,
    --redis-port, --dry-run, --quiet, --start-sim, --sim-binary,
    --output. ``--config`` is intentionally NOT added here (its default
    differs and some examples load YAML via different loaders); pass it
    via ``extra`` if needed.
    """
    p = argparse.ArgumentParser(description=description)
    p.add_argument(
        "--scenario", type=str,
        default=str(Path(example_dir) / "config" / "scenario.json"),
    )
    p.add_argument(
        "--duration", type=float, default=default_duration,
        help=f"sim seconds (default {default_duration:g})",
    )
    p.add_argument(
        "--output", type=str,
        default=str(Path(example_dir) / "output"),
    )
    p.add_argument(
        "--start-sim", action="store_true",
        help="spawn opensim-sim as a subprocess",
    )
    p.add_argument(
        "--sim-binary", type=str,
        default=os.environ.get("OPENSIM_SIM_BIN", str(default_sim_binary())),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="don't connect to Redis / don't publish commands",
    )
    p.add_argument("--redis-host", type=str, default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--quiet", action="store_true")
    if extra:
        for args, kwargs in extra:
            p.add_argument(*args, **kwargs)
    return p
