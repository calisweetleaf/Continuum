"""
Deterministic hashed feature encoders.

These encoders are phase-1 compliant: no trained parameters, no torch,
and no tool-id memorization requirement.  They convert structural JSON
objects into fixed-size numpy vectors suitable for retrieval baselines,
CandidateAttention, and value stubs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from ..core.jcs import canonicalize


@dataclass(frozen=True, slots=True)
class HashingEncoder:
    """Signed feature hashing into a fixed-dimensional float vector."""

    dim: int = 128
    salt: str = "cherry_ttt"

    def encode_tokens(self, tokens: Iterable[str]) -> np.ndarray:
        if self.dim <= 0:
            raise ValueError("HashingEncoder.dim must be positive")
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in tokens:
            digest = hashlib.sha256(f"{self.salt}:{token}".encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dim
            sign = 1.0 if digest[8] & 1 else -1.0
            vec[index] += sign
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec

    def encode_json(self, obj: Any, prefix: str = "json") -> np.ndarray:
        """Canonical JSON object -> character n-gram features."""
        text = canonicalize(obj)
        tokens = [f"{prefix}:len:{len(text)}"]
        tokens.extend(f"{prefix}:ch:{ch}" for ch in text)
        tokens.extend(f"{prefix}:tri:{text[i:i+3]}" for i in range(max(0, len(text) - 2)))
        return self.encode_tokens(tokens)


__all__ = ["HashingEncoder"]
