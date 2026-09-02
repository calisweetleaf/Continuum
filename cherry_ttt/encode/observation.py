"""
Observation encoders for typed tool outputs.
"""

from __future__ import annotations

import numpy as np

from ..core.types import Observation
from .hashing import HashingEncoder


def encode_observation(obs: Observation, dim: int = 128) -> np.ndarray:
    """Encode observation kind and digestible payload bytes."""
    tokens = [f"kind:{obs.kind}", f"bytes:{obs.digestible().hex()}"]
    return HashingEncoder(dim=dim, salt="observation").encode_tokens(tokens)


__all__ = ["encode_observation"]
