"""SimClient — minimal Redis wrapper for sim:commands / sim:state."""
from __future__ import annotations

import json
import time
from typing import Any

import redis

from .commands import ControlCommand
from .state import SimState, parse_sim_state


CMD_CHANNEL = "sim:commands"
STATE_CHANNEL = "sim:state"
EVENTS_CHANNEL = "sim:events"


class SimClient:
    """Thin wrapper around redis-py. Encapsulates connection + per-tick
    state read. Supports both 'uav' and 'target' command targets."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 6379,
        uav_id: str = "10002",
        target_id: str = "10001",
        uav_name: str = "uav",
        target_name: str = "target",
    ) -> None:
        self.host = host
        self.port = port
        self.uav_id = uav_id
        self.target_id = target_id
        self.uav_name = uav_name
        self.target_name = target_name
        self._redis: redis.Redis | None = None
        self._pubsub: redis.client.PubSub | None = None
        self._latest_state: SimState | None = None

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

    def wait_first_state(self, timeout: float = 120.0) -> SimState:
        """Block until the first sim:state message arrives or timeout.

        Default 120s: opensim-sim loads config/HeightSample.csv (~750MB) at
        startup before publishing the first sim:state, which takes ~100s on a
        typical machine — the legacy 5s default made --start-sim always time
        out. Override per-call if needed.
        """
        if self._pubsub is None:
            raise RuntimeError("SimClient not connected; call connect() first")
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._pubsub.get_message(timeout=0.5)
            if msg and msg.get("type") == "message":
                try:
                    raw = json.loads(msg["data"])
                    state = parse_sim_state(
                        raw, uav_id=self.uav_id, target_id=self.target_id
                    )
                    self._latest_state = state
                    return state
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
        raise TimeoutError(
            f"no sim:state received within {timeout}s — is opensim-sim running?"
        )

    def poll_latest(self, timeout: float = 0.05) -> SimState | None:
        """Drain any pending state messages and return the most recent one."""
        if self._pubsub is None:
            raise RuntimeError("SimClient not connected")
        latest: SimState | None = self._latest_state
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._pubsub.get_message(timeout=0.01)
            if not (msg and msg.get("type") == "message"):
                break
            try:
                raw = json.loads(msg["data"])
                latest = parse_sim_state(
                    raw, uav_id=self.uav_id, target_id=self.target_id
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        self._latest_state = latest
        return latest

    def publish(self, cmd: ControlCommand) -> int:
        if self._redis is None:
            raise RuntimeError("SimClient not connected")
        return self._redis.publish(CMD_CHANNEL, json.dumps(cmd.to_publish()))

    def publish_dict(self, d: dict[str, Any]) -> int:
        if self._redis is None:
            raise RuntimeError("SimClient not connected")
        return self._redis.publish(CMD_CHANNEL, json.dumps(d))

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
        """Publish a SimEvent to the ``sim:events`` channel per the
        ``sim-events-channel`` contract.

        Parameters
        ----------
        event_type:
            Namespaced event type (e.g. ``state.enter_track``).
        entity_uid:
            The unique_id of the entity this event relates to.
        sim_time:
            Current simulation time in seconds.
        payload:
            Event-type-specific payload dict.
        team:
            Optional team role (``white``/``red``/``blue``).
            Defaults to ``None`` → consumer falls back to ``white``.
        """
        if self._redis is None:
            raise RuntimeError("SimClient not connected")
        source: dict[str, Any] = {
            "kind": "external",
            "producer": "uav-search-track-car",
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

    def send_engine(self, verb: str) -> int:
        return self.publish_dict({"cmd": verb, "params": {}})

    def __enter__(self) -> "SimClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
