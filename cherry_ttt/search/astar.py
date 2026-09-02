"""
EnvAStar — best-first search over the MDP protocol, port of AStarDecoder.

Source: inference_optimizations.py AStarDecoder / AStarNode / AStarConfig
    / _path_to_id (Cherry RL pipeline), proposal §6.2 port table row 2.
    Frontier mechanics (min-heap on (f, push_index)), strict-inequality
    stale-skip, g_new <= best_g child dedup, best-terminal by effective
    g, trace record schema, and _path_to_id (verbatim, since it is also
    the Tree-GRPO group_id downstream) are all carried exactly; the D6
    parity gate checks trace records field-for-field against goldens.
Integrated: 2026-07-06
Purpose: Token-agnostic A*. Substitutions per the proposal: string
    states -> State, transposition keys -> State.digest (injective over
    the lexical binding's ctx, reproducing string-keyed dedup exactly),
    duck-typed PRM/value models -> injected Callable[[State], float]
    (adaptation to .score-style objects happens at the binding edge).
    The kv-cache prefix-replay fast path is intentionally not ported —
    it is a lexical-substrate optimization; its env analogue is the
    speculative executor (P4).

Preserved quirk: path extraction stops at the first falsy label
    (original: `while current is not None and current.action:`), so an
    empty-string action label would truncate the path. Carried verbatim.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass
from typing import Any, Callable

from ..core.mdp import MDP, State
from ..core.types import ActionCandidate, GoalSpec, TerminalStatus


@dataclass
class EnvAStarConfig:
    """Field-for-field port of AStarConfig (defaults preserved)."""

    max_nodes: int = 200
    max_depth: int = 50
    n_actions: int = 8
    heuristic_weight: float = 1.0
    temperature: float = 0.8
    use_value_heuristic: bool = True


def path_to_id(path: list[str]) -> str:
    """Stable deterministic ID for an action-label path — VERBATIM port of
    _path_to_id: sha256(json.dumps(path, ensure_ascii=False))[:16].

    Verbatim because this doubles as the Tree-GRPO group_id in the
    collectors (proposal §6.3); changing it would silently re-key the
    training corpus."""
    return hashlib.sha256(
        json.dumps(path, ensure_ascii=False).encode()
    ).hexdigest()[:16]


@dataclass(eq=False)
class EnvAStarNode:
    """Frontier node; eq=False prevents auto __eq__ from walking parents
    (original rationale, preserved)."""

    state: State
    g_score: float
    h_score: float
    parent: "EnvAStarNode | None"
    action: ActionCandidate | None
    label: str
    depth: int

    @property
    def f_score(self) -> float:
        return self.g_score + self.h_score


class EnvAStar:
    """Best-first search over any MDP binding (standing invariant 1).

    Source: cherry_ttt P2 (module docstring carries full provenance).

    Args:
        mdp: Any object satisfying the MDP protocol.
        config: Search parameters (port of AStarConfig).
        prm: Optional per-step process-reward scorer over State (g-score
            increments).
        heuristic_fn: Optional value estimator over State (h-score);
            gated by config.use_value_heuristic and scaled by
            heuristic_weight, exactly as the original _heuristic.
        goal: GoalSpec forwarded to mdp.initial_state.
    """

    def __init__(
        self,
        mdp: MDP,
        config: EnvAStarConfig | None = None,
        prm: Callable[[State], float] | None = None,
        heuristic_fn: Callable[[State], float] | None = None,
        goal: GoalSpec | None = None,
    ) -> None:
        self.mdp = mdp
        self.config = config or EnvAStarConfig()
        self.prm = prm
        self.heuristic_fn = heuristic_fn
        self.goal = goal or GoalSpec(predicates=())

    def decode(
        self,
        ctx: str,
        reward_fn: Callable[[State], float] | None = None,
        trace: bool = False,
    ) -> dict[str, Any]:
        """Run A* from ctx; result and trace schemas identical to the
        original AStarDecoder.decode (see its docstring — the contract
        is carried, not paraphrased).

        Args:
            ctx: Root context.
            reward_fn: Optional terminal bonus over State, added to g.
            trace: Emit per-expansion records (zero overhead when False).

        Returns:
            {'text', 'g_score', 'depth', 'nodes_expanded', 'path'} plus
            'trace' when requested.
        """
        cfg = self.config
        mdp = self.mdp
        trace_records: list[dict[str, Any]] = []

        start_state = mdp.initial_state(self.goal, ctx)
        h0 = self._heuristic(start_state)
        root = EnvAStarNode(
            state=start_state, g_score=0.0, h_score=h0,
            parent=None, action=None, label="", depth=0,
        )

        frontier: list[tuple[float, int, EnvAStarNode]] = []
        push_index = 0
        heapq.heappush(frontier, (root.f_score, push_index, root))
        best_g_by_state: dict[str, float] = {str(start_state.digest): 0.0}
        nodes_expanded = 0
        best_terminal: tuple[EnvAStarNode, float] | None = None
        best_nonterminal: EnvAStarNode | None = root

        while frontier and nodes_expanded < cfg.max_nodes:
            _priority, _order, node = heapq.heappop(frontier)

            if node.g_score < best_g_by_state.get(str(node.state.digest), float("-inf")):
                continue
            nodes_expanded += 1

            if trace:
                node_path = self._extract_path(node)
                trace_records.append({
                    "id": path_to_id(node_path),
                    "parent_id": path_to_id(node_path[:-1]) if node.parent is not None else None,
                    "state": node.state.ctx,
                    "action": node.label,
                    "depth": node.depth,
                    "g_score": node.g_score,
                    "h_score": node.h_score,
                    "path": node_path,
                    "is_terminal": False,
                    "terminal_reward": 0.0,
                })

            if mdp.is_terminal(node.state) is not TerminalStatus.OPEN or node.depth >= cfg.max_depth:
                terminal_bonus = float(reward_fn(node.state)) if reward_fn is not None else 0.0
                effective_g = node.g_score + terminal_bonus
                if best_terminal is None or effective_g > best_terminal[1]:
                    best_terminal = (node, effective_g)
                if trace and trace_records:
                    trace_records[-1]["is_terminal"] = True
                    trace_records[-1]["terminal_reward"] = terminal_bonus
                continue

            if best_nonterminal is None or node.g_score > best_nonterminal.g_score:
                best_nonterminal = node

            actions = mdp.legal_actions(node.state, cfg.n_actions)
            for action, _prior in actions:
                new_state, _obs, _cost = mdp.transition(node.state, action)
                step_reward = self._prm_score(new_state)
                g_new = node.g_score + step_reward
                digest_key = str(new_state.digest)
                if g_new <= best_g_by_state.get(digest_key, float("-inf")):
                    continue
                best_g_by_state[digest_key] = g_new
                h_new = self._heuristic(new_state)
                child = EnvAStarNode(
                    state=new_state, g_score=g_new, h_score=h_new,
                    parent=node, action=action,
                    label=mdp.action_label(action), depth=node.depth + 1,
                )
                push_index += 1
                heapq.heappush(frontier, (child.f_score, push_index, child))

        if best_terminal is not None:
            result_node, result_g = best_terminal
        else:
            result_node = best_nonterminal if best_nonterminal is not None else root
            result_g = result_node.g_score

        result: dict[str, Any] = {
            "text": result_node.state.ctx,
            "g_score": result_g,
            "depth": result_node.depth,
            "nodes_expanded": nodes_expanded,
            "path": self._extract_path(result_node),
        }
        if trace:
            result["trace"] = trace_records
        return result

    def _heuristic(self, state: State) -> float:
        """h-score: gated, weighted value estimate (original semantics)."""
        cfg = self.config
        if not cfg.use_value_heuristic or self.heuristic_fn is None:
            return 0.0
        return float(self.heuristic_fn(state)) * cfg.heuristic_weight

    def _prm_score(self, state: State) -> float:
        """Per-step g increment from the PRM; 0.0 when absent."""
        if self.prm is None:
            return 0.0
        return float(self.prm(state))

    def _extract_path(self, node: EnvAStarNode) -> list[str]:
        """Iterative root->node label path; stops at first falsy label
        (preserved quirk — see module docstring)."""
        path: list[str] = []
        current: EnvAStarNode | None = node
        while current is not None and current.label:
            path.append(current.label)
            current = current.parent
        path.reverse()
        return path


# ---------------------------------------------------------------------------
# Cost-regime interface (P3, proposal §2.4) — APPEND-ONLY. decode() above is
# parity-locked (D6); search() below is the goal-directed reinterpretation:
# g = collapsed vector cost (minimized), h = declared heuristic, min-heap =
# textbook A*. Heuristics DECLARE their regime (§9.5: "admissibility theater"
# — learned h is never admissible and claiming optimality with it is false);
# optimality is asserted only under declared-admissible h, and the weighted
# bound C <= w * C* is what gets reported otherwise.
# ---------------------------------------------------------------------------

from ..core.types import Cost, CostWeights, PHASE1_WEIGHTS  # noqa: E402


@dataclass(frozen=True)
class DeclaredHeuristic:
    """A heuristic that states its regime instead of implying it.

    admissible=True asserts fn(s) <= true remaining collapsed cost for
    all s under the weights in force — the caller's proof obligation
    (e.g. |unsat|/k under pure env_calls weighting, proposal §2.4).
    """

    fn: Callable[[State], float]
    admissible: bool
    weight: float = 1.0

    def __call__(self, s: State) -> float:
        return float(self.fn(s)) * self.weight


def admissible_unsat_heuristic(mdp: Any, goal: GoalSpec) -> DeclaredHeuristic:
    """h(s) = |unsat(G, s)| / k — admissible when every action's collapsed
    cost >= 1 and each action satisfies at most k predicates (§2.4).

    Args:
        mdp: A binding exposing unsat_count(s) (ContractMDP does).
        goal: Supplies k = max_per_action.
    """
    k = goal.max_per_action
    return DeclaredHeuristic(fn=lambda s: mdp.unsat_count(s) / k, admissible=True)


@dataclass(frozen=True)
class SearchResult:
    """Outcome of a cost-regime search (Part II SearchResult shape)."""

    status: TerminalStatus
    state: State | None
    actions: tuple[ActionCandidate, ...]
    path_labels: tuple[str, ...]
    total_cost: Cost
    collapsed_cost: float
    nodes_expanded: int
    reopened: int
    optimal_claim: bool  # True only under declared-admissible h + SOLVED


@dataclass(eq=False)
class _CostNode:
    state: State
    g: float
    h: float
    parent: "_CostNode | None"
    action: ActionCandidate | None
    label: str
    cost_edge: Cost
    depth: int

    @property
    def f(self) -> float:
        return self.g + self.h


def _search_impl(
    self: "EnvAStar",
    ctx: str,
    weights: CostWeights = PHASE1_WEIGHTS,
    heuristic: DeclaredHeuristic | None = None,
    max_nodes: int | None = None,
    n_actions: int | None = None,
) -> SearchResult:
    """Goal-directed min-cost A* over the MDP (see class docstring note).

    Pops min f; keeps min-g per digest; counts (and skips) reopenings —
    under a consistent admissible h the reopened count must be zero
    (§8.2), and the P3 gate asserts exactly that.
    """
    mdp = self.mdp
    budget = max_nodes if max_nodes is not None else self.config.max_nodes
    branching = n_actions if n_actions is not None else self.config.n_actions
    h_fn = heuristic if heuristic is not None else DeclaredHeuristic(
        fn=lambda _s: 0.0, admissible=True
    )

    start = mdp.initial_state(self.goal, ctx)
    root = _CostNode(state=start, g=0.0, h=h_fn(start), parent=None,
                     action=None, label="", cost_edge=Cost(), depth=0)
    frontier: list[tuple[float, int, _CostNode]] = []
    push_index = 0
    heapq.heappush(frontier, (root.f, push_index, root))
    best_g: dict[str, float] = {str(start.digest): 0.0}
    closed: set[str] = set()
    nodes_expanded = 0
    reopened = 0
    best_partial = root

    def _finish(node: _CostNode, status: TerminalStatus) -> SearchResult:
        actions: list[ActionCandidate] = []
        labels: list[str] = []
        total = Cost()
        cursor: _CostNode | None = node
        while cursor is not None and cursor.action is not None:
            actions.append(cursor.action)
            labels.append(cursor.label)
            total = total + cursor.cost_edge
            cursor = cursor.parent
        actions.reverse()
        labels.reverse()
        return SearchResult(
            status=status, state=node.state, actions=tuple(actions),
            path_labels=tuple(labels), total_cost=total,
            collapsed_cost=node.g, nodes_expanded=nodes_expanded,
            reopened=reopened,
            optimal_claim=(status is TerminalStatus.SOLVED and h_fn.admissible
                           and h_fn.weight <= 1.0),
        )

    while frontier and nodes_expanded < budget:
        _f, _idx, node = heapq.heappop(frontier)
        key = str(node.state.digest)
        if node.g > best_g.get(key, float("inf")):
            continue  # stale entry
        if key in closed:
            reopened += 1  # must stay 0 under consistent admissible h (§8.2)
            continue
        closed.add(key)
        nodes_expanded += 1

        status = mdp.is_terminal(node.state)
        if status is TerminalStatus.SOLVED:
            return _finish(node, TerminalStatus.SOLVED)
        if status is not TerminalStatus.OPEN:
            continue
        if node.h < best_partial.h or (node.h == best_partial.h and node.g < best_partial.g):
            best_partial = node

        for action, _prior in mdp.legal_actions(node.state, branching):
            new_state, _obs, edge_cost = mdp.transition(node.state, action)
            g_new = node.g + weights.collapse(edge_cost)
            child_key = str(new_state.digest)
            if g_new >= best_g.get(child_key, float("inf")):
                continue
            best_g[child_key] = g_new
            child = _CostNode(
                state=new_state, g=g_new, h=h_fn(new_state), parent=node,
                action=action, label=mdp.action_label(action),
                cost_edge=edge_cost, depth=node.depth + 1,
            )
            push_index += 1
            heapq.heappush(frontier, (child.f, push_index, child))

    return _finish(best_partial, TerminalStatus.BUDGET)


EnvAStar.search = _search_impl
