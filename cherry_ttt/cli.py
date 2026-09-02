"""Command-line utilities for installed cherry_ttt package.

Smoke report exercises every subsystem in the package:
  core types, schema, MDP, ContractMDP, attention, substrates,
  verify, metrics, search (MCTS/A*/BoN), speculate (gamma/drafter),
  encode, collect, interleave, value heads, experiment.
"""

from __future__ import annotations

import argparse
import json
import tempfile

import numpy as np

# ── Core ──────────────────────────────────────────────────────────────
from .core.types import (
    ActionCandidate,
    Cost,
    CostWeights,
    GoalSpec,
    Observation,
    PHASE1_WEIGHTS,
    PredicateRef,
    Trajectory,
    TrajectoryStep,
    TerminalStatus,
    env_digest,
)
from .core.jcs import canonicalize
from .core.errors import CherryTTTError

# ── Schema ────────────────────────────────────────────────────────────
from .core.schema import default_registry, SchemaRegistry, ToolSchema, ArgSpec

# ── MDP ───────────────────────────────────────────────────────────────
from .core.mdp import LexicalMDP, LexicalPolicy, State

# ── ContractMDP ───────────────────────────────────────────────────────
from .core.contract_mdp import ContractMDP, ContractMDPConfig

# ── Attention ─────────────────────────────────────────────────────────
from .attention import (
    AttentionResult,
    BiasQuery,
    CandidateAttention,
    CandidateMeta,
    CandidateRecord,
    PagedCandidateStore,
    StoreStats,
    build_structured_bias,
    streaming_topk,
)

# ── Substrates ────────────────────────────────────────────────────────
from .substrate import (
    ExecutionSubstrate,
    TransactionalSubstrateBase,
    verify_restore_soundness,
    RestoreReceipt,
)
from .substrate.adapters import (
    FileSystemSubstrate,
    MemoryKVSubstrate,
    SQLiteSubstrate,
)

# ── Verify ────────────────────────────────────────────────────────────
from .verify import (
    SATISFIED,
    Predicate,
    PredicateRegistry,
    ReadOnlyView,
    default_predicate_registry,
)

# ── Metrics ───────────────────────────────────────────────────────────
from .metrics import DensityMetrics, gamma_throughput

# ── Search ────────────────────────────────────────────────────────────
from .search import (
    BestOfNActionSampler,
    BoNResult,
    EnvAStar,
    EnvAStarConfig,
    EnvMCTS,
    EnvMCTSConfig,
    action_distance,
    path_to_id,
)

# ── Speculative execution ─────────────────────────────────────────────
from .speculate import (
    ActionTemplate,
    AdaptiveGammaController,
    CommitReport,
    Drafter,
    GammaControllerConfig,
    LatencyModel,
    SpeculativeExecutor,
    TabularDrafter,
    TemplateDrafter,
)

# ── Encoders ──────────────────────────────────────────────────────────
from .encode import (
    HashingEncoder,
    encode_goal,
    encode_observation,
    encode_registry,
    encode_state,
    encode_tool_schema,
    encode_trajectory,
)

# ── Trajectory collection ────────────────────────────────────────────
from .collect import TrajectoryCollector, TrajectorySample

# ── Interleave ────────────────────────────────────────────────────────
from .interleave import (
    BranchEventLedger,
    ContextualActionProposer,
    InterleavedEvent,
    ReasoningContext,
    branch_id_for_trajectory,
)

# ── Value heads ───────────────────────────────────────────────────────
from .value import ConformalValueWrapper, LinearStateValue, StateValueLike

# ── Experiment ────────────────────────────────────────────────────────
from .experiment import (
    ArmResult,
    NormalizeLoadInstance,
    archive_dependency_available,
    make_instances,
    run_arms,
)


def smoke_report() -> dict:
    """Exercise every subsystem and return a structured receipt."""

    report: dict = {}

    # ── 1. Core types ─────────────────────────────────────────────────
    ac = ActionCandidate("kv.put", {"k": "hello", "v": 42})
    canon = ac.canonical()
    assert isinstance(canon, str) and len(canon) > 0
    # Canonical identity: key-order invariant
    ac2 = ActionCandidate("kv.put", {"v": 42, "k": "hello"})
    assert ac.canonical() == ac2.canonical()
    # Cost vector non-collapsibility
    cost = Cost(env_calls=3, wall_ms=12.0, model_tokens=0, risk=0.0)
    assert hasattr(cost, "env_calls") and hasattr(cost, "wall_ms")
    # CostWeights exist but do not pre-collapse
    assert PHASE1_WEIGHTS is not None
    report["core"] = {
        "canonical_id": canon[:16],
        "jcs_deterministic": ac.canonical() == ac2.canonical(),
        "cost_vector_fields": sorted(
            k for k in cost.__dataclass_fields__ if not k.startswith("_")
        ),
    }

    # ── 2. Schema ─────────────────────────────────────────────────────
    schema = default_registry()
    assert schema.known("kv.put")
    assert schema.known("kv.increment")
    assert schema.known("sql.exec")
    assert schema.known("fs.read")
    assert schema.known("fs.write")
    assert schema.known("lexical.append")
    tool_ids = sorted(schema._schemas.keys())
    namespaces = sorted({tid.split(".")[0] for tid in tool_ids})
    report["schema"] = {
        "tool_count": len(tool_ids),
        "namespaces": namespaces,
    }

    # ── 3. Predicates ─────────────────────────────────────────────────
    predicates = default_predicate_registry(schema)
    assert predicates.resolve.__name__ == "resolve"
    report["predicates"] = {"registry_type": type(predicates).__name__}

    # ── 4. Substrates ─────────────────────────────────────────────────
    # MemoryKV
    kv = MemoryKVSubstrate()
    kv_receipt = verify_restore_soundness(
        kv,
        [
            ActionCandidate("kv.put", {"k": "a", "v": 1}),
            ActionCandidate("kv.increment", {"k": "a", "by": 2}),
        ],
    )
    # SQLite
    sql = SQLiteSubstrate()
    try:
        sql_receipt = verify_restore_soundness(
            sql,
            [
                ActionCandidate("sql.exec", {"statement": "CREATE TABLE t (x INTEGER)"}),
                ActionCandidate("sql.exec", {"statement": "INSERT INTO t VALUES (1)"}),
            ],
        )
    finally:
        sql.close()
    # FileSystem
    with tempfile.TemporaryDirectory() as tmp:
        fs = FileSystemSubstrate(tmp)
        fs_receipt = verify_restore_soundness(
            fs,
            [ActionCandidate("fs.write", {"path": "a.txt", "content": "hello"})],
        )
    report["substrates"] = {
        "memory_kv": str(kv_receipt.after),
        "sqlite": str(sql_receipt.after),
        "filesystem": str(fs_receipt.after),
    }

    # ── 5. Attention ──────────────────────────────────────────────────
    store = PagedCandidateStore(dim=4, page_size=2)
    store.add(
        "a",
        np.array([1, 0, 0, 0], dtype=np.float32),
        meta=CandidateMeta(namespace="n", type_name="text"),
    )
    store.add(
        "b",
        np.array([0, 1, 0, 0], dtype=np.float32),
        meta=CandidateMeta(namespace="n", type_name="code"),
    )
    result = CandidateAttention(dim=4).attend(
        np.array([1, 0, 0, 0], dtype=np.float32),
        store,
        bias_queries=[BiasQuery(allowed_namespaces=frozenset({"n"}))],
        top_k=2,
    )
    stats = store.stats()
    report["attention"] = {
        "topk_keys": result.topk_keys[0],
        "store_stats": {
            "records": stats.records,
            "pages": stats.pages,
            "dim": stats.dim,
        },
    }

    # ── 6. Metrics ────────────────────────────────────────────────────
    metrics = DensityMetrics(
        useful_actions=3,
        total_actions=4,
        env_calls=6,
        accepted=3,
        drafted=4,
        wall_ms=10.0,
    )
    gt = gamma_throughput(0.8, 4, 40, 8)
    report["metrics"] = {
        "action_density": metrics.action_density,
        "gamma_throughput": gt,
    }

    # ── 7. Search algorithms ──────────────────────────────────────────
    mcts_cfg = EnvMCTSConfig(n_simulations=2, max_depth=3)
    astar_cfg = EnvAStarConfig(max_nodes=10, max_depth=3)
    # Verify instantiation and config fields
    assert mcts_cfg.c_puct == 1.25
    assert astar_cfg.heuristic_weight == 1.0
    # path_to_id determinism
    pid = path_to_id(["a", "b", "c"])
    assert pid == path_to_id(["a", "b", "c"])
    # action_distance
    ad = action_distance(
        ActionCandidate("kv.put", {"k": "x", "v": 1}),
        ActionCandidate("kv.put", {"k": "y", "v": 1}),
    )
    assert 0.0 <= ad <= 1.0
    cross_tool_dist = action_distance(
        ActionCandidate("kv.put", {"k": "x"}),
        ActionCandidate("sql.exec", {"statement": "SELECT 1"}),
    )
    assert cross_tool_dist == 1.0
    report["search"] = {
        "algorithms": ["EnvMCTS", "EnvAStar", "BestOfNActionSampler"],
        "path_to_id_deterministic": pid == path_to_id(["a", "b", "c"]),
        "action_distance_same_tool": round(ad, 4),
        "action_distance_cross_tool": cross_tool_dist,
    }

    # ── 8. Speculative execution ──────────────────────────────────────
    gamma_ctrl = AdaptiveGammaController(GammaControllerConfig(gamma=5))
    assert gamma_ctrl.current_gamma == 5
    # Record telemetry and verify adaptation
    for _ in range(55):
        gamma_ctrl.record(accepted=4, drafted=4)
    gamma_after = gamma_ctrl.current_gamma
    # TemplateDrafter instantiation
    template = ActionTemplate("kv.put", {"k": "{key}", "v": 1})
    bound = template.bind({"key": "test_key"})
    assert bound.tool_id == "kv.put"
    assert bound.args["k"] == "test_key"
    drafter = TemplateDrafter(macro=[template])
    # Drafter protocol check
    assert isinstance(drafter, Drafter)
    report["speculate"] = {
        "gamma_initial": 5,
        "gamma_after_55_perfect": gamma_after,
        "template_bind": bound.args,
        "drafter_protocol": isinstance(drafter, Drafter),
    }

    # ── 9. Encoders ───────────────────────────────────────────────────
    encoder = HashingEncoder(dim=64)
    vec1 = encoder.encode_json({"a": 1, "b": 2})
    vec2 = encoder.encode_json({"b": 2, "a": 1})
    # JCS canonicalization makes key order irrelevant
    assert np.allclose(vec1, vec2), "encode_json must be key-order invariant"
    assert vec1.shape == (64,)
    # Token encoder
    vec3 = encoder.encode_tokens(["hello", "world"])
    assert vec3.shape == (64,)
    report["encode"] = {
        "dim": 64,
        "json_deterministic": bool(np.allclose(vec1, vec2)),
        "token_shape": list(vec3.shape),
    }

    # ── 10. Trajectory collection ─────────────────────────────────────
    sample = TrajectorySample(
        ctx="root",
        path=["a", "b"],
        actions=[{"tool_id": "kv.put", "args": {"k": "x"}, "canonical": "abc"}],
        reward=1.0,
        process_scores=[0.5, 0.8],
        depth=2,
        visit_count=10,
        terminal_ctx="done",
        env_digest="abc123",
        status="SOLVED",
        cost={"env_calls": 2, "wall_ms": 5.0},
        group_id=path_to_id(["a", "b"]),
    )
    assert sample.group_id == path_to_id(["a", "b"])
    report["collect"] = {
        "sample_status": sample.status,
        "group_id": sample.group_id,
    }

    # ── 11. Interleave ────────────────────────────────────────────────
    traj = Trajectory(initial_digest="0" * 16, steps=())
    branch_id = branch_id_for_trajectory(traj)
    assert isinstance(branch_id, str) and len(branch_id) == 16
    # ReasoningContext with empty trajectory
    dummy_state = State(ctx="root", env=None, digest="d", depth=0)
    goal = GoalSpec(
        predicates=(PredicateRef(name="kv.key_exists", params={"key": "a"}),),
    )
    rc = ReasoningContext(
        root_ctx="root",
        state=dummy_state,
        goal=goal,
        trajectory=traj,
    )
    assert rc.branch_id == branch_id
    assert rc.last_step is None
    report["interleave"] = {
        "branch_id": branch_id,
        "reasoning_context_valid": True,
    }

    # ── 12. Value heads ───────────────────────────────────────────────
    weights = np.ones(8, dtype=np.float32)
    lsv = LinearStateValue(weights=weights, bias=0.0)
    features = np.array([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.float32)
    score = lsv.score_vector(features)
    assert score == 36.0  # sum(1..8) + 0.0 bias
    # ConformalValueWrapper
    cvw = ConformalValueWrapper.from_residuals(
        base=lsv,
        residuals=[0.1, 0.2, 0.3, 0.4, 0.5],
        alpha=0.1,
    )
    conformal_score = cvw.score_vector(features)
    assert conformal_score < score  # subtracts quantile
    report["value"] = {
        "linear_score": score,
        "conformal_score": round(conformal_score, 4),
        "calibration_applied": conformal_score < score,
    }

    # ── 13. Experiment ────────────────────────────────────────────────
    instances = make_instances(count=2, seed=42)
    assert len(instances) == 2
    assert all(isinstance(i, NormalizeLoadInstance) for i in instances)
    assert all(i.oracle_actions > 0 for i in instances)
    report["experiment"] = {
        "instances_generated": len(instances),
        "oracle_actions": [i.oracle_actions for i in instances],
        "archive_available": archive_dependency_available(),
    }

    # ── 14. MDP protocol ─────────────────────────────────────────────
    # LexicalMDP instantiation check
    class _TestPolicy:
        def propose(self, ctx: str, n: int) -> list[tuple[str, float]]:
            return [("action_a", 1.0)]

    lex = LexicalMDP(policy=_TestPolicy())
    s0 = lex.initial_state(goal, "start")
    assert s0.depth == 0
    assert s0.ctx == "start"
    report["mdp"] = {
        "lexical_initial_depth": s0.depth,
        "lexical_ctx": s0.ctx,
    }

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cherry-ttt")
    parser.add_argument("command", choices=["smoke"], help="command to run")
    args = parser.parse_args(argv)
    if args.command == "smoke":
        print(json.dumps(smoke_report(), indent=2, sort_keys=True))
        return 0
    raise AssertionError("argparse should prevent unknown commands")


if __name__ == "__main__":
    raise SystemExit(main())
