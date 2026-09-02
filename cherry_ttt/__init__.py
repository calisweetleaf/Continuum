"""cherry_ttt — test-time tooling: environment-trajectory search on a typed contract surface.

Source: composed from the Cherry RL pipeline's inference_optimizations.py /
inference_protocols.py per CHERRY_TTT_PROPOSAL_v0.1.md.
Integrated: 2026-07-05
"""

from __future__ import annotations

__version__ = "0.2.0"

# ── Core contract surface ──────────────────────────────────────────────
from .core import (
    PHASE1_WEIGHTS,
    ActionCandidate,
    CanonicalizationError,
    CherryTTTError,
    ContractViolation,
    Cost,
    CostWeights,
    EffectClass,
    EffectViolation,
    EnvDigest,
    GoalSpec,
    LedgerViolation,
    Observation,
    PredicateRef,
    SnapshotError,
    SnapshotHandle,
    SoundnessError,
    TerminalStatus,
    Trajectory,
    TrajectoryStep,
    ValidationError,
    canonicalize,
    env_digest,
)

# ── Schema ─────────────────────────────────────────────────────────────
from .core.schema import ArgSpec, SchemaRegistry, ToolSchema, default_registry

# ── MDP protocols ──────────────────────────────────────────────────────
from .core.mdp import LexicalMDP, LexicalPolicy, State
from .core.contract_mdp import ContractMDP

# ── Attention ──────────────────────────────────────────────────────────
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

# ── Substrates ─────────────────────────────────────────────────────────
from .substrate import (
    CachedObservationPredictor,
    ExecutionSubstrate,
    ObservationPredictor,
    PredictionKey,
    RestoreReceipt,
    TransactionalSubstrateBase,
    verify_restore_soundness,
)
from .substrate.adapters import (
    ArchiveChannel,
    ArchiveEpisodeSubstrate,
    ArchiveEvidence,
    ArchiveEvidenceResult,
    ArchiveReadClient,
    EpisodeEvidenceLedger,
    FileSystemSubstrate,
    MemoryKVSubstrate,
    SQLiteSubstrate,
)

# ── Verify ─────────────────────────────────────────────────────────────
from .verify import (
    SATISFIED,
    Predicate,
    PredicateRegistry,
    ReadOnlyView,
    default_predicate_registry,
)

# ── Metrics ────────────────────────────────────────────────────────────
from .metrics import DensityMetrics, gamma_throughput

# ── Search ─────────────────────────────────────────────────────────────
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

# ── Trajectory collection ─────────────────────────────────────────────
from .collect import TrajectoryCollector, TrajectorySample

# ── Encoders ───────────────────────────────────────────────────────────
from .encode import (
    HashingEncoder,
    encode_goal,
    encode_observation,
    encode_registry,
    encode_state,
    encode_tool_schema,
    encode_trajectory,
)

# ── Interleave ─────────────────────────────────────────────────────────
from .interleave import (
    BranchEventLedger,
    ContextualActionProposer,
    InterleavedEvent,
    ReasoningContext,
    branch_id_for_trajectory,
)

# ── Value heads ────────────────────────────────────────────────────────
from .value import ConformalValueWrapper, LinearStateValue, StateValueLike

# ── Experiment ─────────────────────────────────────────────────────────
from .experiment import (
    ArchiveFixtureManifest,
    ArchivePilotInvariantError,
    ArchivePilotReport,
    ArchivePilotUnavailable,
    ArmResult,
    KSAProjectReadClient,
    NormalizeLoadInstance,
    archive_dependency_available,
    make_instances,
    run_archive_memory_pilot,
    run_arms,
)

__all__ = [
    # version
    "__version__",
    # core types
    "ActionCandidate", "CanonicalizationError", "CherryTTTError",
    "ContractViolation", "Cost", "CostWeights", "EffectClass", "EffectViolation",
    "EnvDigest", "GoalSpec", "LedgerViolation", "Observation", "PHASE1_WEIGHTS",
    "PredicateRef", "SnapshotError", "SnapshotHandle", "SoundnessError",
    "TerminalStatus", "Trajectory", "TrajectoryStep", "ValidationError",
    "canonicalize", "env_digest",
    # schema
    "ArgSpec", "SchemaRegistry", "ToolSchema", "default_registry",
    # mdp
    "ContractMDP", "LexicalMDP", "LexicalPolicy", "State",
    # attention
    "AttentionResult", "BiasQuery", "CandidateAttention", "CandidateMeta",
    "CandidateRecord", "PagedCandidateStore", "StoreStats",
    "build_structured_bias", "streaming_topk",
    # substrates
    "ArchiveChannel", "ArchiveEpisodeSubstrate", "ArchiveEvidence",
    "ArchiveEvidenceResult", "ArchiveReadClient", "CachedObservationPredictor",
    "EpisodeEvidenceLedger", "ExecutionSubstrate", "FileSystemSubstrate",
    "MemoryKVSubstrate", "ObservationPredictor", "PredictionKey",
    "RestoreReceipt", "SQLiteSubstrate", "TransactionalSubstrateBase",
    "verify_restore_soundness",
    # verify
    "SATISFIED", "Predicate", "PredicateRegistry", "ReadOnlyView",
    "default_predicate_registry",
    # metrics
    "DensityMetrics", "gamma_throughput",
    # search
    "BestOfNActionSampler", "BoNResult", "EnvAStar", "EnvAStarConfig",
    "EnvMCTS", "EnvMCTSConfig", "action_distance", "path_to_id",
    # speculate
    "ActionTemplate", "AdaptiveGammaController", "CommitReport", "Drafter",
    "GammaControllerConfig", "LatencyModel", "SpeculativeExecutor",
    "TabularDrafter", "TemplateDrafter",
    # collect
    "TrajectoryCollector", "TrajectorySample",
    # encode
    "HashingEncoder", "encode_goal", "encode_observation", "encode_registry",
    "encode_state", "encode_tool_schema", "encode_trajectory",
    # interleave
    "BranchEventLedger", "ContextualActionProposer", "InterleavedEvent",
    "ReasoningContext", "branch_id_for_trajectory",
    # value
    "ConformalValueWrapper", "LinearStateValue", "StateValueLike",
    # experiment
    "ArchiveFixtureManifest", "ArchivePilotInvariantError", "ArchivePilotReport",
    "ArchivePilotUnavailable", "ArmResult", "KSAProjectReadClient",
    "NormalizeLoadInstance", "archive_dependency_available", "make_instances",
    "run_archive_memory_pilot", "run_arms",
]
