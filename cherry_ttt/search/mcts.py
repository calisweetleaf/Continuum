"""
EnvMCTS — MCTS over the MDP protocol, ported verbatim from MCTSGenerator.

Source: inference_optimizations.py MCTSGenerator / MCTSNode / MCTSConfig
    (Cherry RL pipeline), proposal §6.2 port table row 1. Control flow,
    arithmetic expressions (PUCT, depth-discounted backprop, blend
    rules), progressive-widening formula, child ordering, and tie-break
    semantics are carried expression-for-expression: the D6 parity gate
    checks this port bitwise against goldens captured from the original.
Integrated: 2026-07-06
Purpose: Token-agnostic MCTS. The only substitutions are the ones the
    proposal names: string states -> State, string actions ->
    ActionCandidate, string concat -> mdp.transition, tokenizer/torch
    action sampling -> mdp.legal_actions. The rollout is the original's
    deterministic greedy path (legal_actions(n=1) loop) — in the
    original this was the legacy rollout branch; here it is the primary env
    rollout, since env-MDP rollouts must be deterministic drafts.
    Stochastic rollout policies arrive later via injection, post-parity.

v0.4 amendment A2 repairs the preserved root progressive-widening quirk.
    Selection now stops at any node eligible to widen, including the root.
    This intentionally supersedes lexical parity because environment search
    with one immutable root action cannot support native tool reasoning.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from ..core.mdp import MDP, State
from ..core.types import ActionCandidate, Cost, GoalSpec, Observation, TerminalStatus


@dataclass
class EnvMCTSConfig:
    """Field-for-field port of MCTSConfig (defaults preserved)."""

    n_simulations: int = 100
    c_puct: float = 1.25
    puct_c2: float = 19652.0
    temperature: float = 1.0
    max_depth: int = 100
    max_rollout_depth: int = 50
    n_actions: int = 10
    use_value_model: bool = True
    progressive_widening_alpha: float = 0.5
    depth_discount: float = 0.95
    reward_value_blend: float = 0.5
    serialize_tree: bool = False


class EnvMCTSNode:
    """Tree node — bookkeeping identical to MCTSNode; child order is
    append order and is parity-load-bearing (max() takes first maximum)."""

    def __init__(
        self,
        state: State,
        parent: "EnvMCTSNode | None" = None,
        action: ActionCandidate | None = None,
        label: str = "",
        depth: int = 0,
    ) -> None:
        self.state = state
        self.parent = parent
        self.action = action
        self.label = label
        self.depth = depth
        self.children: list[EnvMCTSNode] = []
        self.visits = 0
        self.value_sum = 0.0
        self.prior = 1.0
        self.reward: float | None = None
        self.observation: Observation | None = None
        self.edge_cost = Cost()
        self.is_expanded = False
        self.is_terminal = False

    def value(self) -> float:
        """Mean value; 0.0 when unvisited (original semantics)."""
        if self.visits == 0:
            return 0.0
        return self.value_sum / self.visits

    def ucb_score(self, c_puct: float, c2: float = 19652.0) -> float:
        """AlphaZero/MuZero PUCT, expression carried verbatim for parity."""
        if self.visits == 0:
            return float("inf")
        q_value = self.value()
        if self.parent:
            n_parent = self.parent.visits
            log_factor = math.log((n_parent + c2 + 1) / c2)
            explore_rate = math.sqrt(n_parent * log_factor)
            u_value = c_puct * self.prior * explore_rate / (1 + self.visits)
        else:
            u_value = 0.0
        return q_value + u_value

    def best_child(self, c_puct: float, c2: float = 19652.0) -> "EnvMCTSNode":
        """Max PUCT; first maximum wins ties, so child order matters."""
        return max(self.children, key=lambda c: c.ucb_score(c_puct, c2))

    def add_child(
        self,
        action: ActionCandidate,
        label: str,
        state: State,
        observation: Observation | None = None,
        edge_cost: Cost | None = None,
    ) -> "EnvMCTSNode":
        """Append a child with the complete edge receipt.

        Observation and vector cost are retained so the search tree can become
        a faithful RFT/Tree-GRPO corpus rather than a label-only tree.
        """
        child = EnvMCTSNode(state, parent=self, action=action, label=label,
                            depth=self.depth + 1)
        child.observation = observation
        child.edge_cost = edge_cost or Cost()
        self.children.append(child)
        return child

    def to_dict(self) -> dict[str, Any]:
        """Serialize for tree dumps — field set and order match the original."""
        return {
            "state_preview": self.state.ctx[-80:],
            "action": self.label,
            "depth": self.depth,
            "visits": self.visits,
            "value": self.value(),
            "prior": self.prior,
            "reward": self.reward,
            "is_terminal": self.is_terminal,
            "children": [c.to_dict() for c in self.children],
        }


class EnvMCTS:
    """MCTS search over any MDP binding (standing invariant 1: this class
    must never learn which binding it runs).

    Source: cherry_ttt P2 (module docstring carries full provenance).

    Args:
        mdp: Any object satisfying the MDP protocol.
        config: Search parameters (port of MCTSConfig).
        value_fn: Optional value estimator over State (replaces the
            original's duck-typed value model; adaptation to .score-style
            objects happens at the binding edge).
        goal: GoalSpec forwarded to mdp.initial_state; empty by default
            for the degenerate binding.
    """

    def __init__(
        self,
        mdp: MDP,
        config: EnvMCTSConfig | None = None,
        value_fn: Callable[[State], float] | None = None,
        goal: GoalSpec | None = None,
    ) -> None:
        self.mdp = mdp
        self.config = config or EnvMCTSConfig()
        self.value_fn = value_fn
        self.goal = goal or GoalSpec(predicates=())

    def generate(
        self,
        ctx: str,
        reward_fn: Callable[[State], float] | None = None,
    ) -> dict[str, Any]:
        """Run n_simulations of select/expand/evaluate/backprop from ctx.

        Args:
            ctx: Root context (the prompt, in the degenerate binding).
            reward_fn: Optional terminal reward over State.

        Returns:
            {'text', 'root', 'visit_counts', 'best_child_values'} plus
            'tree_json' when config.serialize_tree — shapes identical to
            the original MCTSGenerator.generate result.
        """
        root = EnvMCTSNode(self.mdp.initial_state(self.goal, ctx))

        for _sim in range(self.config.n_simulations):
            node = self._select(root)

            # A widening attempt can legitimately produce no new action when a
            # deterministic/contextual proposer has exhausted its candidates.
            # In that case continue down the selected branch instead of
            # repeatedly stalling at the same widenable parent.
            while not node.is_terminal and (node.visits > 0 or node is root):
                added = self._expand(node)
                if added:
                    node = max(added, key=lambda child: child.prior)
                    break
                if not node.children:
                    break
                node = node.best_child(self.config.c_puct, self.config.puct_c2)
                if node.visits == 0:
                    break

            value = self._evaluate(node, self.config.max_rollout_depth, reward_fn)
            self._backpropagate(node, value)

        best_sequence = self._get_best_sequence(root)
        result: dict[str, Any] = {
            "text": best_sequence,
            "root": root,
            "visit_counts": self._get_visit_distribution(root),
            "best_child_values": [c.value() for c in root.children],
        }
        if self.config.serialize_tree:
            result["tree_json"] = json.dumps(root.to_dict(), default=str)
        return result

    @staticmethod
    def _pick_expansion_child(node: EnvMCTSNode) -> EnvMCTSNode:
        """Strongest (visits, prior) among children — original tie rule."""
        return max(node.children, key=lambda child: (child.visits, child.prior))

    def _select(self, root: EnvMCTSNode) -> EnvMCTSNode:
        """Descend by PUCT, stopping at any node eligible to widen.

        v0.3 preserved a lexical parity quirk that permanently capped the root
        at one child.  Native tool reasoning requires the root and every
        interior node to widen as visits accumulate, so selection now stops at
        a node whose progressive-widening allowance exceeds its child count.
        """
        node = root
        while not node.is_terminal:
            if not node.children or self._can_widen(node):
                return node
            node = node.best_child(self.config.c_puct, self.config.puct_c2)
        return node

    def _max_children(self, node: EnvMCTSNode) -> int:
        alpha = self.config.progressive_widening_alpha
        return max(1, math.ceil((node.visits + 1) ** alpha))

    def _can_widen(self, node: EnvMCTSNode) -> bool:
        return len(node.children) < self._max_children(node)

    def _expand(self, node: EnvMCTSNode) -> list[EnvMCTSNode]:
        """Progressive widening: max_children = ceil((visits+1)**alpha);
        dedup by action identity (canonical, which for the lexical binding
        coincides with the original's string dedup)."""
        max_children = self._max_children(node)

        if len(node.children) >= max_children and node.is_expanded:
            return []
        n_to_generate = max_children - len(node.children)
        if n_to_generate <= 0:
            node.is_expanded = True
            return []

        actions = self.mdp.legal_actions(node.state, self.config.n_actions)
        existing = {c.action.canonical() for c in node.children if c.action is not None}
        new_actions = [(a, p) for a, p in actions if a.canonical() not in existing]

        added: list[EnvMCTSNode] = []
        for action, prob in new_actions[:n_to_generate]:
            new_state, obs, cost = self.mdp.transition(node.state, action)
            child = node.add_child(
                action,
                self.mdp.action_label(action),
                new_state,
                observation=obs,
                edge_cost=cost,
            )
            child.prior = prob
            if self.mdp.is_terminal(new_state) is not TerminalStatus.OPEN:
                child.is_terminal = True
            added.append(child)

        node.is_expanded = True
        return added

    def _evaluate(
        self,
        node: EnvMCTSNode,
        rollout_budget: int,
        reward_fn: Callable[[State], float] | None,
    ) -> float:
        """Blend rules ported branch-for-branch from the original."""
        cfg = self.config

        if node.is_terminal and reward_fn is not None:
            r = float(reward_fn(node.state))
            node.reward = r
            if self.value_fn and cfg.use_value_model:
                v = self.value_fn(node.state)
                return cfg.reward_value_blend * r + (1 - cfg.reward_value_blend) * v
            return r

        if self.value_fn and cfg.use_value_model:
            v = self.value_fn(node.state)
            if reward_fn is not None:
                rollout_r = self._rollout(node.state, rollout_budget, reward_fn)
                return cfg.reward_value_blend * rollout_r + (1 - cfg.reward_value_blend) * v
            return v

        return self._rollout(node.state, rollout_budget, reward_fn)

    def _rollout(
        self,
        state: State,
        rollout_budget: int,
        reward_fn: Callable[[State], float] | None,
    ) -> float:
        """Deterministic greedy rollout: the original's legacy rollout branch,
        promoted to the primary env rollout (see module docstring)."""
        max_steps = max(0, int(rollout_budget))
        if max_steps == 0:
            if reward_fn:
                return reward_fn(state)
            return 0.0

        current = state
        for _ in range(max_steps):
            if self.mdp.is_terminal(current) is not TerminalStatus.OPEN:
                break
            actions = self.mdp.legal_actions(current, 1)
            if actions:
                current, _obs, _cost = self.mdp.transition(current, actions[0][0])

        if reward_fn:
            return reward_fn(current)
        return 0.0

    def _backpropagate(self, node: EnvMCTSNode | None, value: float) -> None:
        """Depth-discounted backprop, expression carried verbatim."""
        discount = self.config.depth_discount
        current_depth = node.depth if node is not None else 0
        while node is not None:
            node.visits += 1
            discounted = value * (discount ** (current_depth - node.depth))
            node.value_sum += discounted
            node = node.parent

    def _get_best_sequence(self, root: EnvMCTSNode) -> str:
        """Descend by max visit count; return the leaf context."""
        node = root
        while node.children:
            node = max(node.children, key=lambda c: c.visits)
        return node.state.ctx

    def _get_visit_distribution(self, root: EnvMCTSNode) -> list[int]:
        """Visit counts of root children, in child order."""
        return [c.visits for c in root.children]
