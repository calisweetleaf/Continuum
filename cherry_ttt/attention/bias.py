"""
Structured bias/mask utilities for CandidateAttention.

The bias matrix carries the domain semantics: hard type/namespace/ACL
masks and soft cost/staleness penalties.  Hard masks use -inf and are
applied before softmax/top-k exactly like the proposal specifies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True, slots=True)
class CandidateMeta:
    """Metadata attached to one candidate embedding."""

    namespace: str
    type_name: str
    acl: str = "default"
    cost: float = 0.0
    timestamp: float = 0.0


@dataclass(frozen=True, slots=True)
class BiasQuery:
    """Per-query routing constraints."""

    allowed_namespaces: frozenset[str] | None = None
    allowed_types: frozenset[str] | None = None
    allowed_acls: frozenset[str] | None = None
    cost_weight: float = 0.0
    staleness_weight: float = 0.0
    now: float = 0.0


def build_structured_bias(
    queries: Sequence[BiasQuery],
    candidates: Sequence[CandidateMeta],
) -> np.ndarray:
    """Build an MxN bias matrix.

    Hard incompatibility receives -inf; soft penalties are additive.
    """
    bias = np.zeros((len(queries), len(candidates)), dtype=np.float32)
    for i, query in enumerate(queries):
        for j, candidate in enumerate(candidates):
            blocked = (
                (query.allowed_namespaces is not None
                 and candidate.namespace not in query.allowed_namespaces)
                or (query.allowed_types is not None
                    and candidate.type_name not in query.allowed_types)
                or (query.allowed_acls is not None
                    and candidate.acl not in query.allowed_acls)
            )
            if blocked:
                bias[i, j] = -math.inf
                continue
            penalty = query.cost_weight * max(0.0, candidate.cost)
            if query.staleness_weight:
                age = max(0.0, query.now - candidate.timestamp)
                penalty += query.staleness_weight * age
            bias[i, j] = -float(penalty)
    return bias


__all__ = ["BiasQuery", "CandidateMeta", "build_structured_bias"]
