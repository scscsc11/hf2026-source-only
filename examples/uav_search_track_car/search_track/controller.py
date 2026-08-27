"""Controller abstract base class and loader."""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from typing import Type

from .commands import ControlCommand
from .state import SimState


class Controller(ABC):
    """Pluggable search/track algorithm interface.

    Invariants (see contracts/controller-interface.md):
      I-1: decide() is pure (besides self-internal state).
      I-2: reset() is called before the first decide() of each run.
      I-3: Same instance may be reset() and reused for batch runs.
      I-4: decide()'s returned command list is order-sensitive.
      I-5: NEVER issues set_target_entity (banned in this example).
      I-6: In TRACK mode, set_enabled is always False.
    """

    @abstractmethod
    def decide(self, state: SimState, dt: float) -> list[ControlCommand]:
        ...

    def reset(self) -> None:
        return None

    @property
    def name(self) -> str:
        return self.__class__.__name__


def load_controller(spec: str) -> Controller:
    """Resolve 'module.path:ClassName' into an instance."""
    if ":" not in spec:
        raise ValueError(
            f"controller spec must be 'module.path:ClassName', got {spec!r}"
        )
    module_path, _, class_name = spec.rpartition(":")
    mod = importlib.import_module(module_path)
    cls: Type | None = getattr(mod, class_name, None)
    if cls is None:
        raise ImportError(f"{class_name!r} not found in module {module_path!r}")
    if not (isinstance(cls, type) and issubclass(cls, Controller)):
        raise TypeError(f"{spec} is not a Controller subclass")
    return cls()
