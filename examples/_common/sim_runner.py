"""opensim-sim subprocess launch + teardown (T11).

Every example runner had the same boilerplate: resolve the binary, check
it exists, ``Popen`` it with ``--config <scenario>`` and silent stdio,
``sleep`` a fixed boot delay, then a ``finally`` block that terminates
(5s grace) and kills. This module collects that into two helpers:

  * :func:`start_sim` — launch the subprocess and return it;
  * :func:`stop_sim` — the terminate/wait/kill teardown used in ``finally``.

The historical boot delay varied between examples (2.0s vs 3.0s); this
module exposes a single :data:`DEFAULT_BOOT_DELAY` and the examples now
all pass the same value.

Ready-poll (bug fix): the boot delay is *not* how long the sim needs to
become usable. On a cold start ``opensim-sim`` loads the 786 MB terrain
CSV before it subscribes to ``sim:commands`` / publishes ``sim:state``;
that can take anywhere from ~15 s (warm cache) to well over a minute
(cold disk). A fixed 2 s sleep made every ``--start-sim`` runner time
out in phase 1 ("No sim:state received") on slow machines, so the
controller never sent its first ``set_goal`` and the target vehicle
appeared to never maneuver. ``start_sim`` now polls the Redis
``PUBSUB NUMSUB`` count on the command channel and only returns once the
sim has actually subscribed (or ``ready_timeout`` elapses).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

# Unified boot delay (was 2.0 in three examples, 3.0 in
# uav_track_road_target). 2.0s is the historical majority and is enough
# for the sim process to exist; ``start_sim`` then polls Redis for real
# readiness (see :func:`_wait_sim_ready`).
DEFAULT_BOOT_DELAY = 2.0

# How long to wait for opensim-sim to finish init (terrain/grid load) and
# subscribe to the command channel. 180 s comfortably covers a cold 786 MB
# HeightSample.csv load on a slow disk; callers needing more can override.
DEFAULT_READY_TIMEOUT = 180.0


def _wait_sim_ready(
    redis_host: str, redis_port: int, channel: str,
    timeout: float, log, proc: "subprocess.Popen | None",
) -> None:
    """Block until ``opensim-sim`` has subscribed to ``channel``.

    The sim subscribes to the command channel only after init (loading the
    terrain + grid CSVs), so ``PUBSUB NUMSUB <channel>`` flipping to >= 1 is
    a reliable readiness signal — far more accurate than a fixed sleep.
    Returns silently if Redis is unreachable (best-effort), or if the sim
    process exits before becoming ready.
    """
    if timeout <= 0:
        return
    try:
        import redis
    except ImportError:
        return  # no redis module → cannot poll; rely on boot_delay only
    deadline = time.time() + timeout
    try:
        r = redis.Redis(host=redis_host, port=redis_port, socket_timeout=1.0)
    except Exception:
        return
    poll_interval = 1.0
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            log(f"[run] opensim-sim exited (code {proc.returncode}) before "
                "becoming ready")
            return
        try:
            n = r.execute_command("PUBSUB", "NUMSUB", channel)
            count = int(n[-1]) if n else 0
            if count >= 1:
                log(f"[run] opensim-sim ready (subscribed to {channel})")
                return
        except Exception:
            pass  # redis not up yet, or transient error — keep waiting
        time.sleep(poll_interval)
    log(f"[run] WARNING: opensim-sim did not subscribe to {channel} within "
        f"{timeout:.0f}s (still loading terrain?); proceeding anyway.")


def start_sim(
    sim_binary: str,
    scenario: str,
    *,
    log=None,
    boot_delay: float = DEFAULT_BOOT_DELAY,
    redis_host: str = "127.0.0.1",
    redis_port: int = 6379,
    ready_channel: str = "sim:commands",
    ready_timeout: float = DEFAULT_READY_TIMEOUT,
    stderr_file: Optional[str] = None,
) -> Optional[subprocess.Popen]:
    """Spawn opensim-sim as a subprocess and wait until it is ready.

    On success returns the Popen handle. On failure (binary missing) logs an
    error and returns ``None`` — the caller treats ``None`` as "do not run"
    (returns exit code 2). ``log`` defaults to :func:`print` but examples
    pass their quiet-aware logger.

    After spawning, sleeps ``boot_delay`` seconds (so the process exists),
    then polls Redis ``PUBSUB NUMSUB <ready_channel>`` until the sim has
    subscribed (i.e. finished init and is accepting commands) or
    ``ready_timeout`` elapses. This matters because terrain/grid CSV load
    dominates startup time (cold start: tens of seconds to >1 min) and the
    old fixed sleep made controllers send commands before the sim was
    listening, so target vehicles never maneuvered. Set ``ready_timeout=0``
    to skip the poll and rely on ``boot_delay`` alone.
    """
    if log is None:
        log = print
    bin_path = Path(sim_binary)
    if not bin_path.exists():
        log(f"[run] ERROR: opensim-sim not found at {bin_path}; build first")
        return None
    log(f"[run] starting opensim-sim: {bin_path} --config {scenario}")
    if stderr_file:
        Path(stderr_file).parent.mkdir(parents=True, exist_ok=True)
        stderr = open(stderr_file, "w", encoding="utf-8", errors="ignore")
    else:
        stderr = subprocess.DEVNULL
    proc = subprocess.Popen(
        [str(bin_path), "--config", scenario],
        stdout=subprocess.DEVNULL, stderr=stderr,
    )
    time.sleep(boot_delay)
    if proc.poll() is not None:
        log(f"[run] ERROR: opensim-sim exited immediately (code {proc.returncode})")
        return None
    _wait_sim_ready(redis_host, redis_port, ready_channel, ready_timeout, log, proc)
    return proc


def stop_sim(proc: Optional[subprocess.Popen]) -> None:
    """Terminate the sim subprocess: terminate → wait(5s) → kill.

    Safe to call with ``None`` (no-op) and on an already-exited process.
    Used in every example's ``finally`` block.
    """
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
