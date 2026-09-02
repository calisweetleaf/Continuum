"""
SpeculativeExecutor — L2/L3 speculation over the contract surface (§3).

Source: acceptance-resampling machinery ported from
    inference_optimizations.py SpeculativeDecoder (Chen et al. 2023:
    r = p_T(x)/p_D(x), accept w.p. min(1, r), on reject resample from
    (p_T - p_D)+ normalized, bonus draw on full acceptance) — token
    distributions generalized to discrete action distributions keyed by
    canonical identity (D3). L3 structure per proposal §3.3: branch
    prediction transplanted onto tool calls.
Integrated: 2026-07-06
Purpose:
    L2 lossless — verify_chain_lossless: the committed action sequence
        is distributed EXACTLY as verifier-only sampling (§8.4 proof
        obligation; tested distributionally on a discrete space).
    L2 predicate — verify_chain_predicate: accept a_i iff schema-valid
        AND Tier-T legal AND precondition holds; no distributional
        claim, but committed actions never violate declared
        preconditions (§3.2 bounded-regret mode; the D7 default).
    L3 — run_overlapped: execute drafts transactionally WHILE the
        verifier runs (overlap simulated via measured/synthetic
        latencies — the correctness object is commit/rollback, and wall
        time composes as max(env, verify) either way); reversible wrong-
        path work is executed and honestly charged. At the acceptance
        boundary k, restore to the post-a_k snapshot and verify the
        digest BITWISE against the recorded one. Standing invariant 3:
        rollback_verified=False is a SoundnessError raised on the spot,
        never a warning, never a report field quietly set to False.

    Wasted speculative env work is charged honestly into the cycle Cost
    (env_calls counts executed drafts, accepted or not) — hiding it
    would falsify exactly the ledger the γ-controller optimizes (§9.6).

    After a rollback the ContractMDP's internal position marker may be
    stale by design; its ensure-at self-heals on the next transition
    (idempotent restore). Documented, tested, not accidental.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from ..core.contract_mdp import ContractMDP
from ..core.errors import SoundnessError
from ..core.mdp import MDP, State
from ..core.types import ActionCandidate, Cost, EffectClass
from .drafter import Drafter
from .gamma import AdaptiveGammaController

_TIER_T = frozenset({EffectClass.READ, EffectClass.WRITE_REVERSIBLE})

ActionDist = Mapping[ActionCandidate, float]
TargetDistFn = Callable[[State], ActionDist]
PreconditionFn = Callable[[State, ActionCandidate], bool]


@dataclass(frozen=True, slots=True)
class CommitReport:
    """Frozen Part II shape: the receipt of one speculative cycle."""

    committed: int
    drafted: int
    rollback_verified: bool
    cycle: Cost


@dataclass(frozen=True)
class LatencyModel:
    """Synthetic per-cycle latencies for controller experiments (§9.6).

    Real deployments feed measured wall times; the synthetic model
    exists so γ* convergence is testable without a GPU or a network."""

    draft_ms_per_action: float
    verify_ms: float
    env_ms_per_action: float
    jitter: float = 0.0

    def sample(self, rng: random.Random) -> tuple[float, float, float]:
        def _j(value: float) -> float:
            if self.jitter <= 0.0:
                return value
            return max(0.0, value * (1.0 + rng.uniform(-self.jitter, self.jitter)))
        return (_j(self.draft_ms_per_action), _j(self.verify_ms),
                _j(self.env_ms_per_action))


class SpeculativeExecutor:
    """L2/L3 speculation engine (see module docstring)."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random(0)

    # -- L2, lossless (§3.2 / §8.4) ---------------------------------------------

    def verify_chain_lossless(
        self,
        mdp: MDP,
        s: State,
        drafts: list[tuple[ActionCandidate, float | None]],
        target_dist: TargetDistFn,
        draft_dist: TargetDistFn,
    ) -> tuple[int, list[ActionCandidate]]:
        """Chen-et-al acceptance over an action chain.

        Args:
            mdp: Binding used only to advance prefix states for
                conditioning (lexical binding = free transitions, which
                is what the §8.4 test uses).
            s: Prefix state.
            drafts: (action, draft_prob) pairs — probs REQUIRED here.
            target_dist: p_T(. | state) over the shared discrete space.
            draft_dist: p_D(. | state), the drafter's FULL vector — the
                residual (p_T - p_D)+ is undefined without it; point
                probabilities of drafted actions cannot reconstruct it
                (that reconstruction was a real bug caught by the §8.4
                distributional test at the P4 gate, 2026-07-06).

        Returns:
            (accepted_count, committed_actions) where committed includes
            the residual resample on rejection, or the bonus draw from
            p_T on full acceptance — so the committed sequence is
            distributed exactly as verifier-only sampling.
        """
        committed: list[ActionCandidate] = []
        state = s
        accepted = 0
        for action, draft_prob in drafts:
            if draft_prob is None:
                raise ValueError("lossless mode requires draft probabilities")
            p_t = target_dist(state)
            p_target_a = float(p_t.get(action, 0.0))
            ratio = p_target_a / (draft_prob + 1e-12)
            if self.rng.random() < min(1.0, ratio):
                committed.append(action)
                accepted += 1
                state, _obs, _cost = mdp.transition(state, action)
                continue
            # Residual resample from (p_T - p_D)+ normalized; degenerate
            # (all-zero) falls back to p_T — verbatim original semantics.
            p_d = draft_dist(state)
            residual = {a: max(0.0, p_t.get(a, 0.0) - p_d.get(a, 0.0)) for a in p_t}
            total = sum(residual.values())
            dist = residual if total > 1e-12 else dict(p_t)
            committed.append(self._sample(dist))
            return accepted, committed
        # Full acceptance: bonus draw from p_T at the final prefix state.
        committed.append(self._sample(dict(target_dist(state))))
        return accepted, committed

    def _sample(self, dist: dict[ActionCandidate, float]) -> ActionCandidate:
        actions = list(dist.keys())
        weights = [dist[a] for a in actions]
        return self.rng.choices(actions, weights=weights, k=1)[0]

    # -- L2, predicate mode (D7 default) ------------------------------------------

    def verify_chain_predicate(
        self,
        mdp: ContractMDP,
        s: State,
        drafts: list[tuple[ActionCandidate, float | None]],
        precondition: PreconditionFn | None = None,
    ) -> int:
        """Acceptance boundary k: longest prefix where every action is
        schema-valid, Tier-T legal, and precondition-true at its state.

        Pre-checks run BEFORE any execution decision — a forbidden or
        malformed draft is a boundary, structurally never a side effect."""
        state = s
        accepted = 0
        for action, _prob in drafts:
            if not mdp.schema.is_valid(action):
                break
            if mdp.substrate.effect_class(action) not in _TIER_T:
                break
            if precondition is not None and not precondition(state, action):
                break
            state, _obs, _cost = mdp.transition(state, action)
            accepted += 1
        return accepted

    # -- L3, overlapped execution (§3.3) ---------------------------------------------

    def run_overlapped(
        self,
        mdp: ContractMDP,
        s: State,
        drafter: Drafter,
        gamma: int,
        precondition: PreconditionFn | None = None,
        controller: AdaptiveGammaController | None = None,
        latency: LatencyModel | None = None,
    ) -> tuple[State, CommitReport]:
        """One speculative cycle: draft, execute-while-verifying, commit
        to the acceptance boundary, digest-verify the rollback.

        Returns:
            (state_at_boundary, CommitReport). Raises SoundnessError if
            the post-rollback digest differs bitwise from the recorded
            one (invariant 3) — there is no report with
            rollback_verified=False, only the raise.
        """
        wall_start = time.perf_counter()
        drafts = drafter.draft(s, gamma)
        drafted = len(drafts)
        if drafted == 0:
            return s, CommitReport(0, 0, True, Cost())

        # Execute the reversible/schema-valid draft prefix first, as L3
        # requires: wrong-path reversible work may already be in flight
        # while the verifier is deciding the acceptance boundary.  Hard
        # invalid or Tier-T-illegal actions stop execution before side
        # effects; predicate rejection happens in the verifier pass below
        # and can leave executed-but-unaccepted work to roll back.
        states: list[State] = [s]
        executable_actions: list[ActionCandidate] = []
        env_cost = Cost()
        state = s
        for action, _prob in drafts:
            if not mdp.schema.is_valid(action):
                break
            if mdp.substrate.effect_class(action) not in _TIER_T:
                break
            state, _obs, step_cost = mdp.transition(state, action)
            env_cost = env_cost + step_cost
            executable_actions.append(action)
            states.append(state)

        accepted = 0
        for index, action in enumerate(executable_actions):
            verifier_state = states[index]
            if precondition is not None and not precondition(verifier_state, action):
                break
            accepted += 1

        boundary_state = states[accepted]

        # Rollback to the boundary and verify BITWISE (§8.5 / invariant 3).
        if boundary_state.env is None:
            raise SoundnessError("L3 requires env-carrying states")
        mdp.substrate.restore(
            mdp._handles[mdp._alias.get(boundary_state.env.token,  # noqa: SLF001
                                        boundary_state.env.token)]  # type: ignore[arg-type]
        )
        post_digest = str(mdp.substrate.digest())
        verified = post_digest == str(boundary_state.digest)
        if not verified:
            raise SoundnessError(
                f"rollback verification FAILED at boundary {accepted}: digest "
                f"{post_digest} != recorded {boundary_state.digest} — invariant 3"
            )

        # Cycle cost: overlap composes wall as draft + max(env, verify);
        # synthetic latency (when provided) supplies the model-side times,
        # measured env wall is real either way.
        measured_env_ms = (time.perf_counter() - wall_start) * 1000.0
        if latency is not None:
            draft_ms, verify_ms, env_ms = latency.sample(self.rng)
            wall = draft_ms * drafted + max(env_ms * drafted, verify_ms)
        else:
            draft_ms, verify_ms, env_ms = 0.0, 0.0, (
                measured_env_ms / drafted if drafted else 0.0)
            wall = measured_env_ms
        cycle = Cost(wall_ms=wall, env_calls=env_cost.env_calls,
                     model_tokens=0, risk=0.0)

        if controller is not None:
            controller.record(
                accepted=accepted, drafted=drafted,
                draft_ms_per_action=draft_ms, verify_ms=verify_ms,
                env_ms_per_action=env_ms,
            )

        report = CommitReport(committed=accepted, drafted=drafted,
                              rollback_verified=True, cycle=cycle)
        return boundary_state, report


__all__ = ["CommitReport", "LatencyModel", "SpeculativeExecutor"]
