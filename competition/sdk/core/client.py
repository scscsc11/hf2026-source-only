"""SimClient — Redis wrapper for sim:commands / sim:state.

A thin, scenario-agnostic wrapper around redis-py. It subscribes to
``sim:state``, parses each frame into a :class:`WorldState` (full truth,
runner-internal), and publishes commands to ``sim:commands`` with a forced
``unique_id`` (so a player cannot address another entity — invariant I-5).

Abstracted from ``examples/uav_search_track_car/search_track/client.py``
but uses :func:`parse_world_state` instead of the single-uav SimState
parser, and takes the ``unique_id`` at publish time rather than at
construction.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import redis

from .commands import Command
from .world_state import WorldState, parse_world_state


CMD_CHANNEL = "sim:commands"
STATE_CHANNEL = "sim:state"


class SimClient:
    """Redis pub/sub client. Context-managed: ``with SimClient(...) as c:``."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6379) -> None:
        self.host = host
        self.port = port
        self._redis: Optional[redis.Redis] = None
        self._pubsub: Any = None
        self._latest: Optional[WorldState] = None

    def connect(self) -> None:
        self._redis = redis.Redis(host=self.host, port=self.port,
                                  decode_responses=True)
        self._redis.ping()
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        self._pubsub.subscribe(STATE_CHANNEL)

    def close(self) -> None:
        if self._pubsub is not None:
            try:
                self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None
        if self._redis is not None:
            try:
                self._redis.close()
            except Exception:
                pass
            self._redis = None

    def wait_first_state(self, timeout: float = 120.0) -> WorldState:
        """Block until the first sim:state frame arrives or timeout."""
        if self._pubsub is None:
            raise RuntimeError("SimClient not connected; call connect() first")
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._pubsub.get_message(timeout=0.5)
            if msg and msg.get("type") == "message":
                try:
                    raw = json.loads(msg["data"])
                    self._latest = parse_world_state(raw)
                    return self._latest
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
        raise TimeoutError(
            f"no sim:state received within {timeout}s — is opensim-sim running?"
        )

    def poll_latest(self, timeout: float = 0.05) -> Optional[WorldState]:
        """Drain pending frames and return the most recent (or None)."""
        if self._pubsub is None:
            raise RuntimeError("SimClient not connected")
        latest = self._latest
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._pubsub.get_message(timeout=0.01)
            if not (msg and msg.get("type") == "message"):
                break
            try:
                raw = json.loads(msg["data"])
                latest = parse_world_state(raw)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        self._latest = latest
        return latest

    # ── publishing ────────────────────────────────────────────────────

    def publish(self, unique_id: str, cmd: Command) -> int:
        """Publish one Command, forcing ``unique_id`` (invariant I-5).

        Comm commands (``comm.broadcast``/``comm.send``) keep the same
        shape — the engine routes them by the sender's unique_id.
        """
        if self._redis is None:
            raise RuntimeError("SimClient not connected")
        msg = {"cmd": cmd.verb, "unique_id": unique_id, "params": cmd.params}
        return self._redis.publish(CMD_CHANNEL, json.dumps(msg))

    def publish_engine(self, verb: str) -> int:
        """Publish an engine-level command (pause/resume/step/end)."""
        if self._redis is None:
            raise RuntimeError("SimClient not connected")
        return self._redis.publish(CMD_CHANNEL,
                                   json.dumps({"cmd": verb, "params": {}}))

    def publish_raw(self, d: dict) -> int:
        """Publish a pre-built dict (for scenario trajectory injection)."""
        if self._redis is None:
            raise RuntimeError("SimClient not connected")
        return self._redis.publish(CMD_CHANNEL, json.dumps(d))

    def __enter__(self) -> "SimClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
