"""Metrics summary write-out + completion banner (T11).

Each example runner ended with the same shape:

  1. build a ``summary`` dict (example-specific contents);
  2. ``mkdir -p output``, write ``run_<timestamp>.json``;
  3. print a ``"SCENARIO COMPLETE"`` banner with selected fields.

The summary *contents* differ too much across examples to share
(adversarial tracks zones/discovery, multi tracks comm stats,
search-track-car uses its own MetricsRecorder, road-target tracks
laps), so the per-tick ``_summarise(state)`` stays in each example.
This module factors only the two genuinely-identical steps: writing the
JSON file and emitting the completion banner.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def write_summary_json(summary: dict, output_dir: str, *, log=None) -> Path:
    """Write ``summary`` to ``<output_dir>/run_<timestamp>.json``.

    Creates ``output_dir`` if needed. Returns the JSON path. Matches the
    historical convention used by every example (``indent=2``,
    ``ensure_ascii=False``).
    """
    if log is None:
        log = print
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    j_path = out_dir / f"run_{ts}.json"
    j_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    return j_path


def write_json(payload: dict, output_dir: str, filename: str, *, log=None) -> Path:
    """Write ``payload`` to ``<output_dir>/<filename>`` as JSON (indent=2).

    Used by the Spec 025 evaluation layer to write a per-run
    ``evaluation.json`` (total score, dimension breakdown, score timeline)
    alongside each example's existing metrics output.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / filename
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                 encoding="utf-8")
    return p


def print_completion_banner(
    title: str, lines: list[str], *, log=None, width: int = 60
) -> None:
    """Print a ``"=== <title> ==="`` banner with the given body lines.

    Mirrors the historical closing block of every runner: a separator of
    ``width`` ``=`` chars, a title line, one or more ``  key : value``
    lines, then another separator. Pass the title (e.g.
    ``"COOP SCENARIO COMPLETE"``) and the pre-formatted body lines.
    """
    if log is None:
        log = print
    log("")
    log("=" * width)
    log(title)
    for line in lines:
        log(line)
    log("=" * width)
