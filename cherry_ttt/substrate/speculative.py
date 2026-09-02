"""
Deterministic Tier-S observation prediction.

Speculative rollouts are allowed only when the prediction is explicitly
known.  There is no hallucinated observation fallback in this module:
unknown predictions raise, because simulated-rollout drift is one of the
proposal's core failure modes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.errors import ValidationError
from ..core.mdp import State
from ..core.types import ActionCandidate, Observation


@runtime_checkable
class ObservationPredictor(Protocol):
    """Tier-S predictor contract."""

    def predict(self, state: State, action: ActionCandidate) -> Observation: ...


@dataclass(frozen=True, slots=True)
class PredictionKey:
    """Canonical key for deterministic cached predictions."""

    state_digest: str
    action_id: str


class CachedObservationPredictor:
    """Exact cache predictor for idempotent reads and replayed actions.

    The cache is explicit.  A missing key is a hard validation error; it
    does not fabricate an observation shape.
    """

    def __init__(self) -> None:
        self._cache: dict[PredictionKey, Observation] = {}

    def record(self, state: State, action: ActionCandidate, observation: Observation) -> None:
        """Store an observed result for later Tier-S use."""
        self._cache[PredictionKey(str(state.digest), action.canonical())] = observation

    def predict(self, state: State, action: ActionCandidate) -> Observation:
        """Return the recorded observation for (state digest, action)."""
        key = PredictionKey(str(state.digest), action.canonical())
        try:
            return self._cache[key]
        except KeyError as exc:
            raise ValidationError(
                "no deterministic Tier-S prediction for "
                f"state={key.state_digest[:12]} action={key.action_id}; "
                "record the observation first or truncate the rollout"
            ) from exc


__all__ = ["CachedObservationPredictor", "ObservationPredictor", "PredictionKey"]
