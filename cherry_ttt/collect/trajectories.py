"""
Trajectory collectors — the Cherry loop closure (proposal §6.3).

Source: generalized from inference_optimizations.py RolloutSample /
    TreeRolloutCollector / AStarRolloutCollector.collect_grouped():
    group_id = sha256(json.dumps(parent_path))[:16] (path_to_id, carried
    verbatim from search/astar.py — the SAME function keys A* trace
    records, so trace ids and training group ids agree by construction);
    sibling_rewards assembled after the full group exists; never
    Python hash(); never truncated action strings.
Integrated: 2026-07-06
Purpose: Emit environment trajectories with sibling-group structure —
    matched action sequences from the same parent env state, with
    per-trajectory verified rewards and per-action process scores. The
    test-time search tree becomes the post-training corpus; the
    intra-tree advantage estimation the grouped shape supports is the
    credit assignment for training on DECISION QUALITY over tool use.
    TTT is a rollout engine for tool-use post-training, not just an
    inference framework — this module is where that claim is cashed.

Consuming this data (the training loop) is a separate project boundary
    by explicit §10.2 deferral; emission is in scope from day one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.types import EnvDigest, Observation, Trajectory, TrajectoryStep
from ..interleave.context import branch_id_for_trajectory
from ..interleave.events import BranchEventLedger
from ..search.astar import path_to_id
from ..search.mcts import EnvMCTSNode


@dataclass
class TrajectorySample:
    """One training-ready sample (generalized RolloutSample shape).

    prompt/completion become ctx/action-path; full_state becomes the
    terminal ctx plus the env digest — the pair that identifies an
    environment outcome rather than a text outcome."""

    ctx: str
    path: list[str]
    actions: list[dict[str, Any]]          # [{tool_id, args, canonical}]
    reward: float
    process_scores: list[float]            # per-action (PRM/g-increments)
    depth: int
    visit_count: int
    terminal_ctx: str
    env_digest: str
    status: str
    cost: dict[str, float]                 # vector, never collapsed here (D4)
    group_id: str | None = None
    parent_depth: int = 0
    sibling_rewards: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def _action_record(node: EnvMCTSNode) -> dict[str, Any]:
    assert node.action is not None
    return {
        "tool_id": node.action.tool_id,
        "args": dict(node.action.args),
        "canonical": node.action.canonical(),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_hex__": bytes(value).hex()}
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _cost_record(node: EnvMCTSNode) -> dict[str, float]:
    return {
        "wall_ms": float(node.edge_cost.wall_ms),
        "model_tokens": float(node.edge_cost.model_tokens),
        "env_calls": float(node.edge_cost.env_calls),
        "risk": float(node.edge_cost.risk),
    }


def _observation_record(node: EnvMCTSNode) -> dict[str, Any]:
    observation = node.observation
    if observation is None:
        return {"kind": "empty", "payload": None}
    return {"kind": observation.kind, "payload": _json_safe(observation.payload)}


def _node_chain(node: EnvMCTSNode) -> list[EnvMCTSNode]:
    chain: list[EnvMCTSNode] = []
    cursor: EnvMCTSNode | None = node
    while cursor is not None and cursor.action is not None:
        chain.append(cursor)
        cursor = cursor.parent
    chain.reverse()
    return chain


def _sum_cost(chain: list[EnvMCTSNode]) -> dict[str, float]:
    total = {"wall_ms": 0.0, "model_tokens": 0.0, "env_calls": 0.0, "risk": 0.0}
    for node in chain:
        edge = _cost_record(node)
        for key in total:
            total[key] += edge[key]
    return total


def _trajectory_from_chain(
    chain: list[EnvMCTSNode],
    initial_digest: EnvDigest,
) -> Trajectory:
    steps = tuple(
        TrajectoryStep(
            action=node.action,
            observation=node.observation or Observation(kind="empty", payload=None),
            cost=node.edge_cost,
            post_digest=node.state.digest,
        )
        for node in chain
        if node.action is not None
    )
    return Trajectory(initial_digest=initial_digest, steps=steps)


def _tool_events(
    chain: list[EnvMCTSNode],
    event_ledger: BranchEventLedger | None,
) -> list[dict[str, Any]]:
    if not chain:
        return []
    root = chain[0].parent
    initial_digest = root.state.digest if root is not None else chain[0].state.digest
    events: list[dict[str, Any]] = []
    prefix: list[EnvMCTSNode] = []
    for node in chain:
        parent_trajectory = _trajectory_from_chain(prefix, initial_digest)
        parent_branch_id = branch_id_for_trajectory(parent_trajectory)
        if event_ledger is not None:
            for event in event_ledger.events_for(parent_branch_id):
                events.append({
                    "kind": event.kind,
                    "source": event.source,
                    "branch_id": parent_branch_id,
                    "payload": _json_safe(dict(event.payload)),
                })
        events.append({
            "kind": "tool_intent",
            "source": "cherry_ttt",
            "branch_id": parent_branch_id,
            "payload": _action_record(node),
        })
        prefix.append(node)
        current_trajectory = _trajectory_from_chain(prefix, initial_digest)
        events.append({
            "kind": "tool_observation",
            "source": "cherry_ttt",
            "branch_id": branch_id_for_trajectory(current_trajectory),
            "payload": {
                "observation": _observation_record(node),
                "cost": _cost_record(node),
                "post_digest": str(node.state.digest),
            },
        })
    return events


class TrajectoryCollector:
    """Harvest search structures into sibling-grouped TrajectorySamples.

    Args:
        min_reward_threshold: Samples below it are dropped (original
            TreeRolloutCollector semantics).
        min_group_size: Groups smaller than this are dropped in grouped
            mode — Tree-GRPO advantages need intra-group contrast.
    """

    def __init__(self, min_reward_threshold: float = float("-inf"),
                 min_group_size: int = 2) -> None:
        self.min_reward_threshold = min_reward_threshold
        self.min_group_size = min_group_size

    # -- MCTS harvesting ---------------------------------------------------------

    def collect_from_mcts(
        self,
        root: EnvMCTSNode,
        ctx: str,
        reward_of: Callable[[EnvMCTSNode], float] | None = None,
        min_visits: int = 1,
        event_ledger: BranchEventLedger | None = None,
    ) -> list[TrajectorySample]:
        """Walk the tree; every visited child contributes a sample grouped
        with its siblings (same parent => same group_id by parent path).

        Args:
            root: EnvMCTS tree root (post-search).
            ctx: The root context (prompt analogue).
            reward_of: Node reward extractor; defaults to node.reward when
                set (terminal, verified) else node.value() (search value —
                marked in metadata so training can weight accordingly).
            min_visits: Skip children never actually explored.
        """
        extract = reward_of or (
            lambda n: n.reward if n.reward is not None else n.value())
        samples: list[TrajectorySample] = []

        def walk(node: EnvMCTSNode, labels: list[str]) -> None:
            explored = [c for c in node.children if c.visits >= min_visits]
            if not explored:
                return
            rewards = [float(extract(c)) for c in explored]
            group = path_to_id(labels)  # parent path keys the sibling group
            for child, reward in zip(explored, rewards):
                child_labels = labels + [child.label]
                if reward < self.min_reward_threshold:
                    continue
                chain = _node_chain(child)
                root_node = chain[0].parent
                initial_digest = (root_node.state.digest if root_node is not None
                                  else chain[0].state.digest)
                trajectory = _trajectory_from_chain(chain, initial_digest)
                samples.append(TrajectorySample(
                    ctx=ctx,
                    path=list(child_labels),
                    actions=[_action_record(edge) for edge in chain],
                    reward=reward,
                    process_scores=[],
                    depth=child.depth,
                    visit_count=child.visits,
                    terminal_ctx=child.state.ctx,
                    env_digest=str(child.state.digest),
                    status=("terminal" if child.is_terminal else "interior"),
                    cost=_sum_cost(chain),
                    group_id=group,
                    parent_depth=node.depth,
                    sibling_rewards=list(rewards),
                    metadata={
                        "reward_source": ("verified" if child.reward is not None
                                          else "search_value"),
                        "prior": child.prior,
                        "branch_id": branch_id_for_trajectory(trajectory),
                        "trajectory_complete": True,
                        "node_values": [edge.value() for edge in chain],
                    },
                    observations=[_observation_record(edge) for edge in chain],
                    events=_tool_events(chain, event_ledger),
                ))
            for child in explored:
                walk(child, labels + [child.label])

        walk(root, [])
        return samples

    # -- A* trace harvesting ---------------------------------------------------------

    def collect_from_astar_trace(
        self, trace: list[dict[str, Any]], ctx: str,
    ) -> list[TrajectorySample]:
        """Group decode()/trace records by parent_id (already computed with
        the same path_to_id — ids agree with training group keys for free)."""
        by_parent: dict[str | None, list[dict[str, Any]]] = {}
        for record in trace:
            by_parent.setdefault(record["parent_id"], []).append(record)
        samples: list[TrajectorySample] = []
        for parent_id, records in by_parent.items():
            if parent_id is None or len(records) < self.min_group_size:
                continue
            rewards = [float(r["g_score"] + r.get("terminal_reward", 0.0))
                       for r in records]
            for record, reward in zip(records, rewards):
                if reward < self.min_reward_threshold:
                    continue
                samples.append(TrajectorySample(
                    ctx=ctx,
                    path=list(record["path"]),
                    actions=[{"tool_id": "trace", "args": {"label": record["action"]},
                              "canonical": record["id"]}],
                    reward=reward,
                    process_scores=[float(record["g_score"])],
                    depth=int(record["depth"]),
                    visit_count=1,
                    terminal_ctx=str(record["state"]),
                    env_digest="",
                    status=("terminal" if record.get("is_terminal") else "interior"),
                    cost={"wall_ms": 0.0, "model_tokens": 0.0,
                          "env_calls": 0.0, "risk": 0.0},
                    group_id=parent_id,
                    parent_depth=max(0, int(record["depth"]) - 1),
                    sibling_rewards=list(rewards),
                    metadata={"h_score": record.get("h_score", 0.0)},
                ))
        return samples

    # -- emission ---------------------------------------------------------------------

    @staticmethod
    def to_jsonl(samples: list[TrajectorySample], path: str | Path) -> int:
        """Write the corpus artifact; returns sample count."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            for sample in samples:
                fh.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
        return len(samples)


__all__ = ["TrajectoryCollector", "TrajectorySample"]
