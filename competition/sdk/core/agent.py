"""Agent abstract base class — the player's contract.

A player implements exactly one thing: ``decide(obs, dt) -> list[Command]``.
The runner drives the lifecycle (instantiate → configure → reset → decide
loop). See contracts/agent-interface.md for the full contract and
invariants.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional

from .commands import Command
from .observation import Detection, Observation, SKIP_DETECTION


class Agent(ABC):
    """Base class for every scenario's player agent.

    Lifecycle (driven by the runner; the player never calls these on
    themselves except via ``super()``):

      1. ``Agent(my_uid)``  — the runner constructs one instance per
         controllable entity, passing that entity's unique_id.
      2. ``configure(config)`` — inject static task/algorithm params.
      3. ``reset()``  — called once before the first decide() of each run.
      4. ``decide(obs, dt)``  — called each decision cycle (~10 Hz).

    Invariants (contracts/agent-interface.md):
      I-1: decide() is pure aside from self-internal state; no Redis/file/net.
      I-2: reset() is called before the first decide() of each run.
      I-3: the returned command list is order-sensitive (published in order).
      I-4: the agent must not attempt to reach global info beyond ``obs``.
      I-5: commands only affect ``self.my_uid`` — the runner forces this.
    """

    def __init__(self, my_uid: str):
        self.my_uid = my_uid

    def configure(self, config: Any) -> None:
        """Inject static task/algorithm parameters. Optional override.

        ``config`` comes from the scenario's algorithm config / CLI injection
        and is constant for the whole run. It MUST NOT carry dynamic entity
        state (teammate poses, target truth) — that would violate isolation.
        """
        return None

    @abstractmethod
    def decide(self, obs: Observation, dt: float) -> List[Command]:
        """Core decision method, called each decision cycle.

        Args:
            obs: this cycle's observation (self/comm_inbox/briefing only).
            dt: seconds since the last decide() call.
        Returns:
            A list of Commands, published in order to ``self.my_uid``.
            Return an empty list to issue no commands this cycle.
        """
        ...

    def sensor(self, obs: Observation, dt: float) -> Optional[List[Detection]]:
        """感知回调。可选覆盖。四态返回（spec 029 §2）：

          List[Detection]（非空） → 自研识别结果，填入 obs.self.detection 供 decide 用
          []（空列表）            → 选手明确"本帧无检测"，detection 置为
                                    Detection(detected=False)（detection_source=user）
          SKIP_DETECTION         → 端到端：不需要 detection，跳过默认识别器
          None                   → 等同未覆盖，用默认识别器

        未覆盖此方法（基类默认）→ 用默认识别器（AccuracySimulator/YoloDetector）。
        """
        return None

    def reset(self) -> None:
        """Called before the first decide() of each run. Optional override."""
        return None

    @property
    def name(self) -> str:
        return self.__class__.__name__
