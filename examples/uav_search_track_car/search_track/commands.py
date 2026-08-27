"""Control command dataclass and target enum."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CommandTarget(str, Enum):
    UAV = "uav"
    ENGINE = "engine"


@dataclass(frozen=True)
class ControlCommand:
    target: CommandTarget
    cmd: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_publish(self) -> dict[str, Any]:
        return {
            "target": self.target.value,
            "cmd": self.cmd,
            "params": dict(self.params),
        }
