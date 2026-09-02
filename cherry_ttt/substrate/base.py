"""
ExecutionSubstrate contract — the D2 load-bearing ruling, frozen Part II.

Source: CHERRY_TTT_BUILD_PLAN_v0.1.md Part II / D2; the transactional
    enforcement pattern generalizes the guard-then-delegate structure of
    the adapter layer in inference_protocols.py.
Integrated: 2026-07-05
Purpose: The only doorway between search and the world. Tier T
    (transactional) accepts READ / WRITE_REVERSIBLE exclusively —
    enforced by type via EffectViolation in a template method, never by
    convention or documentation (D2). restore(h) must be valid from any
    descendant state of h; MCTS requires arbitrary re-rooting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from ..core.errors import EffectViolation
from ..core.types import (
    ActionCandidate,
    Cost,
    EffectClass,
    EnvDigest,
    Observation,
    SnapshotHandle,
)

_TIER_T_ALLOWED = frozenset({EffectClass.READ, EffectClass.WRITE_REVERSIBLE})


@runtime_checkable
class ExecutionSubstrate(Protocol):
    """Structural contract every substrate satisfies (frozen, Part II)."""

    def execute(self, a: ActionCandidate) -> tuple[Observation, Cost]: ...

    def snapshot(self) -> SnapshotHandle: ...

    def restore(self, h: SnapshotHandle) -> None: ...

    def digest(self) -> EnvDigest: ...

    def effect_class(self, a: ActionCandidate) -> EffectClass: ...

    def snapshot_cost_estimate(self) -> Cost: ...


class TransactionalSubstrateBase(ABC):
    """Tier-T base: effect enforcement lives here, structurally.

    Concrete adapters implement _do_execute and never see forbidden
    actions — execute() is the sole public entry and it gates first.
    This is the type-level guarantee the plan demands: an adapter cannot
    forget to check, because the check is not the adapter's to perform.
    """

    def execute(self, a: ActionCandidate) -> tuple[Observation, Cost]:
        """Gate the action's effect class, then delegate to the adapter.

        Args:
            a: The candidate action.

        Returns:
            (Observation, Cost) from the adapter's _do_execute.

        Raises:
            EffectViolation: If effect_class(a) is WRITE_IRREVERSIBLE or
                EXTERNAL — before any adapter code runs.
        """
        cls = self.effect_class(a)
        if cls not in _TIER_T_ALLOWED:
            raise EffectViolation(
                f"action {a.tool_id!r} classified {cls.name} cannot execute "
                "in Tier T; EXTERNAL/IRREVERSIBLE effects run in Tier R "
                "exclusively (D2)"
            )
        return self._do_execute(a)

    @abstractmethod
    def _do_execute(self, a: ActionCandidate) -> tuple[Observation, Cost]:
        """Perform the already-gated action against real state."""

    @abstractmethod
    def snapshot(self) -> SnapshotHandle:
        """Capture restorable state; returns an opaque handle (D2)."""

    @abstractmethod
    def restore(self, h: SnapshotHandle) -> None:
        """Return to the state at h; valid from any descendant of h (D2)."""

    @abstractmethod
    def digest(self) -> EnvDigest:
        """Content hash of touched state only — never whole-world (D2)."""

    @abstractmethod
    def effect_class(self, a: ActionCandidate) -> EffectClass:
        """Classify a without executing it."""

    @abstractmethod
    def snapshot_cost_estimate(self) -> Cost:
        """Declared branching cost so search can budget (D2)."""
