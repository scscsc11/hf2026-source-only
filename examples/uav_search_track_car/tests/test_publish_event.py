"""Unit tests for SimClient.publish_event() source assembly.

Verifies that the source object conforms to the sim:events channel contract.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# We test publish_event in isolation by mocking the redis connection.
class TestPublishEvent:
    """Spec 018: verify publish_event builds the correct SimEvent message."""

    def _make_client(self):
        """Create a SimClient with a mocked redis connection."""
        from search_track.client import SimClient

        client = SimClient.__new__(SimClient)
        client.host = "127.0.0.1"
        client.port = 6379
        client.uav_id = "10002"
        client.target_id = "10001"
        client.uav_name = "uav"
        client.target_name = "target"
        # Mock the redis publish method
        mock_redis = MagicMock()
        mock_redis.publish.return_value = 1
        client._redis = mock_redis
        client._pubsub = None
        client._latest_state = None
        return client

    def test_source_kind_is_external(self):
        """source.kind MUST be 'external' for controller events."""
        from search_track.client import EVENTS_CHANNEL

        client = self._make_client()
        client.publish_event(
            event_type="state.enter_track",
            entity_uid="10002",
            sim_time=42.0,
        )
        call_args = client._redis.publish.call_args
        assert call_args[0][0] == EVENTS_CHANNEL
        message = json.loads(call_args[0][1])
        assert message["source"]["kind"] == "external"

    def test_source_producer_is_set(self):
        """source.producer MUST be a non-empty string."""
        client = self._make_client()
        client.publish_event(
            event_type="state.enter_track",
            entity_uid="10002",
            sim_time=42.0,
        )
        message = json.loads(client._redis.publish.call_args[0][1])
        assert isinstance(message["source"]["producer"], str)
        assert len(message["source"]["producer"]) > 0

    def test_source_team_default_absent(self):
        """When team=None, source.team should be absent (consumer falls back white)."""
        client = self._make_client()
        client.publish_event(
            event_type="state.enter_track",
            entity_uid="10002",
            sim_time=42.0,
        )
        message = json.loads(client._redis.publish.call_args[0][1])
        assert "team" not in message["source"]

    def test_source_team_explicit(self):
        """When team='red', source.team MUST be 'red'."""
        client = self._make_client()
        client.publish_event(
            event_type="state.enter_track",
            entity_uid="10002",
            sim_time=42.0,
            team="red",
        )
        message = json.loads(client._redis.publish.call_args[0][1])
        assert message["source"]["team"] == "red"

    def test_top_level_fields_present(self):
        """event_type, entity_uid, sim_time, payload, source must all be present."""
        client = self._make_client()
        client.publish_event(
            event_type="target.discovered",
            entity_uid="10002",
            sim_time=10.5,
            payload={"confidence": 0.9},
        )
        message = json.loads(client._redis.publish.call_args[0][1])
        assert message["event_type"] == "target.discovered"
        assert message["entity_uid"] == "10002"
        assert message["sim_time"] == 10.5
        assert isinstance(message["payload"], dict)
        assert "source" in message

    def test_payload_defaults_to_empty(self):
        """When payload=None, payload should be an empty dict."""
        client = self._make_client()
        client.publish_event(
            event_type="state.exit_track",
            entity_uid="10002",
            sim_time=5.0,
        )
        message = json.loads(client._redis.publish.call_args[0][1])
        assert message["payload"] == {}

    def test_publishes_to_sim_events_channel(self):
        """Must publish to 'sim:events' channel."""
        from search_track.client import EVENTS_CHANNEL

        assert EVENTS_CHANNEL == "sim:events"
