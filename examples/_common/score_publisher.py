"""Real-time score publisher for the ``sim:score`` Redis channel (Spec 025).

The :class:`CoopTrackingEvaluator` already computes ``total_score`` and
seven ``dimension_scores`` per tick inside the Python control scripts of
the three scoring examples. This module wraps a thin publish-to-Redis
helper on top so the visualization front-end can subscribe to a single
``sim:score`` channel and render the live score curve.

Design notes (mirrors the project's existing fire-and-forget pattern in
:mod:`redis_sub`):

* The publisher opens its own ``redis.Redis`` connection on construction
  (``decode_responses=True``, like every other example client) and
  publishes JSON strings on the configured channel — exactly the wire
  format ``redis-cli SUBSCRIBE sim:score`` would emit.
* All publish calls are wrapped in ``try/except`` so a transient Redis
  outage cannot kill the 10 Hz control loop. The first error logs a
  warning; subsequent errors within the same ``ScorePublisher`` instance
  are silenced to avoid log spam.
* The channel name is hard-coded to ``"sim:score"`` (per the original
  feature request) but the constructor accepts a ``channel`` override
  for tests and for callers that prefer to read it from config.

Payload contract (consumed by the front-end ``ScorePanel``):

* Each :meth:`publish` call emits a single JSON object with the score
  snapshot produced by :meth:`CoopTrackingEvaluator.score`, augmented
  with ``sim_time``, ``tick``, ``final``, and ``ts`` (wall-clock unix
  seconds) so the front-end can render the live score and detect
  end-of-run independently of the C++ engine's ``sim:state``.
* :meth:`publish_final` is a convenience wrapper that emits a single
  ``final=true`` message — used by the example ``run.py`` files in
  their final-block after the control loop exits.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

# Channel name is intentionally hard-coded per the feature request:
# "redis 里的频道时 sim:score". Front-end imports the same constant via
# ``CHANNELS.score`` in ``visualization/src/rendering/constants.ts``.
SCORE_CHANNEL = "sim:score"

_log = logging.getLogger(__name__)


def _build_payload(
    score_dict: dict[str, Any],
    *,
    sim_time: Optional[float] = None,
    tick: Optional[int] = None,
    final: bool = False,
) -> dict[str, Any]:
    """Augment an evaluator score() result with the wire-protocol fields.

    Keeps the front-end payload shape stable independent of which fields
    the evaluator happens to populate this tick (e.g. ``alive_rate`` is
    only meaningful for the adversarial example).
    """
    payload = dict(score_dict)  # shallow copy — do not mutate caller's dict
    payload["type"] = "score_final" if final else "score"
    payload["final"] = bool(final)
    payload["ts"] = time.time()
    if sim_time is not None:
        payload["sim_time"] = float(sim_time)
    if tick is not None:
        payload["tick"] = int(tick)
    return payload


class ScorePublisher:
    """Publish score snapshots to a Redis channel.

    Args:
        host: Redis host (default ``"127.0.0.1"``).
        port: Redis port (default ``6379``).
        channel: Redis channel to publish to (default :data:`SCORE_CHANNEL`).
        connect: If ``False``, defer the Redis connection until the first
            :meth:`publish` call. Useful for tests that inject a fake
            client. Defaults to ``True``; a connection failure is logged
            and the publisher falls back to lazy mode so subsequent
            publishes can still attempt to reconnect.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        channel: str = SCORE_CHANNEL,
        *,
        connect: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.channel = channel
        self._client: Any = None
        self._warned: bool = False
        if connect:
            self._ensure_client()

    # ── public API ──────────────────────────────────────────────────────

    def publish(
        self,
        score_dict: dict[str, Any],
        *,
        sim_time: Optional[float] = None,
        tick: Optional[int] = None,
    ) -> bool:
        """Publish one tick's score. Returns True on success.

        A Redis outage is logged once and swallowed (returns False); the
        caller's 10 Hz loop must keep running.
        """
        payload = _build_payload(score_dict, sim_time=sim_time, tick=tick)
        return self._publish_payload(payload)

    def publish_final(
        self,
        score_dict: dict[str, Any],
        *,
        sim_time: Optional[float] = None,
        tick: Optional[int] = None,
        evaluation_path: Optional[str] = None,
    ) -> bool:
        """Publish a single ``final=true`` score frame (end of run)."""
        payload = _build_payload(
            score_dict, sim_time=sim_time, tick=tick, final=True
        )
        if evaluation_path:
            payload["evaluation_path"] = evaluation_path
        return self._publish_payload(payload)

    def close(self) -> None:
        """Release the Redis connection. Safe to call multiple times."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ── internals ───────────────────────────────────────────────────────

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import redis  # type: ignore
        except ImportError:
            if not self._warned:
                _log.warning(
                    "redis-py not installed; ScorePublisher disabled"
                )
                self._warned = True
            return None
        try:
            self._client = redis.Redis(
                host=self.host,
                port=self.port,
                decode_responses=True,
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
            )
        except Exception as e:
            if not self._warned:
                _log.warning(
                    "ScorePublisher failed to connect to Redis at "
                    "%s:%d (%s); will retry on next publish",
                    self.host, self.port, e,
                )
                self._warned = True
            self._client = None
        return self._client

    def _publish_payload(self, payload: dict[str, Any]) -> bool:
        client = self._ensure_client()
        if client is None:
            return False
        try:
            # allow_nan=False: 任一字段为 inf/NaN 立即抛 ValueError,而非静默
            # 产出非标准 token 'Infinity'/'NaN'。前端 JSON.parse 对它们抛
            # SyntaxError 会被静默吞掉,导致评分面板一直 '--'(难排查)。
            # 这里 fail-fast 让问题在发布侧立即暴露。与 _vendored 副本同步。
            client.publish(self.channel, json.dumps(payload, allow_nan=False))
            return True
        except Exception as e:
            if not self._warned:
                _log.warning(
                    "ScorePublisher publish failed on %s (%s); "
                    "subsequent failures will be silenced",
                    self.channel, e,
                )
                self._warned = True
            # Best-effort: drop the broken client so the next publish
            # tries to reconnect.
            self._client = None
            return False