"""
ContractMDP — the MDP protocol bound to a real TransactionalSubstrate.

Source: written for cherry_ttt P3 per proposal §1.1/§6.2: transition()
    promoted from free string concatenation to costed, side-effectful,
    snapshot-reversible execution. The lexical binding (core/mdp.py) is
    the degenerate case of this class; nothing in search/ can tell them
    apart (standing invariant 1).
Integrated: 2026-07-06
Purpose: The fusion point of the two verified halves — P1's sound
    substrates and P2's parity-locked search. Every transition is
    ensure-at(s) -> execute -> snapshot -> digest; every candidate is
    Σ-conformed (D3 float rounding) and effect-gated before search ever
    sees it; predicates evaluate through a ReadOnlyView against goals
    resolved from immutable PredicateRefs (§9.7).

Re-rooting (the real engineering): search pops frontier nodes in
    score order, jumping across branches. Clone-ledger substrates
    (memory_kv) restore from any handle; savepoint substrates (sqlite)
    lose handles on abandoned branches. ensure-at therefore: (1) tries
    restore(s.env) via the alias table; (2) on SnapshotError walks the
    parent map to the nearest restorable ancestor, replays the recorded
    actions downward, verifies the replayed digest EQUALS s.digest
    bitwise (a free nondeterminism tripwire — divergence raises
    SoundnessError, never silently corrupts the search), snapshots
    fresh, and aliases the dead token to the live one. Replay execution
    is real env work and is charged into the next transition's Cost
    (honest accounting; the alternative — hiding it — would corrupt the
    §9.6 latency ledger).

Verifier reads (is_terminal / reward / unsat_count) are NOT charged to
    trajectory cost: the cost model prices the plan, not the checking.
    Documented here because it is a modeling decision, not an accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import SnapshotError, SoundnessError, ValidationError
from .mdp import State
from .schema import SchemaRegistry
from .types import (
    ActionCandidate,
    Cost,
    EffectClass,
    GoalSpec,
    Observation,
    TerminalStatus,
    Trajectory,
    TrajectoryStep,
)
from ..interleave.context import ContextualActionProposer, ReasoningContext
from ..substrate.base import TransactionalSubstrateBase
from ..verify.predicates import SATISFIED, PredicateRegistry, ReadOnlyView

_TIER_T = frozenset({EffectClass.READ, EffectClass.WRITE_REVERSIBLE})


@runtime_checkable
class ActionProposer(Protocol):
    """Candidate generator over contract states. Implementations: the
    enumerating proposer (P3 tests), BestOfNActionSampler's inner
    generator, the template drafter (P4, D7), the policy head (later)."""

    def propose(self, s: State, n: int) -> list[tuple[ActionCandidate, float]]: ...


@dataclass
class ContractMDPConfig:
    """Binding parameters."""

    max_depth: int = 32
    ctx_delimiter: str = "\n"


class ContractMDP:
    """MDP over a TransactionalSubstrate (see module docstring).

    Args:
        substrate: A Tier-T substrate (P1-sound).
        proposer: Action candidate source.
        schema: Σ — every proposed candidate is conformed (D3) and
            hard-filtered here, at the MDP boundary, so search never
            sees a malformed or Tier-T-illegal action.
        predicates: Registry resolving GoalSpec refs to live checkers.
        config: Binding parameters.
    """

    def __init__(
        self,
        substrate: TransactionalSubstrateBase,
        proposer: ActionProposer,
        schema: SchemaRegistry,
        predicates: PredicateRegistry,
        config: ContractMDPConfig | None = None,
    ) -> None:
        self.substrate = substrate
        self.proposer = proposer
        self.schema = schema
        self.predicates = predicates
        self.config = config or ContractMDPConfig()
        self._view = ReadOnlyView(substrate)
        self._root_ctx = ""
        self._resolved: list = []
        self._goal: GoalSpec | None = None
        # token -> (parent_token | None, action | None): the replay spine
        self._parents: dict[str, tuple[str | None, ActionCandidate | None]] = {}
        # dead token -> live token (re-snapshotted after replay)
        self._alias: dict[str, str] = {}
        self._handles: dict[str, object] = {}
        self._digests: dict[str, str] = {}
        self._traj: dict[str, Trajectory] = {}
        self._current: str | None = None
        self._pending_overhead = Cost()
        self.replay_count = 0  # observability for tests/metrics

    # -- MDP protocol ---------------------------------------------------------

    def initial_state(self, goal: GoalSpec, ctx: str) -> State:
        """Resolve the goal, snapshot the substrate as root, seed the spine."""
        self._goal = goal
        self._root_ctx = ctx
        self._resolved = self.predicates.resolve(goal)
        handle = self.substrate.snapshot()
        digest = self.substrate.digest()
        token = handle.token
        self._parents[token] = (None, None)
        self._handles[token] = handle
        self._digests[token] = str(digest)
        self._traj[token] = Trajectory(initial_digest=digest)
        self._current = token
        return State(ctx=ctx, env=handle, digest=digest, depth=0)

    def legal_actions(self, s: State, n: int) -> list[tuple[ActionCandidate, float]]:
        """Propose, Σ-conform (D3), and effect-gate candidates.

        Search never sees a candidate that would violate the schema or
        reach a forbidden effect class — the hard filter lives here, at
        the boundary, not as a convention inside search code.
        """
        out: list[tuple[ActionCandidate, float]] = []
        if isinstance(self.proposer, ContextualActionProposer):
            proposed = self.proposer.propose_with_context(self.reasoning_context(s), n)
        else:
            proposed = self.proposer.propose(s, n)
        for candidate, prior in proposed:
            if not self.schema.is_valid(candidate):
                continue
            conformed = self.schema.conform(candidate)
            if self.substrate.effect_class(conformed) not in _TIER_T:
                continue
            out.append((conformed, float(prior)))
        return out

    def transition(self, s: State, a: ActionCandidate) -> tuple[State, Observation, Cost]:
        """ensure-at(s), execute, snapshot, digest — the promoted E(s, a)."""
        if s.env is None:
            raise ValidationError("ContractMDP states must carry an env handle")
        overhead = self._ensure_at(s)
        obs, exec_cost = self.substrate.execute(a)
        handle = self.substrate.snapshot()
        digest = self.substrate.digest()

        parent_token = self._alias.get(s.env.token, s.env.token)
        token = handle.token
        self._parents[token] = (parent_token, a)
        self._handles[token] = handle
        self._digests[token] = str(digest)
        step = TrajectoryStep(action=a, observation=obs, cost=exec_cost, post_digest=digest)
        self._traj[token] = self._traj[parent_token].extended(step)
        self._current = token

        ctx = s.ctx + self.config.ctx_delimiter + f"{self.action_label(a)} => {obs.kind}"
        new_state = State(ctx=ctx, env=handle, digest=digest, depth=s.depth + 1)
        total = exec_cost + self.substrate.snapshot_cost_estimate() + overhead
        return new_state, obs, total

    def is_terminal(self, s: State) -> TerminalStatus:
        """SOLVED when every predicate satisfies; BUDGET past max_depth."""
        if s.depth >= self.config.max_depth:
            return TerminalStatus.BUDGET
        if all(score >= SATISFIED for score in self._scores(s)):
            return TerminalStatus.SOLVED
        return TerminalStatus.OPEN

    def reward(self, s: State, trajectory: Trajectory) -> float:
        """Mean predicate satisfaction in [0, 1] (decomposable by §10.1)."""
        scores = self._scores(s, trajectory)
        return sum(scores) / len(scores) if scores else 0.0

    def action_label(self, a: ActionCandidate) -> str:
        """Readable deterministic label (amendment A1 default refined)."""
        return f"{a.tool_id}:{a.canonical()[:8]}"

    # -- heuristic support ------------------------------------------------------

    def unsat_count(self, s: State) -> int:
        """Number of unsatisfied predicates at s — the numerator of the
        admissible bound h(s) = |unsat| / k (proposal §2.4)."""
        return sum(1 for score in self._scores(s) if score < SATISFIED)

    def trajectory_of(self, s: State) -> Trajectory:
        """The committed trajectory reaching s (collector feed, §6.3)."""
        if s.env is None:
            raise ValidationError("degenerate states carry no trajectory here")
        return self._traj[self._alias.get(s.env.token, s.env.token)]

    def reasoning_context(self, s: State) -> ReasoningContext:
        """Return the complete branch-local context for a reasoning adapter.

        This is the native observation bridge: the proposer receives raw typed
        observations and the full trajectory rather than the lossy ``ctx``
        summary (which intentionally carries only action labels and observation
        kinds for human-readable traces).
        """
        if self._goal is None:
            raise ValidationError("initial_state() must be called before reasoning_context()")
        return ReasoningContext(
            root_ctx=self._root_ctx,
            state=s,
            goal=self._goal,
            trajectory=self.trajectory_of(s),
        )

    # -- internals ---------------------------------------------------------------

    def _scores(self, s: State, trajectory: Trajectory | None = None) -> list[float]:
        self._ensure_at(s)
        traj = trajectory if trajectory is not None else self.trajectory_of(s)
        return [p.check(self._view, traj) for p in self._resolved]

    def _ensure_at(self, s: State) -> Cost:
        """Place the substrate exactly at s; returns accumulated overhead.

        Fast path: already there, or restore(live handle) succeeds.
        Slow path: replay from the nearest restorable ancestor with a
        bitwise digest tripwire (see module docstring).
        """
        assert s.env is not None
        token = self._alias.get(s.env.token, s.env.token)
        overhead, self._pending_overhead = self._pending_overhead, Cost()

        if self._current == token:
            return overhead
        handle = self._handles[token]
        direct_restore_failed = False
        try:
            self.substrate.restore(handle)  # type: ignore[arg-type]
            self._current = token
            return overhead
        except SnapshotError:
            direct_restore_failed = True
        if not direct_restore_failed:
            raise SoundnessError("unreachable restore control-flow breach")

        # Walk up the spine to the nearest restorable ancestor.
        chain: list[tuple[str, ActionCandidate]] = []
        cursor: str | None = token
        anchor: str | None = None
        while cursor is not None:
            parent, action = self._parents[cursor]
            if action is not None and parent is not None:
                chain.append((cursor, action))
                cursor = parent
                try:
                    self.substrate.restore(self._handles[cursor])  # type: ignore[arg-type]
                    anchor = cursor
                    break
                except SnapshotError:
                    continue
            else:
                cursor = parent
        if anchor is None:
            raise SnapshotError(
                f"no restorable ancestor found for state token {token!r}; "
                "the replay spine is broken"
            )

        # Replay downward, re-snapshotting and aliasing each dead node.
        replay_cost = Cost()
        for dead_token, action in reversed(chain):
            _obs, cost = self.substrate.execute(action)
            replay_cost = replay_cost + cost
            fresh = self.substrate.snapshot()
            replay_cost = replay_cost + self.substrate.snapshot_cost_estimate()
            fresh_digest = str(self.substrate.digest())
            if fresh_digest != self._digests[dead_token]:
                raise SoundnessError(
                    f"replay diverged at {dead_token!r}: digest {fresh_digest} != "
                    f"recorded {self._digests[dead_token]} — the substrate is "
                    "nondeterministic under replay (proposal §8.5 breach)"
                )
            parent_of_dead = self._parents[dead_token][0]
            live_parent = self._alias.get(parent_of_dead, parent_of_dead) if parent_of_dead else None
            self._parents[fresh.token] = (live_parent, action)
            self._handles[fresh.token] = fresh
            self._digests[fresh.token] = fresh_digest
            self._traj[fresh.token] = self._traj[dead_token]
            self._alias[dead_token] = fresh.token
            self.replay_count += 1

        self._current = self._alias.get(token, token)
        self._pending_overhead = Cost()
        return overhead + replay_cost


__all__ = ["ActionProposer", "ContractMDP", "ContractMDPConfig"]
