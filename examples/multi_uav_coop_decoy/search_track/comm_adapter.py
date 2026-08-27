"""Communication command adapter for the 017 cooperative example.

Builds the comm.broadcast / comm.send command payloads defined in
contracts/redis-protocol-extension.md, and enforces the 50-byte payload
cap client-side (FR-007) so we never issue a command the kernel will
reject.

Per contracts/redis-protocol-extension.md §4.1, comm commands do NOT use
a new CommandTarget enum value — they use the cmd prefix "comm.*" with the
sender's unique_id. We therefore expose a plain dataclass + to_publish()
that yields the kernel-side JSON shape directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_BYTES = 50


class PayloadTooLarge(ValueError):
    """Raised when a CommCommand payload exceeds the byte cap (FR-007)."""


@dataclass(frozen=True)
class CommCommand:
    """One inter-UAV communication command.

    receiver_uid=None -> broadcast (comm.broadcast)
    receiver_uid=str  -> point-to-point (comm.send)
    """
    sender_uid: str
    payload: str
    receiver_uid: str | None = None
    max_bytes: int = DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        # FR-007: client-side byte cap. payload is a str; encode as UTF-8
        # to count bytes (matches the kernel's std::string::size()).
        n = len(self.payload.encode("utf-8"))
        if n > self.max_bytes:
            raise PayloadTooLarge(
                f"payload is {n} bytes, exceeds cap of {self.max_bytes} "
                f"(FR-007); sender={self.sender_uid!r}"
            )

    @property
    def is_broadcast(self) -> bool:
        return self.receiver_uid is None

    def to_publish(self) -> dict[str, Any]:
        """Yield the JSON dict to publish on sim:commands."""
        params: dict[str, Any] = {"payload": self.payload}
        if self.receiver_uid is not None:
            params["peer_target_unique_id"] = self.receiver_uid
        return {
            "cmd": "comm.broadcast" if self.is_broadcast else "comm.send",
            "unique_id": self.sender_uid,
            "params": params,
        }


def broadcast(sender_uid: str, payload: str, *,
              max_bytes: int = DEFAULT_MAX_BYTES) -> CommCommand:
    """Helper: build a broadcast CommCommand."""
    return CommCommand(sender_uid=sender_uid, payload=payload,
                       receiver_uid=None, max_bytes=max_bytes)


def send_to(sender_uid: str, receiver_uid: str, payload: str, *,
            max_bytes: int = DEFAULT_MAX_BYTES) -> CommCommand:
    """Helper: build a point-to-point CommCommand (FR-003)."""
    return CommCommand(sender_uid=sender_uid, payload=payload,
                       receiver_uid=receiver_uid, max_bytes=max_bytes)
