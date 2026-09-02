"""
Trajectory encoder.
"""

from __future__ import annotations

import numpy as np

from ..core.types import Trajectory
from .hashing import HashingEncoder


def encode_trajectory(traj: Trajectory, dim: int = 128) -> np.ndarray:
    tokens = [f"initial:{traj.initial_digest}", f"status:{traj.status.name}"]
    for index, step in enumerate(traj.steps):
        tokens.append(f"{index}:action:{step.action.tool_id}:{step.action.canonical()}")
        tokens.append(f"{index}:obs:{step.observation.kind}:{step.observation.digestible().hex()}")
        tokens.append(
            f"{index}:cost:{step.cost.wall_ms}:{step.cost.model_tokens}:"
            f"{step.cost.env_calls}:{step.cost.risk}"
        )
        tokens.append(f"{index}:digest:{step.post_digest}")
    return HashingEncoder(dim=dim, salt="trajectory").encode_tokens(tokens)


__all__ = ["encode_trajectory"]
