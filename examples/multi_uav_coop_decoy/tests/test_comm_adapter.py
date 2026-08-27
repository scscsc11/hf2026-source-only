"""Tests for 017 comm adapter (FR-007 byte cap + command shapes)."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
EXAMPLE_DIR = HERE.parents[1]
for p in (str(REPO_ROOT), str(EXAMPLE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from search_track.comm_adapter import (
    CommCommand, PayloadTooLarge, broadcast, send_to,
)


def test_broadcast_command_shape():
    c = broadcast("UAV-0001", "hello")
    assert c.is_broadcast is True
    pub = c.to_publish()
    assert pub["cmd"] == "comm.broadcast"
    assert pub["unique_id"] == "UAV-0001"
    assert pub["params"]["payload"] == "hello"
    assert "peer_target_unique_id" not in pub["params"]


def test_send_to_command_shape():
    c = send_to("UAV-0001", "UAV-0002", "hi")
    assert c.is_broadcast is False
    pub = c.to_publish()
    assert pub["cmd"] == "comm.send"
    assert pub["unique_id"] == "UAV-0001"
    assert pub["params"]["peer_target_unique_id"] == "UAV-0002"
    assert pub["params"]["payload"] == "hi"


def test_payload_within_50_bytes_ok():
    # Exactly 50 bytes — allowed (boundary).
    c = broadcast("UAV-0001", "x" * 50)
    assert len(c.payload) == 50


def test_payload_over_50_bytes_rejected():
    # 51 bytes — must raise (FR-007).
    with pytest.raises(PayloadTooLarge):
        broadcast("UAV-0001", "x" * 51)


def test_payload_byte_count_uses_utf8():
    # Multi-byte UTF-8 char: '€' is 3 bytes. 17 chars = 51 bytes -> reject.
    with pytest.raises(PayloadTooLarge):
        broadcast("UAV-0001", "€" * 17)
    # 16 chars = 48 bytes -> ok.
    c = broadcast("UAV-0001", "€" * 16)
    assert len(c.payload.encode("utf-8")) == 48


def test_custom_max_bytes():
    c = broadcast("UAV-0001", "x" * 10, max_bytes=10)
    assert len(c.payload) == 10
    with pytest.raises(PayloadTooLarge):
        broadcast("UAV-0001", "x" * 11, max_bytes=10)
