"""
Core value types for cherry_ttt — the frozen Part II contracts.

Source: CHERRY_TTT_BUILD_PLAN_v0.1.md Part II (signatures frozen 2026-07-05);
    generalized from inference_optimizations.py where the degenerate lexical
    forms live (token-string actions, free transitions, scalar costs).
Integrated: 2026-07-05
Purpose: The typed contract surface C = <S_env, A, O, E, Sigma, c> from
    proposal §1.1. Actions are structured objects, never strings; Cost is
    a vector, never pre-collapsed (D4); action identity is canonical()
    everywhere identity matters (D3).

Changing any signature in this module after P1 requires a ledger
amendment, not a silent edit (build plan Part II preamble).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal, Mapping, NewType

from .errors import CanonicalizationError, LedgerViolation, ValidationError
from .jcs import canonicalize

# ---------------------------------------------------------------------------
# Opaque handles
# ---------------------------------------------------------------------------

EnvDigest = NewType("EnvDigest", str)
"""Content hash (hex) of touched environment state — the transposition key
(proposal §2.3, D2: Merkle over touched paths, never whole-world)."""


def env_digest(data: bytes) -> EnvDigest:
    """Compute the standard EnvDigest of raw digestible bytes.

    Args:
        data: Concatenated digestible() bytes of touched state, in the
            substrate's declared canonical order.

    Returns:
        Lowercase hex sha256 as an EnvDigest.
    """
    return EnvDigest(hashlib.sha256(data).hexdigest())


@dataclass(frozen=True, slots=True)
class SnapshotHandle:
    """Opaque reference to a substrate snapshot (D2).

    Search never inspects the token; substrates alone interpret it.
    seq provides a total order so adapters can verify restore-from-any-
    descendant (D2) without parsing tokens.
    """

    substrate_id: str
    token: str
    seq: int = 0


class EffectClass(Enum):
    """Effect classification of an action against a substrate (D2).

    Tier T (transactional search) accepts only READ / WRITE_REVERSIBLE;
    EXTERNAL executes in Tier R exclusively — enforced by type via
    EffectViolation, never by convention.
    """

    READ = auto()
    WRITE_REVERSIBLE = auto()
    WRITE_IRREVERSIBLE = auto()
    EXTERNAL = auto()


class TerminalStatus(Enum):
    """MDP terminal classification (Part II, core/mdp.py contract)."""

    SOLVED = auto()
    BUDGET = auto()
    ERROR = auto()
    OPEN = auto()


# ---------------------------------------------------------------------------
# Actions and observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class ActionCandidate:
    """A typed action a = (tool_id, args) drawn from schema sigma(tool_id).

    The single most consequential change from the lexical codebase
    (proposal §1.1): actions are structured objects, not strings.
    Equality and hashing are defined by canonical() — one definition of
    identity for transposition tables, acceptance tests, and dedup (D3).
    """

    tool_id: str
    # Any: args are schema-typed downstream (Sigma registry, P3); at the
    # core-type level they are open JSON values by design.
    args: Mapping[str, Any] = field(default_factory=dict)

    def canonical(self) -> str:
        """Return the D3 canonical identity: sha256(tool_id + jcs(args))[:16].

        Returns:
            16 hex chars — collision-safe at any plausible frontier size
            (64 bits; birthday bound ~ 2**32 candidates).

        Raises:
            CanonicalizationError: If args contain non-JSON or non-finite
                values. Float rounding to schema precision happens at the
                schema boundary before construction (D3), not here.
        """
        payload = self.tool_id + canonicalize(self.args)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ActionCandidate):
            return NotImplemented
        return self.canonical() == other.canonical()

    def __hash__(self) -> int:
        return hash(self.canonical())


@dataclass(frozen=True, slots=True)
class Observation:
    """A tool return value in observation space O (proposal §1.1).

    kind discriminates the payload shape; payload is Any because
    observation space is substrate-defined by construction — typed
    observation heads (encode/observation.py) impose structure later.
    """

    kind: Literal["result", "error", "diff", "scalar", "empty"]
    payload: Any = None

    def digestible(self) -> bytes:
        """Return deterministic bytes for digest/transposition purposes.

        Returns:
            UTF-8 bytes of the canonical JSON of (kind, payload); raw
            bytes payloads are hex-tagged rather than rejected, since
            substrates legitimately return binary observations.

        Raises:
            CanonicalizationError: If payload is neither JSON-compatible
                nor bytes.
        """
        payload: Any = self.payload
        if isinstance(payload, (bytes, bytearray)):
            payload = {"__bytes_hex__": bytes(payload).hex()}
        return canonicalize({"kind": self.kind, "payload": payload}).encode("utf-8")


# ---------------------------------------------------------------------------
# Cost — vector, never pre-collapsed (D4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cost:
    """Vector cost (wall_ms, model_tokens, env_calls, risk), carried
    un-collapsed through the whole system (D4).

    Pre-collapsed costs destroy the ablation axes and make the
    gamma-controller's latency accounting impossible (proposal §9.6).
    Collapse happens only through CostWeights.collapse at a search
    boundary (standing invariant 2).
    """

    wall_ms: float = 0.0
    model_tokens: int = 0
    env_calls: int = 0
    risk: float = 0.0

    def __add__(self, other: "Cost") -> "Cost":
        if not isinstance(other, Cost):
            return NotImplemented
        return Cost(
            wall_ms=self.wall_ms + other.wall_ms,
            model_tokens=self.model_tokens + other.model_tokens,
            env_calls=self.env_calls + other.env_calls,
            risk=self.risk + other.risk,
        )

    def __radd__(self, other: object) -> "Cost":
        # Enables sum(costs) with the int 0 start value; any other type
        # is a contract breach, not silent coercion.
        if other == 0:
            return self
        return NotImplemented

    def __float__(self) -> float:
        raise LedgerViolation(
            "Cost must never be collapsed implicitly; use "
            "CostWeights.collapse at a search boundary (standing invariant 2)"
        )


@dataclass(frozen=True, slots=True)
class CostWeights:
    """The declared lambda-vector that collapses Cost to a scalar at a
    search boundary (D4). Phase-1 default: pure env_calls count, which
    makes the admissible-heuristic regime exact and the oracle
    computable (D4 rationale)."""

    wall_ms: float
    model_tokens: float
    env_calls: float
    risk: float

    def collapse(self, c: Cost) -> float:
        """Collapse a vector Cost to the scalar the search consumes.

        Args:
            c: The un-collapsed cost vector.

        Returns:
            Dot product against this weight vector.
        """
        return (
            self.wall_ms * c.wall_ms
            + self.model_tokens * c.model_tokens
            + self.env_calls * c.env_calls
            + self.risk * c.risk
        )


PHASE1_WEIGHTS = CostWeights(wall_ms=0.0, model_tokens=0.0, env_calls=1.0, risk=0.0)
"""D4 default for phase 1: pure env-call count."""


# ---------------------------------------------------------------------------
# Goals and trajectories
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class PredicateRef:
    """Reference to a registered predicate with bound parameters.

    Identity follows the D3 discipline (canonical hash) so GoalSpecs
    dedup and compare structurally.
    """

    name: str
    # Any: predicate params are plugin-defined JSON values by design.
    params: Mapping[str, Any] = field(default_factory=dict)

    def canonical(self) -> str:
        """Return sha256(name + jcs(params))[:16], the D3 identity."""
        payload = self.name + canonicalize(self.params)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PredicateRef):
            return NotImplemented
        return self.canonical() == other.canonical()

    def __hash__(self) -> int:
        return hash(self.canonical())


@dataclass(frozen=True, slots=True)
class GoalSpec:
    """Decomposable goal: a tuple of independently checkable predicates
    (proposal §10.1) plus k = max predicates satisfiable per action,
    which parameterizes the admissible heuristic |unsat|/k (§2.4)."""

    predicates: tuple[PredicateRef, ...]
    max_per_action: int = 1

    def __post_init__(self) -> None:
        if self.max_per_action < 1:
            raise ValidationError(
                f"GoalSpec.max_per_action must be >= 1, got {self.max_per_action}; "
                "the admissible heuristic |unsat|/k divides by it"
            )


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    """One committed transition: (action, observation, cost, post-digest)."""

    action: ActionCandidate
    observation: Observation
    cost: Cost
    post_digest: EnvDigest


@dataclass(frozen=True, slots=True)
class Trajectory:
    """tau = <s0, a0, o0, s1, ...> as an immutable step sequence with the
    initial digest; the object rewards are defined over (proposal §1.1)
    and the object the collectors emit (§6.3)."""

    initial_digest: EnvDigest
    steps: tuple[TrajectoryStep, ...] = ()
    status: TerminalStatus = TerminalStatus.OPEN

    def total_cost(self) -> Cost:
        """Return the un-collapsed vector sum of all step costs."""
        total = Cost()
        for step in self.steps:
            total = total + step.cost
        return total

    def extended(self, step: TrajectoryStep, status: TerminalStatus | None = None) -> "Trajectory":
        """Return a new Trajectory with step appended (immutability-preserving).

        Args:
            step: The committed transition to append.
            status: Optional terminal reclassification; defaults to current.

        Returns:
            A new Trajectory; self is never mutated.
        """
        return Trajectory(
            initial_digest=self.initial_digest,
            steps=self.steps + (step,),
            status=self.status if status is None else status,
        )


__all__ = [
    "ActionCandidate",
    "CanonicalizationError",
    "Cost",
    "CostWeights",
    "EffectClass",
    "EnvDigest",
    "GoalSpec",
    "Observation",
    "PHASE1_WEIGHTS",
    "PredicateRef",
    "SnapshotHandle",
    "TerminalStatus",
    "Trajectory",
    "TrajectoryStep",
    "env_digest",
]
