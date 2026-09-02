"""
Goal predicates — the verifier layer (proposal §9.7, Part II frozen shape).

Source: written for cherry_ttt P3; the Predicate protocol shape is frozen
    in build plan Part II (verify/predicates.py block); verifier-gaming
    defense per proposal §9.7 mitigation 7.
Integrated: 2026-07-06
Purpose: Predicates are the decomposable goal atoms of GoalSpec
    (proposal §10.1) and the reward source of the whole framework —
    which is exactly why they are the reward-hacking target. Defenses,
    all structural: (1) predicates receive a ReadOnlyView, so their
    evaluation cannot write world state even by bug; (2) their criteria
    live in immutable PredicateRef.params, outside the substrate
    entirely; (3) their reference data lives in the protected namespace
    ('verifier:' keys / 'verifier_' tables) that substrates refuse
    action writes into at the effect-class / engine-authorizer level
    (standing invariant 4, tested adversarially in test_p3).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from ..core.errors import EffectViolation, ValidationError
from ..core.schema import SchemaRegistry
from ..core.types import (
    ActionCandidate,
    Cost,
    EffectClass,
    EnvDigest,
    GoalSpec,
    Observation,
    PredicateRef,
    Trajectory,
)

SATISFIED = 0.999
"""check() scores at or above this count as satisfied (float predicates
may return graded credit below it)."""


class ReadOnlyView:
    """READ-gated facade over a substrate: the only handle predicates get.

    Structural, not conventional: execute() re-gates on effect class and
    raises EffectViolation for anything but READ, before the underlying
    substrate sees the action. digest() passes through.
    """

    def __init__(self, substrate: Any) -> None:
        self._sub = substrate

    def execute(self, a: ActionCandidate) -> tuple[Observation, Cost]:
        """Execute a READ action; anything else is a contract breach."""
        cls = self._sub.effect_class(a)
        if cls is not EffectClass.READ:
            raise EffectViolation(
                f"predicate attempted {cls.name} action {a.tool_id!r}; "
                "predicates hold a ReadOnlyView (§9.7)"
            )
        return self._sub.execute(a)

    def digest(self) -> EnvDigest:
        """Touched-state digest of the underlying substrate."""
        return self._sub.digest()


@runtime_checkable
class Predicate(Protocol):
    """Frozen Part II shape: name + check against a read-only substrate."""

    name: str

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float: ...


PredicateFactory = Callable[[Mapping[str, Any]], Predicate]


class PredicateRegistry:
    """name -> factory; resolves GoalSpecs into live predicate instances.

    Factories may close over runtime objects (schema registries, oracle
    tables); PredicateRef.params stay pure JSON so canonical identity
    (D3) holds for goals exactly as it does for actions.
    """

    def __init__(self) -> None:
        self._factories: dict[str, PredicateFactory] = {}

    def register(self, name: str, factory: PredicateFactory) -> None:
        if name in self._factories:
            raise ValidationError(f"predicate {name!r} already registered")
        self._factories[name] = factory

    def build(self, ref: PredicateRef) -> Predicate:
        """Instantiate one predicate from its reference."""
        factory = self._factories.get(ref.name)
        if factory is None:
            raise ValidationError(
                f"unknown predicate {ref.name!r}; registered: {sorted(self._factories)}"
            )
        return factory(ref.params)

    def resolve(self, goal: GoalSpec) -> list[Predicate]:
        """Instantiate the full goal, order-preserving."""
        return [self.build(ref) for ref in goal.predicates]


# ---------------------------------------------------------------------------
# Built-ins (build plan P3: db_predicate, file_predicate,
# state_digest_equals, schema_validity)
# ---------------------------------------------------------------------------


class DbPredicate:
    """SELECT a scalar, compare against declared expectation.

    params: {"query": "SELECT ...", "op": "eq"|"ge"|"le", "value": v}
    (op defaults to "eq"). Misconfiguration (non-SELECT query) surfaces
    as EffectViolation from the ReadOnlyView — a hard error, never 0.0.
    """

    name = "db_predicate"

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.query = str(params["query"])
        self.op = str(params.get("op", "eq"))
        self.value = params["value"]

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float:
        obs, _cost = sub.execute(ActionCandidate("sql.exec", {"statement": self.query}))
        if obs.kind == "error" or not isinstance(obs.payload, list) or not obs.payload:
            return 0.0
        got = obs.payload[0][0]
        if self.op == "eq":
            ok = got == self.value
        elif self.op == "ge":
            ok = got is not None and got >= self.value
        elif self.op == "le":
            ok = got is not None and got <= self.value
        else:
            raise ValidationError(f"db_predicate op {self.op!r} not in eq/ge/le")
        return 1.0 if ok else 0.0


class KvPredicate:
    """Key equals expected value. params: {"k": str, "v": any}.

    The memory_kv analogue of db_predicate — the P3 synthetic-task
    workhorse (enumerable oracle domain)."""

    name = "kv_predicate"

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.key = str(params["k"])
        self.value = params["v"]

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float:
        obs, _cost = sub.execute(ActionCandidate("kv.get", {"k": self.key}))
        return 1.0 if obs.kind == "result" and obs.payload == self.value else 0.0


class FilePredicate:
    """File existence/content over any substrate exposing fs.read (READ).

    params: {"path": str, "exists": bool} and/or {"contains": str}.
    Graded: each declared criterion contributes equally.
    """

    name = "file_predicate"

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.path = str(params["path"])
        self.exists = params.get("exists")
        self.contains = params.get("contains")
        if self.exists is None and self.contains is None:
            raise ValidationError("file_predicate needs 'exists' and/or 'contains'")

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float:
        obs, _cost = sub.execute(ActionCandidate("fs.read", {"path": self.path}))
        found = obs.kind == "result"
        content = obs.payload if isinstance(obs.payload, str) else ""
        scores: list[float] = []
        if self.exists is not None:
            scores.append(1.0 if found == bool(self.exists) else 0.0)
        if self.contains is not None:
            scores.append(1.0 if found and str(self.contains) in content else 0.0)
        return sum(scores) / len(scores)


class StateDigestEquals:
    """Whole-touched-state check: digest == declared hex. params: {"digest": str}.

    The strongest predicate — used by oracle tests to pin exact target
    states — and inherently ungameable: the digest covers everything the
    action space touched."""

    name = "state_digest_equals"

    def __init__(self, params: Mapping[str, Any]) -> None:
        self.expected = str(params["digest"])

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float:
        return 1.0 if str(sub.digest()) == self.expected else 0.0


class SchemaValidity:
    """Every action in the trajectory conforms to Σ. params: {} (registry
    injected at factory-registration time; refs stay pure JSON).

    Graded: fraction of schema-valid actions; empty trajectory is 1.0.
    """

    name = "schema_validity"

    def __init__(self, registry: SchemaRegistry, params: Mapping[str, Any]) -> None:
        self._registry = registry

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float:
        if not trajectory.steps:
            return 1.0
        valid = sum(1 for step in trajectory.steps if self._registry.is_valid(step.action))
        return valid / len(trajectory.steps)


def default_predicate_registry(schema_registry: SchemaRegistry) -> PredicateRegistry:
    """Registry with all built-ins wired (schema_validity closes over Σ)."""
    registry = PredicateRegistry()
    registry.register("db_predicate", lambda p: DbPredicate(p))
    registry.register("kv_predicate", lambda p: KvPredicate(p))
    registry.register("file_predicate", lambda p: FilePredicate(p))
    registry.register("state_digest_equals", lambda p: StateDigestEquals(p))
    registry.register("schema_validity", lambda p: SchemaValidity(schema_registry, p))
    return registry


__all__ = [
    "SATISFIED",
    "DbPredicate",
    "FilePredicate",
    "KvPredicate",
    "Predicate",
    "PredicateRegistry",
    "ReadOnlyView",
    "SchemaValidity",
    "StateDigestEquals",
    "default_predicate_registry",
]
