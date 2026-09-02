"""
BestOfNActionSampler — the first working search mode (proposal §6.2).

Source: generalized from inference_optimizations.py BestOfNSampler per
    the port table: "N candidate actions/payloads, verifier scoring,
    diversity bonus over action-space distance, format_checker ->
    schema validity (hard filter)".
Integrated: 2026-07-06
Purpose: Greedy trial-and-commit over a ContractMDP: at each state,
    trial-transition every schema-valid candidate (the ensure-at
    machinery in ContractMDP makes sibling trials free — each trial
    restores to the parent before executing), score by unsat reduction
    with a diversity bonus over action-space distance, commit the best,
    repeat until SOLVED or budget. No optimality claim — this is the
    cheap mode; A*.search() is the optimal one. Trial costs are real env
    work and are reported in the result (nothing is hidden from the
    cost ledger).
"""

from __future__ import annotations

from dataclasses import dataclass
from ..core.contract_mdp import ContractMDP
from ..core.errors import EffectViolation
from ..core.jcs import canonicalize
from ..core.mdp import State
from ..core.types import ActionCandidate, Cost, GoalSpec, TerminalStatus


def action_distance(a: ActionCandidate, b: ActionCandidate) -> float:
    """Action-space distance in [0, 1]: 1.0 across tools; Jaccard
    complement over canonical arg items within a tool."""
    if a.tool_id != b.tool_id:
        return 1.0
    items_a = {f"{k}={canonicalize(v)}" for k, v in a.args.items()}
    items_b = {f"{k}={canonicalize(v)}" for k, v in b.args.items()}
    union = items_a | items_b
    if not union:
        return 0.0
    return 1.0 - len(items_a & items_b) / len(union)


@dataclass(frozen=True)
class BoNResult:
    """Outcome of a Best-of-N greedy run."""

    status: TerminalStatus
    state: State | None
    actions: tuple[ActionCandidate, ...]
    committed_cost: Cost
    trial_cost: Cost
    steps: int


class BestOfNActionSampler:
    """Greedy verifier-scored action selection over a ContractMDP.

    Args:
        mdp: The contract binding (supplies Σ-filtered candidates,
            trial transitions, and unsat counts).
        n: Candidates per step.
        diversity_weight: Bonus weight on mean distance to co-candidates
            — breaks ties toward exploration without overriding the
            verifier signal.
    """

    def __init__(self, mdp: ContractMDP, n: int = 8, diversity_weight: float = 0.05) -> None:
        self.mdp = mdp
        self.n = n
        self.diversity_weight = diversity_weight

    def run(self, goal: GoalSpec, ctx: str, max_steps: int = 16) -> BoNResult:
        """Greedy loop: trial all candidates, commit the best, repeat.

        Args:
            goal: The GoalSpec to satisfy.
            ctx: Root context.
            max_steps: Commit budget.

        Returns:
            BoNResult with SOLVED on success, BUDGET on exhaustion;
            committed vs trial costs reported separately.
        """
        state = self.mdp.initial_state(goal, ctx)
        committed: list[ActionCandidate] = []
        committed_cost = Cost()
        trial_cost = Cost()

        for step in range(max_steps):
            if self.mdp.is_terminal(state) is TerminalStatus.SOLVED:
                return BoNResult(TerminalStatus.SOLVED, state, tuple(committed),
                                 committed_cost, trial_cost, step)

            candidates = self.mdp.legal_actions(state, self.n)
            if not candidates:
                break

            actions = [a for a, _p in candidates]
            scored: list[tuple[float, int, ActionCandidate, State, Cost]] = []
            for index, action in enumerate(actions):
                try:
                    new_state, _obs, cost = self.mdp.transition(state, action)
                except EffectViolation:
                    continue  # boundary filter should prevent this; stay safe
                trial_cost = trial_cost + cost
                unsat = self.mdp.unsat_count(new_state)
                others = [b for j, b in enumerate(actions) if j != index]
                diversity = (
                    sum(action_distance(action, b) for b in others) / len(others)
                    if others else 0.0
                )
                # Plateau tiebreak (found at the P5 smoke, 2026-07-06):
                # threshold predicates hold unsat flat until crossed, so a
                # pure -unsat score re-commits idempotent no-ops forever.
                # State-space progress (digest changed) breaks the plateau
                # task-agnostically; weight 0.5 keeps unsat dominant.
                progressed = 0.5 if new_state.digest != state.digest else 0.0
                score = -float(unsat) + progressed + self.diversity_weight * diversity
                scored.append((score, index, action, new_state, cost))

            if not scored:
                break
            # Max score; ties resolved by proposer order (first index).
            scored.sort(key=lambda t: (-t[0], t[1]))
            _score, _index, best_action, best_state, best_cost = scored[0]
            # Re-commit the winner so the substrate sits on the chosen branch
            # (its trial branch may have been abandoned by later trials).
            state, _obs, commit_cost = self.mdp.transition(state, best_action)
            committed.append(best_action)
            committed_cost = committed_cost + commit_cost

        status = (TerminalStatus.SOLVED
                  if self.mdp.is_terminal(state) is TerminalStatus.SOLVED
                  else TerminalStatus.BUDGET)
        return BoNResult(status, state, tuple(committed),
                         committed_cost, trial_cost, len(committed))


__all__ = ["BestOfNActionSampler", "BoNResult", "action_distance"]
