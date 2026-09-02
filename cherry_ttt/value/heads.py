"""
State value heads.

Phase-1 value machinery is deterministic and explicitly calibrated; no
learned model is required to run search.  Learned heads can implement
the same callable shape later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

import numpy as np

from ..core.errors import ValidationError
from ..core.mdp import State
from ..core.types import GoalSpec


@runtime_checkable
class StateValueLike(Protocol):
    """Callable value estimator over environment states."""

    def score(self, state: State, goal: GoalSpec | None = None) -> float: ...


@dataclass(frozen=True, slots=True)
class LinearStateValue:
    """Small linear value head over precomputed feature vectors."""

    weights: np.ndarray
    bias: float = 0.0

    def __post_init__(self) -> None:
        w = np.asarray(self.weights, dtype=np.float32)
        if w.ndim != 1:
            raise ValidationError("LinearStateValue.weights must be a 1D vector")
        object.__setattr__(self, "weights", w)

    def score_vector(self, features: np.ndarray) -> float:
        f = np.asarray(features, dtype=np.float32)
        if f.shape != self.weights.shape:
            raise ValidationError(f"features shape {f.shape} != weights shape {self.weights.shape}")
        return float(f @ self.weights + self.bias)


@dataclass(frozen=True, slots=True)
class ConformalValueWrapper:
    """Conservative calibration wrapper for value estimates.

    The wrapper subtracts an empirical error quantile.  It never claims
    admissibility by itself; callers declare heuristic regimes separately
    in search.astar.DeclaredHeuristic.
    """

    base: LinearStateValue
    residual_quantile: float = 0.0

    @classmethod
    def from_residuals(
        cls,
        base: LinearStateValue,
        residuals: Iterable[float],
        alpha: float = 0.1,
    ) -> "ConformalValueWrapper":
        values = np.asarray(list(residuals), dtype=np.float32)
        if values.size == 0:
            raise ValidationError("ConformalValueWrapper requires residual data")
        if not 0.0 < alpha < 1.0:
            raise ValidationError("alpha must be in (0, 1)")
        q = float(np.quantile(values, 1.0 - alpha))
        return cls(base=base, residual_quantile=q)

    def score_vector(self, features: np.ndarray) -> float:
        return self.base.score_vector(features) - self.residual_quantile


__all__ = ["ConformalValueWrapper", "LinearStateValue", "StateValueLike"]
