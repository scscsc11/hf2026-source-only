"""Multi-entity SimClient for the 017 cooperative example.

Wraps redis-py and parses each sim:state frame into a MultiSimState
(multi-UAV + multi-vehicle view) via parse_multi_sim_state. Reuses the
same two channels (sim:commands / sim:state) as 016 — no new channels.

The client is UAV-agnostic: it does not assume a fixed uav_id. Instead it
discovers UAVs from each state frame by scanning entities of kind=="uav".
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis

from .multi_state import MultiSimState, parse_multi_sim_state


CMD_CHANNEL = "sim:commands"
STATE_CHANNEL = "sim:state"
EVENTS_CHANNEL = "sim:events"


class MultiSimClient:
    """Redis wrapper that yields MultiSimState per tick.

    Unlike 016's SimClient (single uav_id/target_id), this client parses
    ALL entities in each sim:state frame, so it supports 3 UAVs + 18
    vehicles out of the box.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6379) -> None:
        self.host = host
        self.port = port
        self._redis: redis.Redis | None = None
        self._pubsub: redis.client.PubSub | None = None
        self._latest_state: MultiSimState | None = None

    def connect(self) -> None:
        self._redis = redis.Redis(
            host=self.host, port=self.port, decode_responses=True
        )
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

    def wait_first_state(self, timeout: float = 120.0) -> MultiSimState:
        # Default 120s: opensim-sim loads HeightSample.csv (~750MB) before the
        # first sim:state; the legacy 5s default timed out under --start-sim.
        if self._pubsub is None:
            raise RuntimeError("MultiSimClient not connected; call connect() first")
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._pubsub.get_message(timeout=0.5)
            if msg and msg.get("type") == "message":
                try:
                    raw = json.loads(msg["data"])
                    state = parse_multi_sim_state(raw)
                    self._latest_state = state
                    return state
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
        raise TimeoutError(
            f"no sim:state received within {timeout}s — is opensim-sim running?"
        )

    def poll_latest(self, timeout: float = 0.05) -> MultiSimState | None:
        if self._pubsub is None:
            raise RuntimeError("MultiSimClient not connected")
        latest: MultiSimState | None = self._latest_state
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._pubsub.get_message(timeout=0.01)
            if not (msg and msg.get("type") == "message"):
                break
            try:
                raw = json.loads(msg["data"])
                latest = parse_multi_sim_state(raw)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        self._latest_state = latest
        return latest

    def publish_dict(self, d: dict[str, Any]) -> int:
        if self._redis is None:
            raise RuntimeError("MultiSimClient not connected")
        return self._redis.publish(CMD_CHANNEL, json.dumps(d))

    def send_engine(self, verb: str) -> int:
        return self.publish_dict({"cmd": verb, "params": {}})

    # ── Spec 018: publish a structured event to sim:events ──

    def publish_event(
        self,
        *,
        event_type: str,
        entity_uid: str,
        sim_time: float,
        payload: dict[str, Any] | None = None,
        team: str | None = None,
    ) -> int:
        """Publish a SimEvent to the ``sim:events`` channel.

        See ``specs/018-3d-ui-event-wall-entity-ctrl/contracts/sim-events-channel.md``.
        """
        if self._redis is None:
            raise RuntimeError("MultiSimClient not connected")
        source: dict[str, Any] = {
            "kind": "external",
            "producer": "multi-uav-coop-decoy",
        }
        if team is not None:
            source["team"] = team
        message: dict[str, Any] = {
            "event_type": event_type,
            "source": source,
            "entity_uid": entity_uid,
            "sim_time": sim_time,
            "payload": payload or {},
        }
        return self._redis.publish(EVENTS_CHANNEL, json.dumps(message))

    def __enter__(self) -> "MultiSimClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
