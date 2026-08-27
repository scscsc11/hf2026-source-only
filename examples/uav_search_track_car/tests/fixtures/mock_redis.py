"""MockSimClient — in-memory Redis pub/sub stub for unit tests.

Provides the same minimal API used by SimClient (publish/subscribe/get_message)
without requiring a running Redis server.
"""
import json
import threading
from collections import defaultdict, deque
from typing import Any


class MockSimClient:
    """Thread-safe in-memory pub/sub. Supports multiple subscribers per channel."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[deque]] = defaultdict(list)
        self._published: list[tuple[str, dict]] = []
        self._closed = False

    def publish(self, channel: str, message: Any) -> int:
        """Publish a JSON-encodable message to a channel. Returns subscriber count."""
        if self._closed:
            raise RuntimeError("MockSimClient is closed")
        if isinstance(message, str):
            payload = message
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError:
                parsed = {"_raw": message}
        else:
            payload = json.dumps(message)
            parsed = message
        with self._lock:
            self._published.append((channel, parsed))
            queues = self._subscribers.get(channel, [])
            for q in queues:
                q.append(payload)
            return len(queues)

    def subscribe(self, channel: str) -> "_MockSubscriber":
        return _MockSubscriber(self, channel)

    def published_commands(self) -> list[dict]:
        """All messages published to the commands channel, parsed."""
        with self._lock:
            return [msg for ch, msg in self._published if ch == "sim:commands"]

    def inject_state(self, state: dict) -> None:
        """Push a sim:state message to all subscribers of that channel."""
        self.publish("sim:state", state)

    def close(self) -> None:
        self._closed = True


class _MockSubscriber:
    def __init__(self, client: MockSimClient, channel: str):
        self._client = client
        self._channel = channel
        self._queue: deque = deque()
        self._closed = False
        with client._lock:
            client._subscribers[channel].append(self._queue)

    def get_message(self, timeout: float = 0.0):
        if self._closed:
            return None
        try:
            payload = self._queue.popleft()
        except IndexError:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return {"type": "message", "data": json.dumps(data), "channel": self._channel}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._client._lock:
            queues = self._client._subscribers.get(self._channel, [])
            if self._queue in queues:
                queues.remove(self._queue)
