"""Core contract surface: frozen Part II types, errors, canonical identity."""

from __future__ import annotations

from .errors import (
    CanonicalizationError,
    CherryTTTError,
    ContractViolation,
    EffectViolation,
    LedgerViolation,
    SnapshotError,
    SoundnessError,
    ValidationError,
)
from .jcs import canonicalize
from .types import (
    PHASE1_WEIGHTS,
    ActionCandidate,
    Cost,
    CostWeights,
    EffectClass,
    EnvDigest,
    GoalSpec,
    Observation,
    PredicateRef,
    SnapshotHandle,
    TerminalStatus,
    Trajectory,
    TrajectoryStep,
    env_digest,
)

__all__ = [
    "ActionCandidate", "CanonicalizationError", "CherryTTTError",
    "ContractViolation", "Cost", "CostWeights", "EffectClass", "EffectViolation",
    "EnvDigest", "GoalSpec", "LedgerViolation", "Observation", "PHASE1_WEIGHTS",
    "PredicateRef", "SnapshotError", "SnapshotHandle", "SoundnessError",
    "TerminalStatus", "Trajectory", "TrajectoryStep", "ValidationError",
    "canonicalize", "env_digest",
]
