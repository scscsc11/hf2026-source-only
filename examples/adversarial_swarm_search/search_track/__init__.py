"""Search-track submodule (placeholder).

Re-exports the reusable pieces from spec 016/017 (search-track FSM and
comm adapter). The spec 019 swarm controller builds on these — concrete
implementations land in subsequent phases (US4/US5).
"""
from . import state  # noqa: F401
from . import config  # noqa: F401
from .swarm_controller import SwarmController  # noqa: F401
