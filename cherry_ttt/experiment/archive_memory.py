"""Falsifiable Cherry pilot over a real signed knowledge-semantic archive.

Source: Cherry ``ContractMDP``/BoN/A* machinery composed with the optional
    ``KSAProjectReadClient`` and ``ArchiveEpisodeSubstrate``.
Integrated: 2026-07-14
Purpose: Measure lexical, signed-graph, granular-temporal, combined, and scope
    tasks without training, canonical mutation, MCTS, or synthetic latency.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ..attention.bias import CandidateMeta
from ..attention.candidate_attention import CandidateAttention
from ..attention.paged_store import PagedCandidateStore
from ..core.contract_mdp import ContractMDP, ContractMDPConfig
from ..core.mdp import State
from ..core.schema import ArgSpec, SchemaRegistry, ToolSchema
from ..core.types import (
    PHASE1_WEIGHTS,
    ActionCandidate,
    Cost,
    GoalSpec,
    PredicateRef,
    TerminalStatus,
    Trajectory,
)
from ..encode.hashing import HashingEncoder
from ..search.astar import EnvAStar, EnvAStarConfig, admissible_unsat_heuristic
from ..search.bon import BestOfNActionSampler
from ..substrate.adapters.archive import ArchiveEpisodeSubstrate
from ..verify.predicates import PredicateRegistry, ReadOnlyView
from .archive_client import (
    GRAPH_QUERY,
    LEXICAL_QUERY,
    SCOPE_QUERY,
    TEMPORAL_QUERY,
    ArchiveFixtureManifest,
    KSAProjectReadClient,
)

_TOKEN_PATTERN = re.compile(r"[\w-]+", re.UNICODE)
_CONTROLLERS = ("greedy_fixed_order", "best_of_n", "astar_cost")


@dataclass(frozen=True, slots=True)
class PilotTask:
    """One goal, protected oracle references, and bounded action space."""

    name: str
    actions: tuple[ActionCandidate, ...]
    required_source_ids: tuple[str, ...]
    forbidden_source_ids: tuple[str, ...] = ()
    required_channel_by_source: tuple[tuple[str, str], ...] = ()
    metadata_requirements: tuple[tuple[str, str, str], ...] = ()
    canonical_graph_target: str | None = None
    temporal_ordinal: int | None = None

    def goal(self) -> GoalSpec:
        """Compile immutable task references into protected predicate refs."""

        channels = dict(self.required_channel_by_source)
        requirements = {
            source_id: (key, value)
            for source_id, key, value in self.metadata_requirements
        }
        predicates = []
        for source_id in self.required_source_ids:
            params: dict[str, object] = {"source_id": source_id}
            if source_id in channels:
                params["channel"] = channels[source_id]
            if source_id in requirements:
                key, value = requirements[source_id]
                params["metadata_key"] = key
                params["metadata_contains"] = value
            predicates.append(PredicateRef("archive_evidence", params))
        if self.forbidden_source_ids:
            predicates.append(
                PredicateRef(
                    "archive_no_forbidden_evidence",
                    {"source_ids": list(self.forbidden_source_ids)},
                )
            )
        return GoalSpec(predicates=tuple(predicates), max_per_action=1)


@dataclass(frozen=True, slots=True)
class ControllerOutcome:
    """One controller/task result with semantic and work-accounting metrics."""

    controller: str
    task: str
    status: str
    downstream_correct: bool
    action_tool_ids: tuple[str, ...]
    action_canonical_ids: tuple[str, ...]
    path_cost: Cost
    spent_cost: Cost
    actual_env_calls: int
    wasted_env_calls: int
    fingerprint_checks: int
    wall_ms: float
    required_source_recall: float
    required_signed_recall: float
    canonical_link_recall: float | None
    proposal_only_target: bool | None
    temporal_ordinal_correct: bool | None
    scope_leakage: int
    canonical_fingerprint_unchanged: bool
    canonical_edge_evidence: int
    proposal_edge_evidence: int
    evidence_source_ids: tuple[str, ...]
    nodes_expanded: int | None = None
    optimal_claim: bool | None = None

    def to_payload(self) -> dict[str, object]:
        """Return deterministic JSON-compatible outcome data."""

        return {
            "controller": self.controller,
            "task": self.task,
            "status": self.status,
            "downstream_correct": self.downstream_correct,
            "actions": [
                {"tool_id": tool_id, "canonical_id": canonical_id}
                for tool_id, canonical_id in zip(
                    self.action_tool_ids,
                    self.action_canonical_ids,
                    strict=True,
                )
            ],
            "path_cost": _cost_payload(self.path_cost),
            "spent_cost": _cost_payload(self.spent_cost),
            "actual_env_calls": self.actual_env_calls,
            "wasted_env_calls": self.wasted_env_calls,
            "fingerprint_checks": self.fingerprint_checks,
            "wall_ms": round(self.wall_ms, 6),
            "required_source_recall": self.required_source_recall,
            "required_signed_recall": self.required_signed_recall,
            "canonical_link_recall": self.canonical_link_recall,
            "proposal_only_target": self.proposal_only_target,
            "temporal_ordinal_correct": self.temporal_ordinal_correct,
            "scope_leakage": self.scope_leakage,
            "canonical_fingerprint_unchanged": self.canonical_fingerprint_unchanged,
            "canonical_edge_evidence": self.canonical_edge_evidence,
            "proposal_edge_evidence": self.proposal_edge_evidence,
            "evidence_source_ids": list(self.evidence_source_ids),
            "nodes_expanded": self.nodes_expanded,
            "optimal_claim": self.optimal_claim,
        }


@dataclass(frozen=True, slots=True)
class ArchivePilotReport:
    """Complete semantic report and its written artifact paths."""

    payload: Mapping[str, object]
    json_path: Path
    markdown_path: Path
    log_path: Path

    @property
    def status(self) -> str:
        """Return the top-level pass/fail status."""

        return str(self.payload["status"])


class ArchiveEvidencePredicate:
    """Require one source id and optional immutable evidence attributes."""

    name = "archive_evidence"

    def __init__(self, params: Mapping[str, object]) -> None:
        self._source_id = str(params["source_id"])
        self._channel = str(params["channel"]) if "channel" in params else None
        self._metadata_key = (
            str(params["metadata_key"]) if "metadata_key" in params else None
        )
        self._metadata_contains = (
            str(params["metadata_contains"])
            if "metadata_contains" in params
            else None
        )

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float:
        """Return one only when immutable trajectory evidence satisfies the ref."""

        del sub
        for item in _trajectory_evidence(trajectory):
            if item.get("source_id") != self._source_id:
                continue
            if self._channel is not None and item.get("channel") != self._channel:
                continue
            metadata = _evidence_metadata(item)
            if self._metadata_key is not None:
                value = metadata.get(self._metadata_key, "")
                if self._metadata_contains not in value:
                    continue
            return 1.0
        return 0.0


class ArchiveNoForbiddenEvidencePredicate:
    """Reject any forbidden source id observed in a trajectory."""

    name = "archive_no_forbidden_evidence"

    def __init__(self, params: Mapping[str, object]) -> None:
        raw = params.get("source_ids", ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise TypeError("source_ids must be a sequence")
        self._forbidden = frozenset(str(item) for item in raw)

    def check(self, sub: ReadOnlyView, trajectory: Trajectory) -> float:
        """Return one while all protected foreign ids remain absent."""

        del sub
        observed = {str(item.get("source_id", "")) for item in _trajectory_evidence(trajectory)}
        return 1.0 if not observed.intersection(self._forbidden) else 0.0


class FixedArchiveProposer:
    """Expose one task's bounded action set in deterministic fixed order."""

    def __init__(self, actions: Sequence[ActionCandidate]) -> None:
        self._actions = tuple(actions)

    def propose(self, s: State, n: int) -> list[tuple[ActionCandidate, float]]:
        """Return the first ``n`` task actions with fixed descending priors."""

        del s
        return [
            (action, 1.0 / (index + 1))
            for index, action in enumerate(self._actions[:n])
        ]


def run_archive_memory_pilot(
    output_dir: Path,
    *,
    runtime_root: Path | None = None,
) -> ArchivePilotReport:
    """Run the real KSA pilot and write deterministic JSON/Markdown/log artifacts.

    Args:
        output_dir: Caller-owned directory receiving ``result.{json,md,log}``.
        runtime_root: Optional fresh disposable archive root. When omitted, a
            temporary directory is created and removed after the run.

    Returns:
        ArchivePilotReport containing the semantic result and artifact paths.

    Raises:
        ArchivePilotUnavailable: If knowledge-semantic-archive is not importable.
        ArchivePilotInvariantError: If the real fixture violates its proof shape.
    """

    if not isinstance(output_dir, Path):
        raise TypeError("output_dir must be pathlib.Path")
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    run_started = time.perf_counter()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if runtime_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="cherry-ksa-pilot-")
        selected_runtime_root = Path(temporary.name) / "runtime"
    else:
        selected_runtime_root = runtime_root

    try:
        with KSAProjectReadClient(selected_runtime_root) as client:
            fixture = client.fixture
            frozen_fingerprint = client.canonical_fingerprint()
            tasks = _build_tasks(fixture)
            outcomes = []
            for task in tasks:
                for controller in _CONTROLLERS:
                    outcomes.append(_run_controller(client, task, controller))
            lane_ablation = _run_lane_ablation(client, tasks)
            ranking_ablation = _candidate_attention_ablation(client, fixture)
            final_fingerprint = client.canonical_fingerprint()
    finally:
        if temporary is not None:
            temporary.cleanup()

    all_correct = all(outcome.downstream_correct for outcome in outcomes)
    all_frozen = all(
        outcome.canonical_fingerprint_unchanged for outcome in outcomes
    ) and final_fingerprint == frozen_fingerprint
    lane_matches = all(
        bool(item["matches_expected"])
        and bool(item["canonical_fingerprint_unchanged"])
        for item in lane_ablation
    )
    aggregate = _aggregate(outcomes)
    payload: dict[str, object] = {
        "schema_version": "cherry-archive-pilot/v1",
        "status": "pass" if all_correct and all_frozen and lane_matches else "fail",
        "run_metadata": {
            "clock": "UTC",
            "started_at_utc": started_at.isoformat(),
            "wall_ms": round((time.perf_counter() - run_started) * 1_000.0, 6),
            "archive_runtime": "disposable_local",
        },
        "scope": {
            "training": False,
            "canonical_mutation_during_search": False,
            "controllers": list(_CONTROLLERS),
            "tasks": [task.name for task in tasks],
        },
        "fixture": fixture.to_payload(),
        "canonical_fingerprint": {
            "before": frozen_fingerprint,
            "after": final_fingerprint,
            "unchanged": final_fingerprint == frozen_fingerprint,
            "basis": "validated public records plus canonical-link edges and canonical count",
        },
        "outcomes": [outcome.to_payload() for outcome in outcomes],
        "lane_ablation": lane_ablation,
        "aggregate": aggregate,
        "candidate_attention_ablation": ranking_ablation,
        "exclusions": {
            "mcts": (
                "excluded: current EnvMCTS root does not progressively widen after "
                "its first expansion; no MCTS result is evidence in this pilot"
            ),
            "speculative_latency": (
                "excluded: run_overlapped executes real drafts sequentially and its "
                "LatencyModel is synthetic; no concurrency or latency superiority claim"
            ),
            "training": "excluded: this is inference-time mechanism validation only",
        },
        "interpretation": _interpretation(aggregate, ranking_ablation),
    }
    return _write_report(output_dir, payload)


def _run_controller(
    client: KSAProjectReadClient,
    task: PilotTask,
    controller: str,
) -> ControllerOutcome:
    """Execute one fresh read-only episode and evaluate protected evidence."""

    initial_fingerprint = client.canonical_fingerprint()
    schema = _archive_schema()
    predicates = _archive_predicates()
    substrate = ArchiveEpisodeSubstrate(
        client,
        oracle_evidence_ids=task.required_source_ids,
        substrate_id=f"archive-{controller}-{task.name}",
    )
    mdp = ContractMDP(
        substrate,
        FixedArchiveProposer(task.actions),
        schema,
        predicates,
        # ContractMDP classifies depth >= max_depth as BUDGET before checking
        # predicates, so allow the final fixed-order action to be verified.
        ContractMDPConfig(max_depth=max(4, len(task.actions) + 1)),
    )
    goal = task.goal()
    reads_before = client.read_calls
    read_wall_before = client.read_wall_ms
    checks_before = client.fingerprint_checks
    started = time.perf_counter()

    nodes_expanded: int | None = None
    optimal_claim: bool | None = None
    if controller == "greedy_fixed_order":
        state, actions, path_cost, status = _run_greedy(mdp, goal, task)
    elif controller == "best_of_n":
        result = BestOfNActionSampler(
            mdp,
            n=len(task.actions),
            diversity_weight=0.0,
        ).run(goal, task.name, max_steps=len(task.actions))
        if result.state is None:
            raise RuntimeError("BestOfN returned no state")
        state = result.state
        actions = result.actions
        path_cost = result.committed_cost
        status = result.status
    elif controller == "astar_cost":
        search = EnvAStar(
            mdp,
            EnvAStarConfig(
                max_nodes=64,
                max_depth=len(task.actions),
                n_actions=len(task.actions),
                use_value_heuristic=False,
            ),
            goal=goal,
        )
        result = search.search(
            task.name,
            weights=PHASE1_WEIGHTS,
            heuristic=admissible_unsat_heuristic(mdp, goal),
            max_nodes=64,
            n_actions=len(task.actions),
        )
        if result.state is None:
            raise RuntimeError("A* returned no state")
        state = result.state
        actions = result.actions
        path_cost = result.total_cost
        status = result.status
        nodes_expanded = result.nodes_expanded
        optimal_claim = result.optimal_claim
    else:
        raise ValueError(f"unknown pilot controller {controller!r}")

    trajectory = mdp.trajectory_of(state)
    wall_ms = (time.perf_counter() - started) * 1_000.0
    final_fingerprint = client.canonical_fingerprint()
    actual_env_calls = client.read_calls - reads_before
    actual_read_wall_ms = client.read_wall_ms - read_wall_before
    fingerprint_checks = client.fingerprint_checks - checks_before
    evidence = _trajectory_evidence(trajectory)
    metrics = _evidence_metrics(task, evidence, status)
    committed_env_calls = path_cost.env_calls
    return ControllerOutcome(
        controller=controller,
        task=task.name,
        status=status.name.lower(),
        downstream_correct=metrics["downstream_correct"],
        action_tool_ids=tuple(action.tool_id for action in actions),
        action_canonical_ids=tuple(action.canonical() for action in actions),
        path_cost=path_cost,
        # SearchResult.total_cost is selected-path cost. Actual discarded
        # branch/trial work is reconstructed from the real bridge counters so
        # A* and BoN cannot hide exploratory archive reads.
        spent_cost=Cost(
            wall_ms=actual_read_wall_ms,
            env_calls=actual_env_calls,
            risk=0.0,
        ),
        actual_env_calls=actual_env_calls,
        wasted_env_calls=max(0, actual_env_calls - committed_env_calls),
        fingerprint_checks=fingerprint_checks,
        wall_ms=wall_ms,
        required_source_recall=metrics["required_source_recall"],
        required_signed_recall=metrics["required_signed_recall"],
        canonical_link_recall=metrics["canonical_link_recall"],
        proposal_only_target=metrics["proposal_only_target"],
        temporal_ordinal_correct=metrics["temporal_ordinal_correct"],
        scope_leakage=metrics["scope_leakage"],
        canonical_fingerprint_unchanged=(final_fingerprint == initial_fingerprint),
        canonical_edge_evidence=metrics["canonical_edge_evidence"],
        proposal_edge_evidence=metrics["proposal_edge_evidence"],
        evidence_source_ids=tuple(
            sorted({str(item.get("source_id", "")) for item in evidence})
        ),
        nodes_expanded=nodes_expanded,
        optimal_claim=optimal_claim,
    )


def _run_greedy(
    mdp: ContractMDP,
    goal: GoalSpec,
    task: PilotTask,
) -> tuple[State, tuple[ActionCandidate, ...], Cost, TerminalStatus]:
    """Commit legal actions in fixed order until the protected goal is solved."""

    state = mdp.initial_state(goal, task.name)
    actions = []
    total = Cost()
    for action in task.actions:
        if mdp.is_terminal(state) is TerminalStatus.SOLVED:
            break
        legal = {candidate.canonical(): candidate for candidate, _ in mdp.legal_actions(state, 99)}
        conformed = legal.get(action.canonical())
        if conformed is None:
            continue
        state, _observation, cost = mdp.transition(state, conformed)
        actions.append(conformed)
        total = total + cost
    status = mdp.is_terminal(state)
    if status is TerminalStatus.OPEN:
        status = TerminalStatus.BUDGET
    return state, tuple(actions), total, status


def _build_tasks(fixture: ArchiveFixtureManifest) -> tuple[PilotTask, ...]:
    """Create the five predeclared tasks from runtime canonical identifiers."""

    return (
        PilotTask(
            name="lexical",
            actions=_actions(LEXICAL_QUERY, LEXICAL_QUERY, LEXICAL_QUERY),
            required_source_ids=(fixture.lexical_record_id,),
            required_channel_by_source=((fixture.lexical_record_id, "lexical"),),
        ),
        PilotTask(
            name="graph_signed_hop",
            actions=_actions(GRAPH_QUERY, GRAPH_QUERY, GRAPH_QUERY),
            required_source_ids=(fixture.graph_target_record_id,),
            required_channel_by_source=((fixture.graph_target_record_id, "graph"),),
            metadata_requirements=(
                (fixture.graph_target_record_id, "path_provenance", "canonical_link"),
            ),
            canonical_graph_target=fixture.graph_target_record_id,
        ),
        PilotTask(
            name="temporal_ordinal",
            actions=_actions(TEMPORAL_QUERY, TEMPORAL_QUERY, TEMPORAL_QUERY),
            required_source_ids=(fixture.temporal_message_source_id,),
            required_channel_by_source=((fixture.temporal_message_source_id, "temporal"),),
            metadata_requirements=(
                (fixture.temporal_message_source_id, "ordinal", "1"),
            ),
            temporal_ordinal=1,
        ),
        PilotTask(
            name="combined_graph_temporal",
            actions=_actions(GRAPH_QUERY, GRAPH_QUERY, TEMPORAL_QUERY),
            required_source_ids=(
                fixture.graph_target_record_id,
                fixture.temporal_message_source_id,
            ),
            required_channel_by_source=(
                (fixture.graph_target_record_id, "graph"),
                (fixture.temporal_message_source_id, "temporal"),
            ),
            metadata_requirements=(
                (fixture.graph_target_record_id, "path_provenance", "canonical_link"),
                (fixture.temporal_message_source_id, "ordinal", "1"),
            ),
            canonical_graph_target=fixture.graph_target_record_id,
            temporal_ordinal=1,
        ),
        PilotTask(
            name="scope_isolation",
            actions=_actions(SCOPE_QUERY, SCOPE_QUERY, SCOPE_QUERY),
            required_source_ids=(fixture.scope_record_id,),
            forbidden_source_ids=(fixture.foreign_record_id,),
            required_channel_by_source=((fixture.scope_record_id, "lexical"),),
        ),
    )


def _actions(
    lexical_query: str,
    graph_query: str,
    temporal_query: str,
) -> tuple[ActionCandidate, ...]:
    """Return the bounded lexical, graph, and temporal action sequence."""

    return (
        ActionCandidate("archive.search", {"query": lexical_query, "limit": 100}),
        ActionCandidate(
            "archive.explore_knowledge_graph",
            {
                "query": graph_query,
                "limit": 100,
                "graph_limit": 100,
                "hop_depth": 2,
            },
        ),
        ActionCandidate(
            "archive.search_conversation_messages",
            {"query": temporal_query, "limit": 100},
        ),
    )


def _archive_schema() -> SchemaRegistry:
    """Declare exactly the read actions admitted by this pilot."""

    schema = SchemaRegistry()
    schema.declare(
        ToolSchema(
            "archive.search",
            {"query": ArgSpec("str"), "limit": ArgSpec("int")},
        )
    )
    schema.declare(
        ToolSchema(
            "archive.explore_knowledge_graph",
            {
                "query": ArgSpec("str"),
                "limit": ArgSpec("int"),
                "graph_limit": ArgSpec("int"),
                "hop_depth": ArgSpec("int"),
            },
        )
    )
    schema.declare(
        ToolSchema(
            "archive.search_conversation_messages",
            {"query": ArgSpec("str"), "limit": ArgSpec("int")},
        )
    )
    return schema


def _archive_predicates() -> PredicateRegistry:
    """Register trajectory-only archive predicates with immutable refs."""

    predicates = PredicateRegistry()
    predicates.register("archive_evidence", ArchiveEvidencePredicate)
    predicates.register(
        "archive_no_forbidden_evidence",
        ArchiveNoForbiddenEvidencePredicate,
    )
    return predicates


def _trajectory_evidence(trajectory: Trajectory) -> list[dict[str, object]]:
    """Extract normalized immutable evidence payloads from committed steps."""

    evidence = []
    for step in trajectory.steps:
        payload = step.observation.payload
        if not isinstance(payload, Mapping):
            continue
        result = payload.get("result")
        if not isinstance(result, Mapping):
            continue
        raw_items = result.get("evidence")
        if not isinstance(raw_items, Sequence) or isinstance(
            raw_items,
            (str, bytes, bytearray),
        ):
            continue
        for raw in raw_items:
            if isinstance(raw, Mapping):
                evidence.append({str(key): value for key, value in raw.items()})
    return evidence


def _evidence_metadata(item: Mapping[str, object]) -> dict[str, str]:
    """Decode ArchiveEvidence's sorted pair representation."""

    raw = item.get("metadata", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return {}
    metadata = {}
    for pair in raw:
        if (
            isinstance(pair, Sequence)
            and not isinstance(pair, (str, bytes, bytearray))
            and len(pair) == 2
        ):
            metadata[str(pair[0])] = str(pair[1])
    return metadata


def _evidence_metrics(
    task: PilotTask,
    evidence: Sequence[Mapping[str, object]],
    status: TerminalStatus,
) -> dict[str, object]:
    """Score authority, graph provenance, temporal ordinal, and leakage."""

    by_source: dict[str, list[Mapping[str, object]]] = {}
    for item in evidence:
        by_source.setdefault(str(item.get("source_id", "")), []).append(item)
    required_found = [source_id in by_source for source_id in task.required_source_ids]
    required_source_recall = sum(required_found) / max(1, len(required_found))
    signed_found = []
    for source_id in task.required_source_ids:
        items = by_source.get(source_id, ())
        signed_found.append(
            any(
                _evidence_metadata(item).get("authority") == "signed_usms_validated"
                for item in items
            )
        )
    required_signed_recall = sum(signed_found) / max(1, len(signed_found))
    scope_leakage = sum(
        1 for source_id in task.forbidden_source_ids if source_id in by_source
    )
    canonical_edges = 0
    proposal_edges = 0
    for item in evidence:
        metadata = _evidence_metadata(item)
        if metadata.get("item_kind") != "edge":
            continue
        if metadata.get("provenance") == "canonical_link":
            canonical_edges += 1
        elif metadata.get("provenance") == "derived_similarity_proposal":
            proposal_edges += 1

    canonical_recall: float | None = None
    proposal_only: bool | None = None
    if task.canonical_graph_target is not None:
        target_items = by_source.get(task.canonical_graph_target, ())
        canonical = any(
            "canonical_link" in _evidence_metadata(item).get("path_provenance", "")
            for item in target_items
        )
        proposal = any(
            "derived_similarity_proposal"
            in _evidence_metadata(item).get("path_provenance", "")
            for item in target_items
        )
        canonical_recall = 1.0 if canonical else 0.0
        proposal_only = proposal and not canonical

    ordinal_correct: bool | None = None
    if task.temporal_ordinal is not None:
        expected = str(task.temporal_ordinal)
        ordinal_correct = any(
            _evidence_metadata(item).get("ordinal") == expected
            for source_id in task.required_source_ids
            for item in by_source.get(source_id, ())
        )
    downstream_correct = (
        status is TerminalStatus.SOLVED
        and required_source_recall == 1.0
        and required_signed_recall == 1.0
        and scope_leakage == 0
        and canonical_recall in (None, 1.0)
        and ordinal_correct in (None, True)
    )
    return {
        "downstream_correct": downstream_correct,
        "required_source_recall": required_source_recall,
        "required_signed_recall": required_signed_recall,
        "canonical_link_recall": canonical_recall,
        "proposal_only_target": proposal_only,
        "temporal_ordinal_correct": ordinal_correct,
        "scope_leakage": scope_leakage,
        "canonical_edge_evidence": canonical_edges,
        "proposal_edge_evidence": proposal_edges,
    }


def _candidate_attention_ablation(
    client: KSAProjectReadClient,
    fixture: ArchiveFixtureManifest,
) -> dict[str, object]:
    """Compare normalized hash cosine with exact CandidateAttention scores."""

    result = client.explore_knowledge_graph(
        GRAPH_QUERY,
        limit=100,
        graph_limit=100,
        hop_depth=2,
    )
    candidates = tuple(
        item
        for item in result.evidence
        if dict(item.metadata).get("item_kind") == "record"
    )
    if not candidates:
        return {
            "available": False,
            "reason": "real graph read returned no record evidence",
            "claim": "deferred; no synthetic ranking result substituted",
        }
    dim = 128
    encoder = HashingEncoder(dim=dim, salt="archive-evidence-ranking")
    query_vector = encoder.encode_tokens(_lexical_tokens(GRAPH_QUERY))
    store = PagedCandidateStore(dim=dim, page_size=32)
    vectors = []
    source_ids = []
    for item in candidates:
        vector = encoder.encode_tokens(_lexical_tokens(item.content))
        vectors.append(vector)
        source_ids.append(item.source_id)
        store.add(
            item.source_id,
            vector,
            meta=CandidateMeta(
                namespace=fixture.primary_project_id,
                type_name=item.channel,
                acl="project",
            ),
            payload=item.source_id,
        )
    attention = CandidateAttention(dim).attend(
        query_vector,
        store,
        top_k=len(candidates),
    )
    attention_scores = {
        key: float(score) * math.sqrt(dim)
        for key, score in zip(
            attention.topk_keys[0],
            attention.topk_scores[0],
            strict=True,
        )
    }
    cosine_scores = {
        source_id: float(query_vector @ vector)
        for source_id, vector in zip(source_ids, vectors, strict=True)
    }
    attention_order = sorted(
        source_ids,
        key=lambda source_id: (-attention_scores[source_id], source_id),
    )
    cosine_order = sorted(
        source_ids,
        key=lambda source_id: (-cosine_scores[source_id], source_id),
    )
    max_abs_delta = max(
        abs(attention_scores[source_id] - cosine_scores[source_id])
        for source_id in source_ids
    )
    return {
        "available": True,
        "claim": (
            "mechanism smoke only: untrained normalized HashingEncoder vectors; "
            "no learned or superiority claim"
        ),
        "candidate_count": len(source_ids),
        "candidate_attention_order": attention_order,
        "cosine_order": cosine_order,
        "rankings_equal": attention_order == cosine_order,
        "max_abs_score_delta_after_scale": max_abs_delta,
        "required_graph_target_rank": (
            attention_order.index(fixture.graph_target_record_id) + 1
            if fixture.graph_target_record_id in attention_order
            else None
        ),
        "expected_relation": (
            "CandidateAttention without structured bias is scaled dot product; "
            "unit-normalized vectors should tie cosine up to floating precision"
        ),
    }


def _run_lane_ablation(
    client: KSAProjectReadClient,
    tasks: Sequence[PilotTask],
) -> list[dict[str, object]]:
    """Run fixed-order lane subsets and compare against preregistered capability."""

    arms = {
        "lexical_only": (0,),
        "graph_only": (1,),
        "temporal_only": (2,),
        "graph_temporal": (1, 2),
    }
    expected = {
        "lexical": {"lexical_only"},
        "graph_signed_hop": {"graph_only", "graph_temporal"},
        "temporal_ordinal": {"temporal_only", "graph_temporal"},
        "combined_graph_temporal": {"graph_temporal"},
        "scope_isolation": {"lexical_only"},
    }
    results = []
    for task in tasks:
        for arm, indices in arms.items():
            ablated = replace(
                task,
                actions=tuple(task.actions[index] for index in indices),
            )
            outcome = _run_controller(client, ablated, "greedy_fixed_order")
            expected_correct = arm in expected[task.name]
            results.append(
                {
                    "task": task.name,
                    "arm": arm,
                    "expected_correct": expected_correct,
                    "observed_correct": outcome.downstream_correct,
                    "matches_expected": outcome.downstream_correct == expected_correct,
                    "actual_env_calls": outcome.actual_env_calls,
                    "required_source_recall": outcome.required_source_recall,
                    "required_signed_recall": outcome.required_signed_recall,
                    "canonical_link_recall": outcome.canonical_link_recall,
                    "temporal_ordinal_correct": outcome.temporal_ordinal_correct,
                    "scope_leakage": outcome.scope_leakage,
                    "canonical_fingerprint_unchanged": (
                        outcome.canonical_fingerprint_unchanged
                    ),
                }
            )
    return results


def _lexical_tokens(text: str) -> tuple[str, ...]:
    """Return deterministic case-folded tokens for the mechanism ablation."""

    return tuple(token.casefold() for token in _TOKEN_PATTERN.findall(text))


def _aggregate(outcomes: Sequence[ControllerOutcome]) -> dict[str, object]:
    """Aggregate factual controller metrics without selecting a winner."""

    aggregate = {}
    for controller in _CONTROLLERS:
        selected = [outcome for outcome in outcomes if outcome.controller == controller]
        aggregate[controller] = {
            "tasks": len(selected),
            "solved_and_correct": sum(outcome.downstream_correct for outcome in selected),
            "actual_env_calls": sum(outcome.actual_env_calls for outcome in selected),
            "path_env_calls": sum(outcome.path_cost.env_calls for outcome in selected),
            "wasted_env_calls": sum(outcome.wasted_env_calls for outcome in selected),
            "wall_ms": round(sum(outcome.wall_ms for outcome in selected), 6),
            "scope_leakage": sum(outcome.scope_leakage for outcome in selected),
            "fingerprints_unchanged": all(
                outcome.canonical_fingerprint_unchanged for outcome in selected
            ),
        }
    return aggregate


def _interpretation(
    aggregate: Mapping[str, object],
    ranking_ablation: Mapping[str, object],
) -> dict[str, object]:
    """State exactly what this small unmatched-work pilot can and cannot claim."""

    controller_calls = {
        controller: int(metrics["actual_env_calls"])
        for controller, metrics in aggregate.items()
        if isinstance(metrics, Mapping)
    }
    return {
        "controller_work_matched": False,
        "reason": (
            "BoN trials and A* branch expansions are charged as actual archive calls; "
            "the fixed-order baseline commits only its chosen path. Compare both "
            "actual and path calls; do not infer superiority from solve rate alone."
        ),
        "actual_env_calls_by_controller": controller_calls,
        "candidate_attention_result": (
            "expected mechanism equivalence"
            if ranking_ablation.get("rankings_equal") is True
            else "no equivalence result"
        ),
        "validated_claim": (
            "Cherry can search reversible evidence ledgers over real scoped signed "
            "archive reads while preserving canonical state; this pilot does not "
            "validate training or general performance superiority."
        ),
    }


def _cost_payload(cost: Cost) -> dict[str, object]:
    """Return one uncollapsed cost vector."""

    return {
        "wall_ms": round(cost.wall_ms, 6),
        "model_tokens": cost.model_tokens,
        "env_calls": cost.env_calls,
        "risk": cost.risk,
    }


def _write_report(output_dir: Path, payload: Mapping[str, object]) -> ArchivePilotReport:
    """Write deterministic JSON, concise Markdown, and a plain run log."""

    json_path = output_dir / "result.json"
    markdown_path = output_dir / "result.md"
    log_path = output_dir / "result.log"
    json_text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    json_path.write_text(json_text, encoding="utf-8")

    aggregate = payload["aggregate"]
    lines = [
        "# Cherry real archive pilot",
        "",
        f"Status: **{payload['status']}**",
        "",
        "I ran fixed-order greedy, BestOfN, and cost-regime A* against a disposable ",
        "real knowledge-semantic-archive service. The action surface was read-only; ",
        "the reversible state was Cherry's evidence ledger.",
        "",
        "## Controller accounting",
        "",
        "| Controller | Correct tasks | Actual env calls | Path env calls | Wasted calls | Leakage |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    if isinstance(aggregate, Mapping):
        for controller in _CONTROLLERS:
            metrics = aggregate[controller]
            if isinstance(metrics, Mapping):
                lines.append(
                    f"| `{controller}` | {metrics['solved_and_correct']}/{metrics['tasks']} "
                    f"| {metrics['actual_env_calls']} | {metrics['path_env_calls']} "
                    f"| {metrics['wasted_env_calls']} | {metrics['scope_leakage']} |"
                )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "I do not call this a trained result or a controller-superiority result. ",
            "Total work is intentionally reported because BoN trials and A* expansions ",
            "are not free. MCTS and speculative latency are excluded for the reasons ",
            "recorded in `result.json`.",
            "",
            "The CandidateAttention comparison is a normalized deterministic hashing ",
            "mechanism smoke. With no structured bias or trained projection it should ",
            "match cosine ranking up to floating precision; a tie is the expected result.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")

    outcomes = payload["outcomes"]
    log_lines = [f"status={payload['status']}"]
    if isinstance(outcomes, Sequence):
        for outcome in outcomes:
            if isinstance(outcome, Mapping):
                log_lines.append(
                    " ".join(
                        (
                            f"controller={outcome['controller']}",
                            f"task={outcome['task']}",
                            f"status={outcome['status']}",
                            f"correct={outcome['downstream_correct']}",
                            f"actual_env_calls={outcome['actual_env_calls']}",
                            f"scope_leakage={outcome['scope_leakage']}",
                            f"fingerprint_unchanged={outcome['canonical_fingerprint_unchanged']}",
                        )
                    )
                )
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return ArchivePilotReport(
        payload=payload,
        json_path=json_path,
        markdown_path=markdown_path,
        log_path=log_path,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the real pilot from the command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args(argv)
    report = run_archive_memory_pilot(
        args.output_dir,
        runtime_root=args.runtime_root,
    )
    print(report.json_path)
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ArchivePilotReport",
    "ControllerOutcome",
    "PilotTask",
    "run_archive_memory_pilot",
]
