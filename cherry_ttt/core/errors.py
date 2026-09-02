"""
Contract-violation hierarchy for cherry_ttt.

Source: inference_protocols.py (Cherry RL pipeline) — two-root pattern:
    InferenceProtocolError(RuntimeError) for contract breaches,
    ProtocolValidationError(ValueError) for invalid inputs/outputs.
Integrated: 2026-07-05
Purpose: Generalizes the two-root pattern to the TTT contract surface.
    Runtime-root errors mean a frozen contract (build plan Part II) was
    violated during execution; value-root errors mean the caller handed
    the framework something malformed before execution began.

Every error class is domain-specific; no code anywhere in cherry_ttt may
raise or catch bare Exception (Codex v2 rule 1D).
"""

from __future__ import annotations


class CherryTTTError(RuntimeError):
    """Root for all runtime contract violations inside cherry_ttt."""


class ContractViolation(CherryTTTError):
    """A frozen interface contract (build plan Part II) was breached at runtime."""


class EffectViolation(ContractViolation):
    """An action with a forbidden EffectClass reached a substrate tier that
    must not execute it (D2: Tier T accepts only READ / WRITE_REVERSIBLE).

    Enforced structurally by TransactionalSubstrate.execute — this error is
    the type-level guarantee, never a logged warning.
    """


class SnapshotError(ContractViolation):
    """snapshot()/restore() broke the substrate soundness contract (D2):
    restore from a descendant state failed, or a handle was invalid."""


class SoundnessError(SnapshotError):
    """Post-restore digest differs from pre-snapshot digest (proposal §8.5).

    This is the safety-critical failure: it gates everything in speculate/.
    A SoundnessError is a hard stop for the episode, never recoverable.
    """


class CanonicalizationError(ValueError):
    """Input cannot be canonicalized under RFC 8785 (D3): non-finite float,
    non-JSON-serializable payload, or a key that is not a str."""


class ValidationError(ValueError):
    """Caller-supplied object failed pre-execution validation
    (pattern: ProtocolValidationError)."""


class LedgerViolation(CherryTTTError):
    """A standing invariant (build plan Part IV) was breached — e.g. a Cost
    collapsed outside CostWeights.collapse, or search touched a substrate
    without going through MDP.transition."""
