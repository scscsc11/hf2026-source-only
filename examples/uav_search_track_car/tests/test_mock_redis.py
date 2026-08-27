"""Tests for the in-memory MockSimClient (T012)."""
import json
import time

from .fixtures.mock_redis import MockSimClient


def test_publish_returns_subscriber_count():
    m = MockSimClient()
    s = m.subscribe("sim:state")
    try:
        n = m.publish("sim:state", {"hello": "world"})
        assert n == 1
    finally:
        s.close()
        m.close()


def test_subscriber_receives_published_message():
    m = MockSimClient()
    s = m.subscribe("sim:state")
    try:
        m.inject_state({"sim_time": 0.5, "status": "running"})
        deadline = time.time() + 1.0
        got = None
        while time.time() < deadline and got is None:
            got = s.get_message(timeout=0.1)
        assert got is not None
        assert got["type"] == "message"
        data = json.loads(got["data"])
        assert data["sim_time"] == 0.5
    finally:
        s.close()
        m.close()


def test_published_commands_recorded():
    m = MockSimClient()
    try:
        m.publish("sim:commands", {"target": "uav", "cmd": "set_destination", "params": {}})
        m.publish("sim:commands", {"target": "gimbal", "cmd": "set_orientation", "params": {}})
        cmds = m.published_commands()
        assert len(cmds) == 2
        assert cmds[0]["cmd"] == "set_destination"
    finally:
        m.close()
