"""Native reasoning/tool interleave contracts.

This module is intentionally agnostic to SRA, Varys, neural memory, and any
specific model runtime.  It defines the branch-local information surface a
reasoning runtime needs in order to emit tool actions *inside* a reasoning
trajectory without converting observations into assistant-style narration.

The execution/search kernel continues to own typed actions, reversible state,
and cost accounting.  A model adapter can implement ``ContextualActionProposer``
and consume the complete branch trajectory, including raw tool observations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.mdp import State
from ..core.types import ActionCandidate, GoalSpec, Trajectory, TrajectoryStep


def branch_id_for_trajectory(trajectory: Trajectory) -> str:
    """Stable branch identity from the canonical action sequence."""
    path = [step.action.canonical() for step in trajectory.steps]
    payload = json.dumps(path, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ReasoningContext:
    """Read-only branch-local context exposed to a native reasoning adapter.

    ``root_ctx`` is the original task/context supplied to the MDP.  ``state``
    identifies the current reversible environment branch.  ``trajectory`` is
    the complete typed action/observation history that produced that branch.
    No natural-language tool announcement is required and observation payloads
    are not discarded.
    """

    root_ctx: str
    state: State
    goal: GoalSpec
    trajectory: Trajectory

    @property
    def branch_id(self) -> str:
        """Stable identity shared by reasoning events and trajectory export."""
        return branch_id_for_trajectory(self.trajectory)

    @property
    def last_step(self) -> TrajectoryStep | None:
        """Return the latest action/observation transition, if one exists."""
        if not self.trajectory.steps:
            return None
        return self.trajectory.steps[-1]


@runtime_checkable
class ContextualActionProposer(Protocol):
    """Action proposer that consumes native branch trajectory state.

    Implementations may be backed by SRA, a policy head, a deterministic test
    policy, or another reasoning runtime.  The contract returns typed tool
    candidates directly; it does not require a textual ``call_tool`` turn.
    """

    def propose_with_context(
        self,
        context: ReasoningContext,
        n: int,
    ) -> list[tuple[ActionCandidate, float]]: ...


__all__ = ["ContextualActionProposer", "ReasoningContext", "branch_id_for_trajectory"]
