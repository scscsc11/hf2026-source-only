"""Redis pubsub subscribe template (T11).

The four example runners subscribed to ``sim:state`` three different
ways:

  * adversarial: manual ``redis.Redis(...).pubsub()`` + a
    wait-for-subscribe-ack loop;
  * multi / search_track_car: via their own ``SimClient``/``MultiSimClient``
    wrappers (which already encapsulate the subscribe);
  * uav_track_road_target: ``pubsub(ignore_subscribe_messages=True)``
    with a manual first-state scan.

This module provides the bare-Redis subscribe helper used by the
adversarial runner (the only one that talks to ``redis-py`` directly
without a client wrapper). The SimClient-based examples keep their own
client (its subscribe is not duplicated glue worth pulling here); only
the standalone subscribe-ack pattern is shared.
"""
from __future__ import annotations

import time


def connect_redis(host: str, port: int, channel: str, timeout: float = 5.0):
    """Connect to Redis, subscribe to ``channel``, wait for the ack.

    Returns ``(redis_client, pubsub)``. Raises ``RuntimeError`` if
    redis-py is not installed or the subscribe-ack doesn't arrive within
    ``timeout`` seconds. The ``decode_responses=True`` setting matches
    every existing example so ``pubsub.get_message()`` returns ``str``
    payloads (not ``bytes``).
    """
    try:
        import redis  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "redis-py is required for live runs; install with `pip install redis`"
        ) from e
    r = redis.Redis(host=host, port=port, decode_responses=True)
    pubsub = r.pubsub()
    pubsub.subscribe(channel)
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = pubsub.get_message(timeout=0.1)
        if msg and msg.get("type") == "subscribe":
            return r, pubsub
    raise RuntimeError(
        f"Redis subscribe timed out ({channel} on {host}:{port})"
    )
