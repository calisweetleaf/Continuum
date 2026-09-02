"""
Goal and state encoders.
"""

from __future__ import annotations

import numpy as np

from ..core.mdp import State
from ..core.types import GoalSpec
from .hashing import HashingEncoder


def encode_goal(goal: GoalSpec, dim: int = 128) -> np.ndarray:
    tokens = [f"max_per_action:{goal.max_per_action}"]
    tokens.extend(f"predicate:{ref.name}:{ref.canonical()}" for ref in goal.predicates)
    return HashingEncoder(dim=dim, salt="goal").encode_tokens(tokens)


def encode_state(state: State, goal: GoalSpec | None = None, dim: int = 128) -> np.ndarray:
    tokens = [
        f"digest:{state.digest}",
        f"depth:{state.depth}",
        f"ctx_tail:{state.ctx[-256:]}",
    ]
    if goal is not None:
        tokens.extend(f"goal:{ref.canonical()}" for ref in goal.predicates)
    return HashingEncoder(dim=dim, salt="state").encode_tokens(tokens)


__all__ = ["encode_goal", "encode_state"]
