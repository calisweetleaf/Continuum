#!/usr/bin/env python3
"""Reactive attention fabric for typed environmental streams.

Source: Morpheus flash_attn.py (2023-05-19, ID gpt40mni)
Integrated: 2026-08-24
Purpose: Sit around cognition, memory, environment, and capability execution as a
model-independent reactive attention fabric. The primitive is observe(stream events),
not attention(tokens).

Lineage. What survived from the Morpheus artifact: reactionary rather than fixed
attention; arbitrarily many simultaneous streams; heterogeneous information; hierarchical
multiscale processing; adaptive routing; persistent context distinct from persistent
memory; compute that follows what the system is actually receiving; long-lived operation.

What was corrected: "FlashAttention" was ordinary dense matmul of three always-computed
branches. "SubQuadraticAttention" chunked Q but still compared every query to every key,
so arithmetic remained O(N^2). DynamicRouter evaluated every expert and mixed afterward.
ModalityProcessor assumed a four-way model-owned text/vision/audio/other world.
PersistentMemory pickled tensors and treated active context as memory. Those functions
are implemented here under honest names, with hard conditional compute, staged candidate
narrowing, and a memory boundary that cannot be overwritten by a context summary.

This module does not import EXHUMA, LICHE, or CONTINUUM. Those systems may submit
streams or consume attended views through the protocols defined below.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol, Sequence

VECTOR_DIM = 16
DEFAULT_DENSE_LIMIT = 48
DEFAULT_BLOCK_SIZE = 8
DEFAULT_TOP_K = 32
DEFAULT_HIERARCHICAL_BLOCKS = 4
DEFAULT_CHUNK_SIZE = 16
DEFAULT_CHILD_CAP = 256
FILE_HEAD_BYTES = 4096
INTERRUPT_MARGIN = 1.25
RECENCY_HALF_LIFE_S = 30.0
KERNEL_SCALE = VECTOR_DIM ** -0.5
JSON_RECORD_VERSION = 1

TopologyName = Literal[
    "exact_dense",
    "block_streaming_exact",
    "linear_kernel",
    "sparse_top_k",
    "retrieval_first",
    "hierarchical_multiscale",
    "cross_stream",
    "temporal_event",
]


class AttentionFabricError(Exception):
    """Base domain error for the reactive attention fabric."""

    default_remediation = "Correct the stream, query, or resource input named in the error."

    def __init__(self, message: str, remediation: str | None = None) -> None:
        """Initialize a fabric error with operator remediation.

        Args:
            message: What failed.
            remediation: What to do next. Uses the class default when omitted.
        """
        self.message = message
        self.remediation = remediation or self.default_remediation
        super().__init__(message)


class InvalidStreamError(AttentionFabricError):
    """Raised when a stream event, adapter ingress, or query is malformed."""

    default_remediation = "Supply a typed stream with identity, domain, and a content reference."


class MemoryBoundaryError(AttentionFabricError):
    """Raised when durable memory cannot be read or written."""

    default_remediation = "Check the memory runtime path and that records carry provenance."


class AdapterError(AttentionFabricError):
    """Raised when a stream adapter cannot project native source material."""

    default_remediation = "Register an adapter for the domain or keep the native source as a reference."


class KernelError(AttentionFabricError):
    """Raised when an attention kernel cannot legally run on the candidate set."""

    default_remediation = "Reduce candidates, change topology, or provide routing vectors."


class TopologyError(AttentionFabricError):
    """Raised when topology selection or kernel registration is invalid."""

    default_remediation = "Register the named kernel or leave topology selection to pressure."


class AttentionRole(str, Enum):
    """Surviving Morpheus stream roles, now independent of model modality."""

    PRIMARY = "primary"
    AUXILIARY = "auxiliary"
    MONITORING = "monitoring"
    ANALYSIS = "analysis"
    INTEGRATION = "integration"
    TEMPORAL = "temporal"


class PersistencePolicy(str, Enum):
    """How long a stream's native source is expected to remain addressable."""

    EPHEMERAL = "ephemeral"
    SESSION = "session"
    DURABLE = "durable"
    IMMUTABLE = "immutable"


class FabricEventKind(str, Enum):
    """Mailbox verbs that can reallocate attention without a full restart."""

    STREAM_CREATE = "stream_create"
    STREAM_APPEND = "stream_append"
    PRIORITY_CHANGE = "priority_change"
    INVALIDATE_PROJECTION = "invalidate_projection"
    CROSS_STREAM_INTEGRATE = "cross_stream_integrate"
    WAKE_CONTEXT = "wake_context"
    REALLOCATE = "reallocate"


class StreamDormancy(str, Enum):
    """Whether a stream currently occupies active context."""

    ACTIVE = "active"
    DORMANT = "dormant"


@dataclass(frozen=True, slots=True)
class ContentRef:
    """Address into native source material. Never a replacement for the source."""

    scheme: str
    locator: str
    offset: int | None = None
    length: int | None = None
    checksum: str | None = None
    mime: str | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        """Serialize the reference without inlining native bytes.

        Returns:
            A JSON-safe address record.
        """
        return {
            "scheme": self.scheme,
            "locator": self.locator,
            "offset": self.offset,
            "length": self.length,
            "checksum": self.checksum,
            "mime": self.mime,
        }


@dataclass(frozen=True, slots=True)
class ProvenanceStamp:
    """Origin metadata that must survive projection and context assembly."""

    authority: str
    agent: str
    recorded_at: float
    lineage: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Serialize provenance for durable storage.

        Returns:
            A JSON-safe provenance record. Values are schema-shaped mixed types.
        """
        return {
            "authority": self.authority,
            "agent": self.agent,
            "recorded_at": self.recorded_at,
            "lineage": list(self.lineage),
        }


@dataclass(frozen=True, slots=True)
class NativeIngress:
    """Native environmental material before any attentional projection."""

    domain: str
    source: str
    locator: str
    payload: object | None = None  # opaque native in-process value; may be a file, row, or object
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One typed emission from an environmental or cognitive stream."""

    stream_id: str
    event_id: str
    source: str
    domain: str
    content_ref: ContentRef
    timestamp: float
    kind: FabricEventKind = FabricEventKind.STREAM_APPEND
    sequence_id: int | None = None
    temporal_extent: tuple[float, float] | None = None
    role: AttentionRole = AttentionRole.PRIMARY
    priority: float = 1.0
    importance: float = 1.0
    novelty: float = 0.0
    urgency: float = 0.0
    uncertainty: float = 0.0
    estimated_compute_cost: float = 1.0
    authority: float = 0.5
    persistence: PersistencePolicy = PersistencePolicy.SESSION
    provenance: ProvenanceStamp | None = None
    payload: object | None = None  # native source when already in-process; not a substitute for content_ref
    projection: tuple[float, ...] | None = None
    routing_vector: tuple[float, ...] | None = None

    def rank(self) -> float:
        """Return the hard-selection rank used before expensive kernels.

        Returns:
            A finite score. Higher means more worth computing.
        """
        return _rank(
            priority=self.priority,
            importance=self.importance,
            novelty=self.novelty,
            urgency=self.urgency,
            uncertainty=self.uncertainty,
            cost=self.estimated_compute_cost,
            authority=self.authority,
        )


@dataclass(frozen=True, slots=True)
class AttentionCandidate:
    """A selectable unit that may remain a reference until hard-selected."""

    candidate_id: str
    stream_id: str
    event_id: str
    domain: str
    timestamp: float
    content_ref: ContentRef
    routing_vector: tuple[float, ...]
    projection: tuple[float, ...] | None = None
    priority: float = 1.0
    importance: float = 1.0
    novelty: float = 0.0
    urgency: float = 0.0
    uncertainty: float = 0.0
    estimated_compute_cost: float = 1.0
    authority: float = 0.5

    def rank(self) -> float:
        """Return metadata rank for staged selection.

        Returns:
            A finite score used by hard routing.
        """
        return _rank(
            priority=self.priority,
            importance=self.importance,
            novelty=self.novelty,
            urgency=self.urgency,
            uncertainty=self.uncertainty,
            cost=self.estimated_compute_cost,
            authority=self.authority,
        )


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """Request sent across the memory boundary. Not an assembled context."""

    text: str | None = None
    stream_ids: tuple[str, ...] | None = None
    domains: tuple[str, ...] | None = None
    routing_vector: tuple[float, ...] | None = None
    min_importance: float = 0.0
    limit: int = 32


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Durable evidence returned by an external memory runtime."""

    record_id: str
    stream_id: str
    event_ids: tuple[str, ...]
    domain: str
    content_ref: ContentRef
    provenance: ProvenanceStamp
    importance: float
    created_at: float
    routing_vector: tuple[float, ...]
    projection: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class DurableRecord:
    """What the fabric may ask memory to keep. Never an active context blob."""

    record_id: str
    stream_id: str
    event_ids: tuple[str, ...]
    domain: str
    content_ref: ContentRef
    provenance: ProvenanceStamp
    importance: float
    routing_vector: tuple[float, ...]
    payload_inline: object | None = None  # JSON-shaped native excerpt only; source remains at content_ref


@dataclass(frozen=True, slots=True)
class MemoryReceipt:
    """Proof that durable memory accepted a record without consuming context."""

    record_id: str
    stored_at: float
    path: str


@dataclass(frozen=True, slots=True)
class ResourcePressure:
    """Live resource state that participates in topology and budget decisions."""

    cpu_load: float
    ram_used_ratio: float
    queue_depth: int
    latency_budget_ms: float
    candidate_population: int
    gpu_vram_used_ratio: float | None = None

    def severity(self) -> float:
        """Return a causal pressure scalar used by topology selection.

        Returns:
            A value in roughly [0, 2]. Higher means less compute may be spent.
        """
        gpu = self.gpu_vram_used_ratio if self.gpu_vram_used_ratio is not None else 0.0
        queue_term = min(self.queue_depth / 32.0, 1.0)
        latency_term = 0.0 if self.latency_budget_ms >= 100.0 else (100.0 - self.latency_budget_ms) / 100.0
        population_term = min(self.candidate_population / 100000.0, 1.0)
        return (
            0.25 * _clamp01(self.cpu_load)
            + 0.30 * _clamp01(self.ram_used_ratio)
            + 0.15 * _clamp01(gpu)
            + 0.15 * queue_term
            + 0.10 * latency_term
            + 0.05 * population_term
        )

    def active_budget(self) -> int:
        """Return how many candidates may become active under this pressure.

        Returns:
            A hard cap. Pressure shrinks this; it is not an after-the-fact metric.
        """
        base = DEFAULT_TOP_K
        if self.ram_used_ratio >= 0.90:
            base = min(base, 8)
        elif self.ram_used_ratio >= 0.75:
            base = min(base, 16)
        if self.latency_budget_ms <= 10.0:
            base = min(base, 8)
        elif self.latency_budget_ms <= 25.0:
            base = min(base, 16)
        if self.candidate_population >= 100000:
            base = min(base, 32)
        if self.queue_depth >= 16:
            base = min(base, 12)
        return max(1, base)


@dataclass(frozen=True, slots=True)
class WorkMeter:
    """Honest accounting of work performed versus work avoided."""

    pairs_scored: int = 0
    pairs_attended: int = 0
    linear_cells: int = 0
    blocks_expanded: int = 0
    blocks_skipped: int = 0
    candidates_considered: int = 0
    candidates_selected: int = 0
    kernels_evaluated: int = 0
    stages: tuple[str, ...] = ()

    def quadratic_pairs_possible(self, n_query: int, n_key: int) -> int:
        """Return the dense QK pair count that a false 'flash' kernel would pay.

        Args:
            n_query: Query count.
            n_key: Key count.

        Returns:
            n_query * n_key.
        """
        return n_query * n_key


@dataclass(frozen=True, slots=True)
class AttentionReceipt:
    """Trace from native streams to the attended view, including lossy steps."""

    mechanism: str
    complexity_class: str
    contributing_streams: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    candidates_before: int
    candidates_after: int
    work: WorkMeter
    confidence: float
    salience: float
    pressure_severity: float
    topology_reason: str
    rejected_topologies: tuple[str, ...]
    lossy_transforms: tuple[str, ...]
    elapsed_ms: float
    preempted: bool


@dataclass(frozen=True, slots=True)
class ContextSlot:
    """One active-context cell. Reconstructible from the native reference."""

    stream_id: str
    event_id: str
    content_ref: ContentRef
    domain: str
    projection: tuple[float, ...] | None
    salience: float


@dataclass(frozen=True, slots=True)
class ActiveContext:
    """Temporary projection assembled for present cognition. Not memory."""

    assembled_at: float
    pressure_severity: float
    slots: tuple[ContextSlot, ...]
    query_id: str
    reconstructible: bool = True


@dataclass(frozen=True, slots=True)
class KernelResult:
    """Output of a single selected attention kernel."""

    outputs: tuple[tuple[float, ...], ...]
    selected_indices: tuple[int, ...]
    weights: tuple[tuple[float, ...], ...]
    work: WorkMeter
    complexity_class: str
    lossy_transforms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AttendedCognitiveView:
    """The fabric's product: an attended view plus provenance, not a model token."""

    status: Literal["ok", "empty", "preempted", "pressure_reduced"]
    query_id: str
    focus_stream_ids: tuple[str, ...]
    selected_event_ids: tuple[str, ...]
    slots: tuple[ContextSlot, ...]
    integrated_projection: tuple[tuple[float, ...], ...]
    context: ActiveContext
    receipt: AttentionReceipt


@dataclass(frozen=True, slots=True)
class NumericAttendResult:
    """PyTorch-shaped numeric path that still carries a receipt."""

    status: Literal["ok", "empty"]
    output: tuple[tuple[float, ...], ...]
    receipt: AttentionReceipt


@dataclass(frozen=True, slots=True)
class PathStreamSummary:
    """Bounded filesystem-stream inspection used by the harness describe_path gate."""

    status: str
    normalized_path: str
    stream_id: str
    event_count: int
    domain: str
    mechanism: str
    candidates_before: int
    candidates_after: int


@dataclass
class AttendPlan:
    """Snapshot of routing decisions taken before the selected kernel runs."""

    kernel: AttentionKernel
    queries: list[tuple[float, ...]]
    filtered: list[AttentionCandidate]
    candidates_before: int
    budget: int
    topology: TopologyName
    reason: str
    rejected: tuple[str, ...]
    pressure: ResourcePressure
    started: float


@dataclass(frozen=True, slots=True)
class AttentionQuery:
    """An explicit ask against currently observed streams and/or supplied candidates."""

    query_id: str
    routing_vector: tuple[float, ...] | None = None
    stream_ids: tuple[str, ...] | None = None
    domains: tuple[str, ...] | None = None
    text: str | None = None
    latency_budget_ms: float = 100.0
    max_candidates: int | None = None
    topology: TopologyName | None = None
    pressure: ResourcePressure | None = None
    now: float | None = None
    include_memory: bool = False
    extra_candidates: tuple[AttentionCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ObserveResult:
    """Outcome of ingesting one event into the resident fabric."""

    status: Literal["buffered", "woke", "reallocated", "preempt_requested"]
    stream_id: str
    event_id: str
    dormant: bool
    pending_depth: int
    view: AttendedCognitiveView | None = None


@dataclass
class StreamState:
    """Mutable live state for one registered stream."""

    spec_id: str
    domain: str
    source: str
    role: AttentionRole
    persistence: PersistencePolicy
    priority: float
    dormancy: StreamDormancy
    events: deque[StreamEvent] = field(default_factory=deque)
    cached_projection: tuple[float, ...] | None = None
    cache_valid: bool = False
    last_event_id: str | None = None


class MemoryRuntime(Protocol):
    """Durable memory owned outside the fabric. Context must not be written here."""

    def recall(self, query: MemoryQuery) -> tuple[MemoryCandidate, ...]:
        """Return durable candidates without assembling active context.

        Args:
            query: Retrieval constraints and optional routing vector.

        Returns:
            Zero or more provenance-bearing memory candidates.
        """

    def remember(self, record: DurableRecord) -> MemoryReceipt:
        """Persist a record that still points at native source.

        Args:
            record: Durable evidence. Must not be an active-context summary.

        Returns:
            A storage receipt.
        """


class StreamAdapter(Protocol):
    """Transforms native source into typed attentional addresses without erasing it."""

    def supports(self, ingress: NativeIngress) -> bool:
        """Return whether this adapter can address the native ingress.

        Args:
            ingress: Native environmental material.

        Returns:
            True when this adapter should handle the ingress.
        """

    def ingest(self, ingress: NativeIngress) -> tuple[StreamEvent, ...]:
        """Emit stream events whose content_ref still names the native source.

        Args:
            ingress: Native environmental material.

        Returns:
            One or more events. Payload is optional; the reference is required.
        """


class AttentionKernel(Protocol):
    """One attention topology with an honest complexity class."""

    name: TopologyName
    complexity_class: str

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Run this kernel only. Callers must not mix sibling kernels afterward.

        Args:
            queries: Query vectors already chosen for this allocation.
            candidates: Hard-narrowed or still-full candidate set, depending on topology.
            abort: Returns True when a higher-value event has requested preemption.

        Returns:
            Kernel outputs, selected indices, and work accounting.
        """


def _clamp01(value: float) -> float:
    """Clamp a scalar into [0, 1]."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _rank(
    priority: float,
    importance: float,
    novelty: float,
    urgency: float,
    uncertainty: float,
    cost: float,
    authority: float,
) -> float:
    """Combine metadata into a hard-selection rank."""
    return (
        1.2 * priority
        + 1.4 * importance
        + 1.1 * novelty
        + 1.3 * urgency
        + 0.6 * authority
        - 0.8 * uncertainty
        - 0.5 * max(cost, 0.0)
    )


def _fit_dim(vector: Sequence[float], dim: int = VECTOR_DIM) -> tuple[float, ...]:
    """Pad or crop a vector to the fabric's routing dimension."""
    if len(vector) == dim:
        return tuple(float(x) for x in vector)
    if len(vector) > dim:
        return tuple(float(x) for x in vector[:dim])
    return tuple(float(x) for x in vector) + (0.0,) * (dim - len(vector))


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    """Dot product over the overlapping prefix."""
    return sum(a * b for a, b in zip(left, right))


def _l2(vector: Sequence[float]) -> float:
    """Euclidean norm."""
    return math.sqrt(sum(x * x for x in vector)) or 1e-9


def _normalize(vector: Sequence[float]) -> tuple[float, ...]:
    """L2-normalize a vector."""
    norm = _l2(vector)
    return tuple(x / norm for x in vector)


def _mean_vectors(vectors: Sequence[Sequence[float]], dim: int = VECTOR_DIM) -> tuple[float, ...]:
    """Mean of vectors, or zeros when empty."""
    if not vectors:
        return (0.0,) * dim
    acc = [0.0] * dim
    for vector in vectors:
        fitted = _fit_dim(vector, dim)
        for index, value in enumerate(fitted):
            acc[index] += value
    scale = 1.0 / len(vectors)
    return tuple(value * scale for value in acc)


def _softmax(values: Sequence[float]) -> tuple[float, ...]:
    """Numerically stable softmax."""
    if not values:
        return ()
    peak = max(values)
    exps = [math.exp(value - peak) for value in values]
    total = sum(exps) or 1e-9
    return tuple(value / total for value in exps)


def _hashed_projection(material: str, dim: int = VECTOR_DIM) -> tuple[float, ...]:
    """Cheap model-free projection from bytes/text. Not a replacement for the source."""
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    raw: list[float] = []
    cursor = 0
    while len(raw) < dim:
        chunk = digest if cursor == 0 else hashlib.sha256(digest + bytes([cursor])).digest()
        for byte in chunk:
            raw.append((byte / 127.5) - 1.0)
            if len(raw) >= dim:
                break
        cursor += 1
    return _normalize(raw[:dim])


def _metadata_vector(
    domain: str,
    priority: float,
    importance: float,
    novelty: float,
    urgency: float,
    uncertainty: float,
    cost: float,
    authority: float,
    timestamp: float,
    extra: str = "",
) -> tuple[float, ...]:
    """Build a routing vector from typed metadata when no embedding exists."""
    recency = math.tanh((time.time() - timestamp) / 3600.0)
    domain_bits = _hashed_projection(f"{domain}:{extra}", dim=8)
    core = _fit_dim(
        (
            _clamp01(priority),
            _clamp01(importance),
            _clamp01(novelty),
            _clamp01(urgency),
            _clamp01(uncertainty),
            _clamp01(cost / 10.0),
            _clamp01(authority),
            recency,
            *domain_bits,
        )
    )
    return _normalize(core)


def _candidate_key(candidate: AttentionCandidate) -> tuple[float, ...]:
    """Key vector used by kernels: projection when present, else routing vector."""
    if candidate.projection is not None:
        return _fit_dim(candidate.projection)
    return _fit_dim(candidate.routing_vector)


def _candidate_value(candidate: AttentionCandidate) -> tuple[float, ...]:
    """Value vector. Same as key unless a distinct projection exists."""
    return _candidate_key(candidate)


def _top_k_indices(scores: Sequence[float], k: int) -> tuple[int, ...]:
    """Return indices of the k largest scores. Hard selection, not a soft mix."""
    if k <= 0 or not scores:
        return ()
    order = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
    return tuple(order[: min(k, len(order))])


def _weighted_sum(weights: Sequence[float], vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    """Mix selected value vectors by attention weights."""
    if not weights or not vectors:
        return (0.0,) * VECTOR_DIM
    acc = [0.0] * VECTOR_DIM
    for weight, vector in zip(weights, vectors):
        fitted = _fit_dim(vector)
        for index, value in enumerate(fitted):
            acc[index] += weight * value
    return tuple(acc)


def _json_safe(value: object) -> bool:
    """Return True when a payload may be inlined into durable memory JSON."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_safe(item) for key, item in value.items())
    return False


def _event_to_candidate(event: StreamEvent) -> AttentionCandidate:
    """Project a live event into a selectable candidate without copying native bytes."""
    routing = event.routing_vector or _metadata_vector(
        domain=event.domain,
        priority=event.priority,
        importance=event.importance,
        novelty=event.novelty,
        urgency=event.urgency,
        uncertainty=event.uncertainty,
        cost=event.estimated_compute_cost,
        authority=event.authority,
        timestamp=event.timestamp,
        extra=event.content_ref.locator,
    )
    return AttentionCandidate(
        candidate_id=event.event_id,
        stream_id=event.stream_id,
        event_id=event.event_id,
        domain=event.domain,
        timestamp=event.timestamp,
        content_ref=event.content_ref,
        routing_vector=_fit_dim(routing),
        projection=_fit_dim(event.projection) if event.projection is not None else None,
        priority=event.priority,
        importance=event.importance,
        novelty=event.novelty,
        urgency=event.urgency,
        uncertainty=event.uncertainty,
        estimated_compute_cost=event.estimated_compute_cost,
        authority=event.authority,
    )


def _memory_to_candidate(item: MemoryCandidate) -> AttentionCandidate:
    """Address a durable memory record as an attention candidate."""
    return AttentionCandidate(
        candidate_id=item.record_id,
        stream_id=item.stream_id,
        event_id=item.event_ids[0] if item.event_ids else item.record_id,
        domain=item.domain,
        timestamp=item.created_at,
        content_ref=item.content_ref,
        routing_vector=_fit_dim(item.routing_vector),
        projection=_fit_dim(item.projection) if item.projection is not None else None,
        importance=item.importance,
        authority=0.8,
    )


def sample_resource_pressure(
    queue_depth: int = 0,
    latency_budget_ms: float = 100.0,
    candidate_population: int = 0,
) -> ResourcePressure:
    """Sample host pressure from procfs when present.

    Args:
        queue_depth: Current fabric mailbox depth.
        latency_budget_ms: Remaining latency budget for this allocation.
        candidate_population: Known candidate count at decision time.

    Returns:
        A pressure snapshot used causally by topology selection.
    """
    cpu_load = 0.0
    try:
        load1, _, _ = os.getloadavg()
        cpus = os.cpu_count() or 1
        cpu_load = load1 / float(cpus)
    except OSError:
        cpu_load = 0.0
    ram_used_ratio = _read_ram_used_ratio()
    gpu_ratio = _read_gpu_vram_ratio()
    return ResourcePressure(
        cpu_load=cpu_load,
        ram_used_ratio=ram_used_ratio,
        gpu_vram_used_ratio=gpu_ratio,
        queue_depth=queue_depth,
        latency_budget_ms=latency_budget_ms,
        candidate_population=candidate_population,
    )


def _read_ram_used_ratio() -> float:
    """Read MemAvailable/MemTotal from /proc/meminfo when the host provides it."""
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return 0.5
    total = 0.0
    available = 0.0
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total = float(line.split()[1])
        elif line.startswith("MemAvailable:"):
            available = float(line.split()[1])
    if total <= 0.0:
        return 0.5
    return _clamp01(1.0 - (available / total))


def _read_gpu_vram_ratio() -> float | None:
    """Read CUDA memory pressure only when torch.cuda is actually available."""
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    try:
        total = torch.cuda.get_device_properties(0).total_memory
        reserved = torch.cuda.memory_reserved(0)
    except (RuntimeError, AssertionError):
        return None
    if total <= 0:
        return None
    return _clamp01(reserved / float(total))


def _as_numeric_matrix(data: object) -> tuple[tuple[float, ...], ...]:
    """Convert nested numeric objects, including optional torch tensors, to tuples.

    Args:
        data: A sequence of sequences of floats, or a tensor-shaped runtime object.

    Returns:
        A tuple of routing/value vectors.

    Raises:
        InvalidStreamError: When the object cannot be read as vectors.
    """
    if data is None:
        raise InvalidStreamError("Numeric input is empty.")
    shape = getattr(data, "shape", None)
    detach = getattr(data, "detach", None)
    if shape is not None and callable(detach):
        try:
            cpu = detach().cpu().tolist()
        except (RuntimeError, TypeError, AttributeError) as error:
            raise InvalidStreamError(f"Tensor-shaped input could not be materialized: {error}") from error
        return _as_numeric_matrix(cpu)
    if isinstance(data, (list, tuple)):
        if not data:
            return ()
        first = data[0]
        if isinstance(first, (int, float)):
            return (_fit_dim([float(x) for x in data]),)
        rows: list[tuple[float, ...]] = []
        for row in data:
            if not isinstance(row, (list, tuple)):
                raise InvalidStreamError("Numeric matrix rows must themselves be sequences.")
            rows.append(_fit_dim([float(x) for x in row]))
        return tuple(rows)
    raise InvalidStreamError(f"Unsupported numeric input type: {type(data)!r}")


class FileStreamAdapter:
    """Addresses files and directories without ingesting them as token sequences."""

    def supports(self, ingress: NativeIngress) -> bool:
        """Accept filesystem, file, and directory domains, or existing paths.

        Args:
            ingress: Native environmental material.

        Returns:
            True when the locator is a path this adapter can stat.
        """
        if ingress.domain in {"sqlite", "database"}:
            return False
        suffix = Path(ingress.locator).suffix.lower()
        if suffix in {".sqlite", ".db", ".sqlite3"}:
            return False
        if ingress.domain in {"filesystem", "file", "directory"}:
            return True
        return Path(ingress.locator).exists()

    def ingest(self, ingress: NativeIngress) -> tuple[StreamEvent, ...]:
        """Emit file or directory events that keep the path as the source of truth.

        Args:
            ingress: Native path material.

        Returns:
            A directory event plus capped child file refs, or a single file event.

        Raises:
            AdapterError: When the path cannot be read as a filesystem object.
        """
        path = Path(ingress.locator).expanduser().resolve()
        if not path.exists():
            raise AdapterError(f"Filesystem locator does not exist: {path}")
        stamp = ProvenanceStamp(
            authority=ingress.source or "filesystem",
            agent="FileStreamAdapter",
            recorded_at=time.time(),
            lineage=(str(path),),
        )
        if path.is_dir():
            return self._ingest_directory(path, ingress, stamp)
        return (self._ingest_file(path, ingress, stamp, stream_id=_stream_id_for(path)),)

    def _ingest_directory(
        self,
        path: Path,
        ingress: NativeIngress,
        stamp: ProvenanceStamp,
    ) -> tuple[StreamEvent, ...]:
        """Emit a directory event and bounded child file references."""
        stream_id = _stream_id_for(path)
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise AdapterError(f"Could not list directory {path}: {error}") from error
        cap = DEFAULT_CHILD_CAP
        events = [
            StreamEvent(
                stream_id=stream_id,
                event_id=str(uuid.uuid4()),
                source=ingress.source or "filesystem",
                domain="directory",
                content_ref=ContentRef(scheme="dir", locator=str(path), length=len(children)),
                timestamp=path.stat().st_mtime,
                kind=FabricEventKind.STREAM_CREATE,
                importance=0.8,
                routing_vector=_metadata_vector(
                    "directory", 1.0, 0.8, 0.2, 0.0, 0.0, 0.2, 0.7, path.stat().st_mtime, path.name
                ),
                projection=_hashed_projection(f"dir:{path}:{len(children)}"),
                provenance=stamp,
            )
        ]
        for child in children[:cap]:
            if child.is_file():
                events.append(self._ingest_file(child, ingress, stamp, stream_id=stream_id))
            elif child.is_dir():
                child_stat = child.stat()
                events.append(
                    StreamEvent(
                        stream_id=stream_id,
                        event_id=str(uuid.uuid4()),
                        source=ingress.source or "filesystem",
                        domain="directory",
                        content_ref=ContentRef(scheme="dir", locator=str(child.resolve())),
                        timestamp=child_stat.st_mtime,
                        importance=0.4,
                        routing_vector=_metadata_vector(
                            "directory", 0.6, 0.4, 0.1, 0.0, 0.1, 0.2, 0.5, child_stat.st_mtime, child.name
                        ),
                        provenance=stamp,
                    )
                )
        return tuple(events)

    def _ingest_file(
        self,
        path: Path,
        ingress: NativeIngress,
        stamp: ProvenanceStamp,
        stream_id: str,
    ) -> StreamEvent:
        """Address one file. Head bytes may be hashed; the file is not tokenized."""
        stat = path.stat()
        checksum = None
        head = b""
        if stat.st_size <= FILE_HEAD_BYTES * 4:
            try:
                head = path.read_bytes()[:FILE_HEAD_BYTES]
                checksum = hashlib.sha256(head).hexdigest()
            except OSError as error:
                raise AdapterError(f"Could not read file head for {path}: {error}") from error
        material = f"{path.name}:{stat.st_size}:{stat.st_mtime}:{checksum or ''}"
        return StreamEvent(
            stream_id=stream_id,
            event_id=str(uuid.uuid4()),
            source=ingress.source or "filesystem",
            domain="file",
            content_ref=ContentRef(
                scheme="file",
                locator=str(path.resolve()),
                length=stat.st_size,
                checksum=checksum,
                mime=path.suffix.lstrip(".") or None,
            ),
            timestamp=stat.st_mtime,
            estimated_compute_cost=min(stat.st_size / 1_000_000.0, 10.0),
            routing_vector=_metadata_vector(
                "file", 1.0, 0.7, 0.2, 0.0, 0.1, min(stat.st_size / 1_000_000.0, 10.0), 0.6, stat.st_mtime, path.name
            ),
            projection=_hashed_projection(material if not head else f"{material}:{head[:64]!r}"),
            provenance=stamp,
        )


class SqliteStreamAdapter:
    """Addresses sqlite databases as row/table streams without tensorizing the DB."""

    def supports(self, ingress: NativeIngress) -> bool:
        """Accept sqlite/database domains or .sqlite/.db locators.

        Args:
            ingress: Native environmental material.

        Returns:
            True when this adapter should open the locator as sqlite.
        """
        if ingress.domain in {"sqlite", "database"}:
            return True
        suffix = Path(ingress.locator).suffix.lower()
        return suffix in {".sqlite", ".db", ".sqlite3"}

    def ingest(self, ingress: NativeIngress) -> tuple[StreamEvent, ...]:
        """Emit a database event plus one event per table, leaving rows as rowids.

        Args:
            ingress: Path to a sqlite file.

        Returns:
            Database and table events. Row payloads stay in sqlite.

        Raises:
            AdapterError: When the database cannot be opened.
        """
        path = Path(ingress.locator).expanduser().resolve()
        if not path.exists():
            raise AdapterError(f"SQLite locator does not exist: {path}")
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise AdapterError(f"SQLite open failed for {path}: {error}") from error
        try:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            events: list[StreamEvent] = []
            db_stream = _stream_id_for(path)
            stamp = ProvenanceStamp(
                authority=ingress.source or "sqlite",
                agent="SqliteStreamAdapter",
                recorded_at=time.time(),
                lineage=(str(path),),
            )
            events.append(
                StreamEvent(
                    stream_id=db_stream,
                    event_id=str(uuid.uuid4()),
                    source=ingress.source or "sqlite",
                    domain="sqlite",
                    content_ref=ContentRef(scheme="sqlite", locator=str(path)),
                    timestamp=path.stat().st_mtime,
                    kind=FabricEventKind.STREAM_CREATE,
                    importance=0.9,
                    routing_vector=_hashed_projection(f"sqlite:{path}:{len(tables)}"),
                    provenance=stamp,
                )
            )
            for table in tables:
                count_row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                count = int(count_row[0]) if count_row else 0
                events.append(
                    StreamEvent(
                        stream_id=db_stream,
                        event_id=str(uuid.uuid4()),
                        source=ingress.source or "sqlite",
                        domain="sqlite",
                        content_ref=ContentRef(scheme="sqlite", locator=f"{path}#{table}", length=count),
                        timestamp=path.stat().st_mtime,
                        sequence_id=count,
                        importance=0.6,
                        estimated_compute_cost=min(count / 1000.0, 10.0),
                        routing_vector=_metadata_vector(
                            "sqlite", 0.8, 0.6, 0.1, 0.0, 0.1, min(count / 1000.0, 10.0), 0.7, path.stat().st_mtime, table
                        ),
                        provenance=stamp,
                    )
                )
            return tuple(events)
        except sqlite3.Error as error:
            raise AdapterError(f"SQLite catalog read failed for {path}: {error}") from error
        finally:
            connection.close()

    def iter_row_candidates(self, db_path: str, table: str, limit: int | None = None) -> tuple[AttentionCandidate, ...]:
        """Yield row candidates as sqlite rowid references without loading blobs.

        Args:
            db_path: Filesystem path to the database.
            table: Table name already known to exist.
            limit: Optional hard cap on emitted rows.

        Returns:
            Candidates whose content_ref locators are path#table/rowid.

        Raises:
            AdapterError: When the table cannot be scanned.
        """
        path = Path(db_path).expanduser().resolve()
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise AdapterError(f"SQLite open failed for {path}: {error}") from error
        candidates: list[AttentionCandidate] = []
        try:
            sql = f'SELECT rowid, * FROM "{table}"'
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
            cursor = connection.execute(sql)
            column_names = [item[0] for item in cursor.description]
            for row in cursor:
                mapping = {column_names[index]: row[index] for index in range(len(column_names))}
                rowid = int(mapping.get("rowid") or mapping.get("id") or 0)
                text_parts = [f"{key}={value}" for key, value in mapping.items() if isinstance(value, (str, int, float))]
                locator = f"{path}#{table}/{rowid}"
                routing = _hashed_projection("|".join(text_parts[:12]))
                candidates.append(
                    AttentionCandidate(
                        candidate_id=f"{table}:{rowid}",
                        stream_id=_stream_id_for(path),
                        event_id=f"{table}:{rowid}",
                        domain="sqlite",
                        timestamp=path.stat().st_mtime,
                        content_ref=ContentRef(scheme="sqlite", locator=locator),
                        routing_vector=routing,
                        importance=0.5,
                    )
                )
        except sqlite3.Error as error:
            raise AdapterError(f"SQLite row scan failed for {path}#{table}: {error}") from error
        finally:
            connection.close()
        return tuple(candidates)


class StructuredObjectAdapter:
    """Addresses in-process structured objects, tool results, receipts, and diffs."""

    SUPPORTED = frozenset(
        {
            "structured_object",
            "tool_schema",
            "tool_result",
            "execution_receipt",
            "code",
            "diff",
            "text",
            "memory",
            "trajectory",
            "branch",
            "sensor",
            "model_activation",
            "model_output",
            "process",
            "terminal",
            "network_event",
            "custom",
        }
    )

    def supports(self, ingress: NativeIngress) -> bool:
        """Accept typed object domains, including user-defined custom streams.

        Args:
            ingress: Native environmental material.

        Returns:
            True when the domain is an in-process structured surface.
        """
        return ingress.domain in self.SUPPORTED or ingress.payload is not None

    def ingest(self, ingress: NativeIngress) -> tuple[StreamEvent, ...]:
        """Keep the object as payload when present and always emit a content_ref.

        Args:
            ingress: Native object or text material.

        Returns:
            A single stream event addressing the object.
        """
        rendered = _render_payload(ingress.payload)
        locator = ingress.locator or f"object:{uuid.uuid4()}"
        checksum = hashlib.sha256(rendered.encode("utf-8")).hexdigest() if rendered else None
        projection = _hashed_projection(rendered or locator)
        return (
            StreamEvent(
                stream_id=_stream_id_for(locator),
                event_id=str(uuid.uuid4()),
                source=ingress.source or ingress.domain,
                domain=ingress.domain or "structured_object",
                content_ref=ContentRef(scheme="object", locator=locator, checksum=checksum),
                timestamp=time.time(),
                payload=ingress.payload,
                projection=projection,
                routing_vector=projection,
                provenance=ProvenanceStamp(
                    authority=ingress.source or ingress.domain,
                    agent="StructuredObjectAdapter",
                    recorded_at=time.time(),
                    lineage=(locator,),
                ),
            ),
        )


def _render_payload(payload: object | None) -> str:
    """Render a native payload to text for hashing without claiming to be the source."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload[:8000]
    if _json_safe(payload):
        return json.dumps(payload, sort_keys=True, default=str)[:8000]
    return repr(payload)[:8000]


def _stream_id_for(path: Path | str) -> str:
    """Derive a stable stream id from a locator."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


class SqliteDurableMemory:
    """SQLite-backed memory runtime. Stores records and refs, never active context."""

    def __init__(self, db_path: str) -> None:
        """Open or create a provenance-preserving memory database.

        Args:
            db_path: Filesystem path to the fabric-owned sqlite file.
        """
        self._path = str(Path(db_path).expanduser().resolve())
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS durable_records (
                    record_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    importance REAL NOT NULL,
                    content_ref_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    event_ids_json TEXT NOT NULL,
                    routing_json TEXT NOT NULL,
                    payload_json TEXT,
                    record_version INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_durable_stream ON durable_records(stream_id, importance DESC)"
            )
            connection.commit()
        except sqlite3.Error as error:
            raise MemoryBoundaryError(f"Memory schema init failed: {error}") from error
        finally:
            connection.close()

    def remember(self, record: DurableRecord) -> MemoryReceipt:
        """Persist a record that still points at native source.

        Args:
            record: Durable evidence. Inline payload must be JSON-safe or omitted.

        Returns:
            A storage receipt.

        Raises:
            MemoryBoundaryError: When the write fails or payload is not JSON-safe.
        """
        if record.payload_inline is not None and not _json_safe(record.payload_inline):
            raise MemoryBoundaryError(
                "Durable memory refuses non-JSON payloads; store a content_ref instead of inlining native bytes."
            )
        created_at = time.time()
        try:
            connection = sqlite3.connect(self._path)
            connection.execute(
                """
                INSERT OR REPLACE INTO durable_records (
                    record_id, stream_id, domain, created_at, importance,
                    content_ref_json, provenance_json, event_ids_json, routing_json,
                    payload_json, record_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.stream_id,
                    record.domain,
                    created_at,
                    record.importance,
                    json.dumps(record.content_ref.as_dict()),
                    json.dumps(record.provenance.as_dict()),
                    json.dumps(list(record.event_ids)),
                    json.dumps(list(record.routing_vector)),
                    json.dumps(record.payload_inline) if record.payload_inline is not None else None,
                    JSON_RECORD_VERSION,
                ),
            )
            connection.commit()
        except sqlite3.Error as error:
            raise MemoryBoundaryError(f"Memory write failed: {error}") from error
        finally:
            connection.close()
        return MemoryReceipt(record_id=record.record_id, stored_at=created_at, path=self._path)

    def recall(self, query: MemoryQuery) -> tuple[MemoryCandidate, ...]:
        """Return durable candidates without assembling active context.

        Args:
            query: Retrieval constraints and optional routing vector.

        Returns:
            Provenance-bearing memory candidates, ranked then optionally vector-narrowed.

        Raises:
            MemoryBoundaryError: When the database cannot be read.
        """
        clauses = ["importance >= ?"]
        params: list[object] = [query.min_importance]  # query parameters are schema-shaped mixed SQL bind values
        if query.stream_ids:
            placeholders = ",".join("?" for _ in query.stream_ids)
            clauses.append(f"stream_id IN ({placeholders})")
            params.extend(query.stream_ids)
        if query.domains:
            placeholders = ",".join("?" for _ in query.domains)
            clauses.append(f"domain IN ({placeholders})")
            params.extend(query.domains)
        sql = (
            "SELECT record_id, stream_id, domain, created_at, importance, content_ref_json, "
            "provenance_json, event_ids_json, routing_json FROM durable_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY importance DESC, created_at DESC LIMIT ?"
        )
        fetch_limit = max(query.limit * 4, query.limit)
        params.append(fetch_limit)
        try:
            connection = sqlite3.connect(self._path)
            rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise MemoryBoundaryError(f"Memory recall failed: {error}") from error
        finally:
            connection.close()
        candidates: list[MemoryCandidate] = []
        for row in rows:
            content = json.loads(row[5])
            provenance = json.loads(row[6])
            event_ids = tuple(json.loads(row[7]))
            routing = tuple(float(x) for x in json.loads(row[8]))
            candidates.append(
                MemoryCandidate(
                    record_id=str(row[0]),
                    stream_id=str(row[1]),
                    event_ids=event_ids,
                    domain=str(row[2]),
                    content_ref=ContentRef(
                        scheme=str(content["scheme"]),
                        locator=str(content["locator"]),
                        offset=content.get("offset"),
                        length=content.get("length"),
                        checksum=content.get("checksum"),
                        mime=content.get("mime"),
                    ),
                    provenance=ProvenanceStamp(
                        authority=str(provenance["authority"]),
                        agent=str(provenance["agent"]),
                        recorded_at=float(provenance["recorded_at"]),
                        lineage=tuple(provenance.get("lineage") or ()),
                    ),
                    importance=float(row[4]),
                    created_at=float(row[3]),
                    routing_vector=_fit_dim(routing),
                )
            )
        if query.routing_vector is None:
            return tuple(candidates[: query.limit])
        scored = sorted(
            candidates,
            key=lambda item: _dot(_fit_dim(query.routing_vector or ()), item.routing_vector),
            reverse=True,
        )
        return tuple(scored[: query.limit])


class ExactDenseAttention:
    """Exact softmax attention. Arithmetic is O(N^2). Use only on small sets."""

    name: TopologyName = "exact_dense"
    complexity_class = "quadratic_arithmetic_full_materialization"

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Compare every query to every remaining key.

        Args:
            queries: Query vectors.
            candidates: Candidate keys/values. Must already be small.
            abort: Preemption check between query rows.

        Returns:
            Dense attention outputs over all supplied candidates.
        """
        if abort():
            return _empty_kernel(self.complexity_class, ("preempted_before_dense",))
        keys = [_candidate_key(item) for item in candidates]
        values = [_candidate_value(item) for item in candidates]
        outputs: list[tuple[float, ...]] = []
        weights_out: list[tuple[float, ...]] = []
        pairs = 0
        for query in queries:
            if abort():
                break
            scores = [KERNEL_SCALE * _dot(query, key) for key in keys]
            pairs += len(keys)
            weights = _softmax(scores)
            outputs.append(_weighted_sum(weights, values))
            weights_out.append(weights)
        selected = tuple(range(len(candidates)))
        work = WorkMeter(
            pairs_scored=pairs,
            pairs_attended=pairs,
            candidates_considered=len(candidates),
            candidates_selected=len(candidates),
            kernels_evaluated=1,
            stages=("exact_dense",),
        )
        return KernelResult(
            outputs=tuple(outputs),
            selected_indices=selected,
            weights=tuple(weights_out),
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=(),
        )


class BlockStreamingExactAttention:
    """Exact attention with chunked Q. Arithmetic remains O(N^2); working memory is O(chunk * N).

    This is what the historical SubQuadraticAttention actually computed. The name no
    longer claims subquadratic arithmetic.
    """

    name: TopologyName = "block_streaming_exact"
    complexity_class = "quadratic_arithmetic_chunked_memory"

    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        """Configure the query chunk used to bound working memory.

        Args:
            chunk_size: Number of queries materialized against all keys at once.
        """
        self.chunk_size = max(1, chunk_size)

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Stream query chunks against the full key set.

        Args:
            queries: Query vectors.
            candidates: Full remaining candidate set.
            abort: Checked between chunks so a high-value event can preempt.

        Returns:
            Exact outputs equivalent to dense attention when not preempted.
        """
        keys = [_candidate_key(item) for item in candidates]
        values = [_candidate_value(item) for item in candidates]
        outputs: list[tuple[float, ...]] = []
        weights_out: list[tuple[float, ...]] = []
        pairs = 0
        for start in range(0, len(queries), self.chunk_size):
            if abort():
                break
            chunk = queries[start : start + self.chunk_size]
            for query in chunk:
                scores = [KERNEL_SCALE * _dot(query, key) for key in keys]
                pairs += len(keys)
                weights = _softmax(scores)
                outputs.append(_weighted_sum(weights, values))
                weights_out.append(weights)
        work = WorkMeter(
            pairs_scored=pairs,
            pairs_attended=pairs,
            candidates_considered=len(candidates),
            candidates_selected=len(candidates),
            kernels_evaluated=1,
            stages=("block_streaming_exact",),
        )
        return KernelResult(
            outputs=tuple(outputs),
            selected_indices=tuple(range(len(candidates))),
            weights=tuple(weights_out),
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=("query_chunked_materialization",),
        )


class LinearKernelAttention:
    """ELU+1 feature-map attention. Arithmetic is O((N+Q) d^2), linear in sequence length.

    This is a positive-feature linear kernel, not a GPU FlashAttention kernel and not
    Performer FAVOR+.
    """

    name: TopologyName = "linear_kernel"
    complexity_class = "linear_in_n_quadratic_in_d"

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Accumulate phi(K)^T V once, then query it with phi(Q).

        Args:
            queries: Query vectors.
            candidates: Candidate keys/values.
            abort: Checked after the K accumulation pass.

        Returns:
            Linear-kernel outputs over all candidates, without forming an N x N matrix.
        """
        dim = VECTOR_DIM
        kv = [[0.0] * dim for _ in range(dim)]
        normalizer = [0.0] * dim
        cells = 0
        for candidate in candidates:
            if abort():
                break
            phi_key = _phi_elu(_candidate_key(candidate))
            value = _candidate_value(candidate)
            for row in range(dim):
                normalizer[row] += phi_key[row]
                key_scale = phi_key[row]
                row_acc = kv[row]
                for col in range(dim):
                    row_acc[col] += key_scale * value[col]
                    cells += 1
        if abort():
            return _empty_kernel(self.complexity_class, ("preempted_after_kv_accumulation",))
        outputs: list[tuple[float, ...]] = []
        for query in queries:
            phi_query = _phi_elu(_fit_dim(query))
            numeric = [0.0] * dim
            denom = 0.0
            for row in range(dim):
                denom += phi_query[row] * normalizer[row]
                scale = phi_query[row]
                kv_row = kv[row]
                for col in range(dim):
                    numeric[col] += scale * kv_row[col]
                    cells += 1
            denom = denom if abs(denom) > 1e-9 else 1e-9
            outputs.append(tuple(value / denom for value in numeric))
        work = WorkMeter(
            pairs_scored=len(candidates),
            pairs_attended=len(candidates) + len(queries),
            linear_cells=cells,
            candidates_considered=len(candidates),
            candidates_selected=len(candidates),
            kernels_evaluated=1,
            stages=("linear_kernel_elu_plus_one",),
        )
        return KernelResult(
            outputs=tuple(outputs),
            selected_indices=tuple(range(len(candidates))),
            weights=(),
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=("elu_plus_one_feature_map",),
        )


def _phi_elu(vector: Sequence[float]) -> tuple[float, ...]:
    """Positive ELU+1 feature map used by linear kernel attention."""
    mapped: list[float] = []
    for value in vector:
        if value >= 0.0:
            mapped.append(value + 1.0)
        else:
            mapped.append(math.exp(value))
    return tuple(mapped)


class SparseTopKAttention:
    """Hard top-k routing then exact attention on the survivors. Skips unselected keys."""

    name: TopologyName = "sparse_top_k"
    complexity_class = "linear_score_plus_qk_on_k"

    def __init__(self, top_k: int = DEFAULT_TOP_K) -> None:
        """Configure the hard selection width.

        Args:
            top_k: Maximum keys that receive exact attention.
        """
        self.top_k = max(1, top_k)

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Score all keys cheaply, keep k, then run exact attention only on those k.

        Args:
            queries: Query vectors.
            candidates: Possibly large candidate set.
            abort: Checked after scoring.

        Returns:
            Exact attention over the hard-selected subset.
        """
        if not candidates:
            return _empty_kernel(self.complexity_class, ())
        centroid = _mean_vectors(queries)
        scores = [
            KERNEL_SCALE * _dot(centroid, _candidate_key(item)) + 0.15 * item.rank()
            for item in candidates
        ]
        if abort():
            return _empty_kernel(self.complexity_class, ("preempted_after_scoring",))
        selected = _top_k_indices(scores, min(self.top_k, len(candidates)))
        subset = [candidates[index] for index in selected]
        dense = ExactDenseAttention().attend(queries, subset, abort)
        work = WorkMeter(
            pairs_scored=len(candidates),
            pairs_attended=dense.work.pairs_attended,
            candidates_considered=len(candidates),
            candidates_selected=len(subset),
            kernels_evaluated=1,
            stages=("hard_top_k", "exact_dense_on_survivors"),
        )
        return KernelResult(
            outputs=dense.outputs,
            selected_indices=selected,
            weights=dense.weights,
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=("hard_top_k_exclusion",),
        )


class RetrievalFirstAttention:
    """Metadata filter, then hard top-k, then exact attention. Required for huge stores."""

    name: TopologyName = "retrieval_first"
    complexity_class = "metadata_filter_then_topk_then_exact"

    def __init__(self, top_k: int = DEFAULT_TOP_K, min_rank: float = -1e9) -> None:
        """Configure staged narrowing.

        Args:
            top_k: Survivors that receive exact attention.
            min_rank: Metadata rank floor applied before vector scoring.
        """
        self.top_k = max(1, top_k)
        self.min_rank = min_rank

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Drop candidates on metadata before any pairwise neural attention.

        Args:
            queries: Query vectors.
            candidates: Large external candidate space.
            abort: Checked after the metadata pass.

        Returns:
            Exact attention over a hard-narrowed subset.
        """
        filtered = [item for item in candidates if item.rank() >= self.min_rank]
        if abort():
            return _empty_kernel(self.complexity_class, ("preempted_after_metadata_filter",))
        sparse = SparseTopKAttention(top_k=self.top_k)
        result = sparse.attend(queries, filtered, abort)
        work = WorkMeter(
            pairs_scored=result.work.pairs_scored,
            pairs_attended=result.work.pairs_attended,
            candidates_considered=len(candidates),
            candidates_selected=result.work.candidates_selected,
            kernels_evaluated=1,
            stages=("metadata_filter",) + result.work.stages,
        )
        return KernelResult(
            outputs=result.outputs,
            selected_indices=result.selected_indices,
            weights=result.weights,
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=("metadata_hard_filter",) + result.lossy_transforms,
        )


class HierarchicalMultiscaleAttention:
    """Coarse block attention, then fine attention only inside high-mass blocks."""

    name: TopologyName = "hierarchical_multiscale"
    complexity_class = "coarse_to_fine_skipped_blocks"

    def __init__(self, block_size: int = DEFAULT_BLOCK_SIZE, keep_blocks: int = DEFAULT_HIERARCHICAL_BLOCKS) -> None:
        """Configure the multiscale partition.

        Args:
            block_size: Events per coarse block.
            keep_blocks: How many blocks are expanded at fine scale.
        """
        self.block_size = max(2, block_size)
        self.keep_blocks = max(1, keep_blocks)

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Attend to block summaries, then expand only the winning blocks.

        Args:
            queries: Query vectors.
            candidates: Ordered candidate set, treated as a 1D hierarchy.
            abort: Checked after the coarse pass.

        Returns:
            Fine attention over expanded blocks only. Unselected blocks are not computed.
        """
        if not candidates:
            return _empty_kernel(self.complexity_class, ())
        blocks: list[list[AttentionCandidate]] = []
        for start in range(0, len(candidates), self.block_size):
            blocks.append(list(candidates[start : start + self.block_size]))
        summaries: list[AttentionCandidate] = []
        for index, block in enumerate(blocks):
            summary_vec = _mean_vectors([_candidate_key(item) for item in block])
            peak = max(item.rank() for item in block)
            representative = block[0]
            summaries.append(
                AttentionCandidate(
                    candidate_id=f"block:{index}",
                    stream_id=representative.stream_id,
                    event_id=representative.event_id,
                    domain=representative.domain,
                    timestamp=representative.timestamp,
                    content_ref=representative.content_ref,
                    routing_vector=summary_vec,
                    projection=summary_vec,
                    importance=peak,
                    priority=peak,
                )
            )
        coarse = ExactDenseAttention().attend(queries, summaries, abort)
        if abort():
            return _empty_kernel(self.complexity_class, ("preempted_after_coarse",))
        block_mass = [0.0] * len(blocks)
        for weight_row in coarse.weights:
            for index, weight in enumerate(weight_row):
                block_mass[index] += weight
        keep = _top_k_indices(block_mass, min(self.keep_blocks, len(blocks)))
        keep_set = set(keep)
        expanded: list[AttentionCandidate] = []
        selected_original: list[int] = []
        for block_index, block in enumerate(blocks):
            if block_index in keep_set:
                start = block_index * self.block_size
                expanded.extend(block)
                selected_original.extend(range(start, start + len(block)))
        skipped = len(blocks) - len(keep_set)
        fine = ExactDenseAttention().attend(queries, expanded, abort)
        work = WorkMeter(
            pairs_scored=coarse.work.pairs_attended + fine.work.pairs_attended,
            pairs_attended=coarse.work.pairs_attended + fine.work.pairs_attended,
            blocks_expanded=len(keep_set),
            blocks_skipped=skipped,
            candidates_considered=len(candidates),
            candidates_selected=len(expanded),
            kernels_evaluated=1,
            stages=("coarse_block_summaries", "hard_block_select", "fine_on_expanded_blocks"),
        )
        return KernelResult(
            outputs=fine.outputs,
            selected_indices=tuple(selected_original),
            weights=fine.weights,
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=("unexpanded_blocks_never_computed",),
        )


class CrossStreamAttention:
    """Attend among stream summaries, then expand only streams that survive hard routing."""

    name: TopologyName = "cross_stream"
    complexity_class = "stream_summary_then_expand_winners"

    def __init__(self, keep_streams: int = 3, inner_k: int = 16) -> None:
        """Configure cross-stream hard routing.

        Args:
            keep_streams: Streams expanded into event-level attention.
            inner_k: Per-stream event cap after expansion.
        """
        self.keep_streams = max(1, keep_streams)
        self.inner_k = max(1, inner_k)

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Drop whole streams before paying event-level attention inside them.

        Args:
            queries: Query vectors.
            candidates: Mixed-stream candidate set.
            abort: Checked after stream-level attention.

        Returns:
            Event-level attention over surviving streams only.
        """
        grouped: dict[str, list[tuple[int, AttentionCandidate]]] = defaultdict(list)
        for index, candidate in enumerate(candidates):
            grouped[candidate.stream_id].append((index, candidate))
        summaries: list[AttentionCandidate] = []
        stream_ids: list[str] = []
        for stream_id, items in grouped.items():
            vectors = [_candidate_key(item) for _, item in items]
            peak = max(item.rank() for _, item in items)
            representative = items[0][1]
            summaries.append(
                AttentionCandidate(
                    candidate_id=f"stream:{stream_id}",
                    stream_id=stream_id,
                    event_id=representative.event_id,
                    domain=representative.domain,
                    timestamp=representative.timestamp,
                    content_ref=representative.content_ref,
                    routing_vector=_mean_vectors(vectors),
                    importance=peak,
                    priority=peak,
                )
            )
            stream_ids.append(stream_id)
        coarse = ExactDenseAttention().attend(queries, summaries, abort)
        if abort():
            return _empty_kernel(self.complexity_class, ("preempted_after_stream_summaries",))
        mass = [0.0] * len(summaries)
        for weight_row in coarse.weights:
            for index, weight in enumerate(weight_row):
                mass[index] += weight
        keep = _top_k_indices(mass, min(self.keep_streams, len(summaries)))
        keep_ids = {stream_ids[index] for index in keep}
        expanded: list[AttentionCandidate] = []
        selected_indices: list[int] = []
        for stream_id, items in grouped.items():
            if stream_id not in keep_ids:
                continue
            ranked_items = sorted(items, key=lambda pair: pair[1].rank(), reverse=True)[: self.inner_k]
            for original_index, candidate in ranked_items:
                selected_indices.append(original_index)
                expanded.append(candidate)
        fine = ExactDenseAttention().attend(queries, expanded, abort)
        skipped_streams = len(grouped) - len(keep_ids)
        work = WorkMeter(
            pairs_scored=coarse.work.pairs_attended + fine.work.pairs_attended,
            pairs_attended=coarse.work.pairs_attended + fine.work.pairs_attended,
            blocks_expanded=len(keep_ids),
            blocks_skipped=skipped_streams,
            candidates_considered=len(candidates),
            candidates_selected=len(expanded),
            kernels_evaluated=1,
            stages=("cross_stream_summaries", "hard_stream_select", "exact_on_winner_streams"),
        )
        return KernelResult(
            outputs=fine.outputs,
            selected_indices=tuple(selected_indices),
            weights=fine.weights,
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=("unselected_streams_never_expanded",),
        )


class TemporalEventAttention:
    """Recency- and urgency-weighted hard selection over an event timeline."""

    name: TopologyName = "temporal_event"
    complexity_class = "temporal_decay_then_topk_exact"

    def __init__(self, top_k: int = DEFAULT_TOP_K, half_life_s: float = RECENCY_HALF_LIFE_S) -> None:
        """Configure temporal decay.

        Args:
            top_k: Events that receive exact attention.
            half_life_s: Recency half-life in seconds.
        """
        self.top_k = max(1, top_k)
        self.half_life_s = max(1e-6, half_life_s)

    def attend(
        self,
        queries: Sequence[tuple[float, ...]],
        candidates: Sequence[AttentionCandidate],
        abort: Callable[[], bool],
    ) -> KernelResult:
        """Score by recency, urgency, and novelty, then exact-attend the winners.

        Args:
            queries: Query vectors.
            candidates: Timestamped events, possibly from different streams.
            abort: Checked after temporal scoring.

        Returns:
            Exact attention over temporally selected events.
        """
        now = time.time()
        lam = math.log(2.0) / self.half_life_s
        scores = []
        for item in candidates:
            recency = math.exp(-lam * max(0.0, now - item.timestamp))
            scores.append(recency * (1.0 + item.urgency) * (1.0 + item.novelty) * (0.5 + item.importance))
        if abort():
            return _empty_kernel(self.complexity_class, ("preempted_after_temporal_score",))
        selected = _top_k_indices(scores, min(self.top_k, len(candidates)))
        subset = [candidates[index] for index in selected]
        dense = ExactDenseAttention().attend(queries, subset, abort)
        work = WorkMeter(
            pairs_scored=len(candidates),
            pairs_attended=dense.work.pairs_attended,
            candidates_considered=len(candidates),
            candidates_selected=len(subset),
            kernels_evaluated=1,
            stages=("temporal_decay_score", "hard_top_k", "exact_on_temporal_survivors"),
        )
        return KernelResult(
            outputs=dense.outputs,
            selected_indices=selected,
            weights=dense.weights,
            work=work,
            complexity_class=self.complexity_class,
            lossy_transforms=("temporal_hard_exclusion",),
        )


def _empty_kernel(complexity_class: str, lossy: tuple[str, ...]) -> KernelResult:
    """Return a preempted or empty kernel result without inventing attention mass."""
    return KernelResult(
        outputs=(),
        selected_indices=(),
        weights=(),
        work=WorkMeter(kernels_evaluated=1, stages=("aborted",)),
        complexity_class=complexity_class,
        lossy_transforms=lossy,
    )


class TopologySelector:
    """Hard topology choice. Exactly one kernel runs; siblings are not evaluated."""

    def select(
        self,
        candidate_count: int,
        stream_count: int,
        pressure: ResourcePressure,
        forced: TopologyName | None = None,
    ) -> tuple[TopologyName, str, tuple[str, ...]]:
        """Choose one topology from pressure, population, and stream cardinality.

        Args:
            candidate_count: Remaining candidates after metadata collection.
            stream_count: Distinct stream ids in that set.
            pressure: Live resource pressure. This changes the decision.
            forced: Optional explicit topology for tests or callers.

        Returns:
            Selected name, reason, and the topologies that were considered but not run.
        """
        considered: tuple[TopologyName, ...] = (
            "exact_dense",
            "block_streaming_exact",
            "linear_kernel",
            "sparse_top_k",
            "retrieval_first",
            "hierarchical_multiscale",
            "cross_stream",
            "temporal_event",
        )
        if forced is not None:
            if forced not in considered:
                raise TopologyError(f"Unknown topology {forced!r}")
            rejected = tuple(name for name in considered if name != forced)
            return forced, "caller_forced_single_kernel", rejected
        budget = pressure.active_budget()
        severity = pressure.severity()
        if candidate_count <= 0:
            return "exact_dense", "empty_set", tuple(name for name in considered if name != "exact_dense")
        if candidate_count <= min(DEFAULT_DENSE_LIMIT, budget) and severity < 0.85:
            chosen: TopologyName = "exact_dense"
            reason = "small_candidate_set_exact_is_legal"
        elif candidate_count >= 4096 or pressure.candidate_population >= 10000:
            chosen = "retrieval_first"
            reason = "huge_candidate_space_requires_staged_hard_selection"
        elif stream_count >= 3 and candidate_count > budget:
            chosen = "cross_stream"
            reason = "multiple_streams_compete_before_event_expansion"
        elif pressure.latency_budget_ms <= 20.0 and candidate_count > budget:
            chosen = "linear_kernel"
            reason = "latency_budget_forbids_quadratic_pairs"
        elif pressure.ram_used_ratio >= 0.88 and candidate_count > DEFAULT_DENSE_LIMIT:
            chosen = "block_streaming_exact"
            reason = "memory_pressure_chunks_queries_but_arithmetic_stays_quadratic"
        elif candidate_count >= budget * 2:
            chosen = "hierarchical_multiscale"
            reason = "medium_set_coarse_to_fine_skips_low_mass_blocks"
        else:
            chosen = "sparse_top_k"
            reason = "default_hard_topk_avoids_unselected_keys"
        rejected = tuple(name for name in considered if name != chosen)
        return chosen, reason, rejected


class EventMailbox:
    """Thread-safe event mailbox. Not an application server."""

    def __init__(self) -> None:
        """Create an empty mailbox."""
        self._lock = threading.Lock()
        self._items: deque[StreamEvent] = deque()

    def push(self, event: StreamEvent) -> None:
        """Enqueue an event. Higher rank is served first.

        Args:
            event: Incoming stream event.
        """
        with self._lock:
            self._items.append(event)
            self._items = deque(sorted(self._items, key=lambda item: item.rank(), reverse=True))

    def pop(self) -> StreamEvent | None:
        """Pop the highest-rank pending event.

        Returns:
            The next event, or None when empty.
        """
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def peek_rank(self) -> float:
        """Return the highest pending rank, or 0 when empty.

        Returns:
            A rank scalar for preemption comparisons.
        """
        with self._lock:
            if not self._items:
                return 0.0
            return self._items[0].rank()

    def depth(self) -> int:
        """Return current queue depth.

        Returns:
            Pending event count.
        """
        with self._lock:
            return len(self._items)

    def drain(self, limit: int | None = None) -> tuple[StreamEvent, ...]:
        """Pop up to limit events.

        Args:
            limit: Maximum events to pop. None drains the mailbox.

        Returns:
            Popped events in rank order.
        """
        taken: list[StreamEvent] = []
        remaining = limit if limit is not None else 2**30
        while remaining > 0:
            event = self.pop()
            if event is None:
                break
            taken.append(event)
            remaining -= 1
        return tuple(taken)


class ReactiveAttentionFabric:
    """Model-independent reactive attention fabric.

    Native descendant of Morpheus FlashAttention, rewritten so incoming system state
    changes allocation, unused kernels are not run, and context cannot overwrite memory.
    """

    def __init__(
        self,
        memory: MemoryRuntime | None = None,
        memory_path: str | None = None,
        dense_limit: int = DEFAULT_DENSE_LIMIT,
    ) -> None:
        """Create a resident fabric with default adapters and honest kernels.

        Args:
            memory: External memory runtime. When omitted, a sqlite runtime is created.
            memory_path: Path for the default sqlite memory runtime.
            dense_limit: Candidate count above which exact dense is refused by default.
        """
        self._lock = threading.RLock()
        self._streams: dict[str, StreamState] = {}
        self._mailbox = EventMailbox()
        self._adapters: list[StreamAdapter] = [FileStreamAdapter(), SqliteStreamAdapter(), StructuredObjectAdapter()]
        self._kernels: dict[TopologyName, AttentionKernel] = {
            "exact_dense": ExactDenseAttention(),
            "block_streaming_exact": BlockStreamingExactAttention(),
            "linear_kernel": LinearKernelAttention(),
            "sparse_top_k": SparseTopKAttention(),
            "retrieval_first": RetrievalFirstAttention(),
            "hierarchical_multiscale": HierarchicalMultiscaleAttention(),
            "cross_stream": CrossStreamAttention(),
            "temporal_event": TemporalEventAttention(),
        }
        self._selector = TopologySelector()
        self._dense_limit = dense_limit
        self._context: ActiveContext | None = None
        self._preempt = threading.Event()
        self._active_rank = 0.0
        self._active_query_id = ""
        if memory is not None:
            self._memory = memory
        else:
            path = memory_path or str(
                Path(os.environ.get("TMPDIR", "/tmp")) / f"attention-memory-{uuid.uuid4().hex}.sqlite"
            )
            self._memory = SqliteDurableMemory(path)

    def register_adapter(self, adapter: StreamAdapter) -> None:
        """Register a stream adapter. Earlier adapters win on first supports() match.

        Args:
            adapter: User-defined or replacement adapter.
        """
        with self._lock:
            self._adapters.insert(0, adapter)

    def register_kernel(self, kernel: AttentionKernel) -> None:
        """Register or replace one topology kernel. Used for optimized backends.

        Args:
            kernel: Kernel whose name matches a TopologyName.

        Raises:
            TopologyError: When the kernel name is not a known topology.
        """
        name = kernel.name
        if name not in self._kernels and name not in {
            "exact_dense",
            "block_streaming_exact",
            "linear_kernel",
            "sparse_top_k",
            "retrieval_first",
            "hierarchical_multiscale",
            "cross_stream",
            "temporal_event",
        }:
            raise TopologyError(f"Cannot register unknown topology {name!r}")
        with self._lock:
            self._kernels[name] = kernel

    def remember(self, record: DurableRecord) -> MemoryReceipt:
        """Write durable evidence through the memory boundary.

        Args:
            record: Provenance-bearing record that still points at native source.

        Returns:
            Memory receipt. Active context is not modified.
        """
        return self._memory.remember(record)

    def recall(self, query: MemoryQuery) -> tuple[MemoryCandidate, ...]:
        """Ask the memory runtime for candidates. Does not assemble context.

        Args:
            query: Retrieval constraints.

        Returns:
            Durable candidates.
        """
        return self._memory.recall(query)

    def context_snapshot(self) -> ActiveContext | None:
        """Return the current active context without touching durable memory.

        Returns:
            The last assembled context, or None before the first attend().
        """
        with self._lock:
            return self._context

    def ingest_native(self, ingress: NativeIngress) -> tuple[StreamEvent, ...]:
        """Adapt native material and observe the resulting events.

        Args:
            ingress: File, sqlite, object, or other typed source.

        Returns:
            The events produced by the winning adapter.

        Raises:
            AdapterError: When no adapter supports the ingress.
        """
        adapter = self._adapter_for(ingress)
        events = adapter.ingest(ingress)
        for event in events:
            self.observe(event)
        return events

    def observe(self, event: StreamEvent) -> ObserveResult:
        """Admit one event. May wake a stream or request preemption of in-flight work.

        Args:
            event: Typed stream event. Native source remains at content_ref.

        Returns:
            Buffer/wake/preempt status. A view is produced only on REALLOCATE events.

        Raises:
            InvalidStreamError: When required identity fields are missing.
        """
        if not event.stream_id or not event.event_id:
            raise InvalidStreamError("stream_id and event_id are required.")
        if event.content_ref.locator == "":
            raise InvalidStreamError("content_ref.locator is required; projection is not a source.")
        with self._lock:
            state = self._ensure_stream(event)
            if event.kind is FabricEventKind.PRIORITY_CHANGE:
                state.priority = event.priority
            elif event.kind is FabricEventKind.INVALIDATE_PROJECTION:
                state.cache_valid = False
                state.cached_projection = None
            elif event.kind is FabricEventKind.WAKE_CONTEXT:
                state.dormancy = StreamDormancy.ACTIVE
            elif event.kind is FabricEventKind.STREAM_CREATE:
                state.dormancy = StreamDormancy.ACTIVE
            if event.kind in {FabricEventKind.STREAM_APPEND, FabricEventKind.STREAM_CREATE, FabricEventKind.WAKE_CONTEXT}:
                state.events.append(event)
                state.last_event_id = event.event_id
                state.cache_valid = False
                if event.rank() >= 2.5:
                    state.dormancy = StreamDormancy.ACTIVE
            rank = event.rank()
            preempt_requested = False
            if self._active_query_id and rank > self._active_rank * INTERRUPT_MARGIN:
                self._preempt.set()
                preempt_requested = True
                self._mailbox.push(event)
            need_realloc = event.kind in {FabricEventKind.REALLOCATE, FabricEventKind.CROSS_STREAM_INTEGRATE}
            dormant = state.dormancy is StreamDormancy.DORMANT
            woke = (not dormant) and event.kind is FabricEventKind.WAKE_CONTEXT
        view: AttendedCognitiveView | None = None
        status: Literal["buffered", "woke", "reallocated", "preempt_requested"]
        if need_realloc:
            status = "reallocated"
            view = self.attend(
                AttentionQuery(
                    query_id=f"realloc-{event.event_id}",
                    stream_ids=(event.stream_id,),
                    routing_vector=event.routing_vector,
                )
            )
        elif preempt_requested:
            status = "preempt_requested"
        elif woke:
            status = "woke"
        else:
            status = "buffered"
        return ObserveResult(
            status=status,
            stream_id=event.stream_id,
            event_id=event.event_id,
            dormant=dormant,
            pending_depth=self._mailbox.depth(),
            view=view,
        )

    def drain(self, budget: int = 32) -> AttendedCognitiveView:
        """Absorb pending mailbox events and assemble an attended view.

        Args:
            budget: Maximum mailbox events to absorb before attending.

        Returns:
            An attended cognitive view over current active streams.
        """
        absorbed = self._mailbox.drain(limit=budget)
        for event in absorbed:
            self.observe(event)
        return self.attend(AttentionQuery(query_id=f"drain-{uuid.uuid4()}"))

    def attend(self, query: AttentionQuery) -> AttendedCognitiveView:
        """Select a topology, avoid unselected work, and emit a provenanced view.

        Args:
            query: Explicit attentional ask, optional candidate override, optional topology.

        Returns:
            Attended view plus receipt. Durable memory is read only when requested.
        """
        with self._lock:
            plan = self._plan_attend(query)
        result = plan.kernel.attend(plan.queries, plan.filtered, self._should_abort)
        if result.work.kernels_evaluated != 1:
            raise KernelError("Adaptive compute must evaluate exactly one kernel per attend().")
        with self._lock:
            late: list[AttentionCandidate] = []
            if self._should_abort():
                incoming = self._mailbox.drain(limit=8)
                late = [_event_to_candidate(event) for event in incoming]
                plan.filtered.extend(late)
            return self._commit_view(query, plan, result, late_candidates=tuple(late))

    def forward(
        self,
        inputs: Mapping[str, object] | tuple[object, object, object],
        latency_budget_ms: float = 100.0,
    ) -> NumericAttendResult:
        """Conventional numeric/QKV path for PyTorch interoperability.

        Dict keys are stream domains whose values are numeric sequences. A QKV tuple
        is treated as one model_activation stream. This is not FlashAttention.

        Args:
            inputs: Mapping of stream name to numeric data, or (q, k, v) numeric objects.
            latency_budget_ms: Pressure input for topology selection.

        Returns:
            Numeric outputs plus an attention receipt.

        Raises:
            InvalidStreamError: When inputs cannot be read as vectors.
        """
        extra: list[AttentionCandidate] = []
        query_vectors: list[tuple[float, ...]]
        if isinstance(inputs, tuple) and len(inputs) == 3:
            queries = _as_numeric_matrix(inputs[0])
            keys = _as_numeric_matrix(inputs[1])
            values = _as_numeric_matrix(inputs[2])
            query_vectors = list(queries)
            for index, key in enumerate(keys):
                value = values[index] if index < len(values) else key
                extra.append(
                    AttentionCandidate(
                        candidate_id=f"qkv:{index}",
                        stream_id="model_activation",
                        event_id=f"k:{index}",
                        domain="model_activation",
                        timestamp=time.time(),
                        content_ref=ContentRef(scheme="tensor", locator=f"k[{index}]"),
                        routing_vector=_fit_dim(key),
                        projection=_fit_dim(value),
                    )
                )
        elif isinstance(inputs, Mapping):
            query_vectors = []
            for stream_name, payload in inputs.items():
                matrix = _as_numeric_matrix(payload)
                query_vectors.extend(matrix)
                for index, row in enumerate(matrix):
                    extra.append(
                        AttentionCandidate(
                            candidate_id=f"{stream_name}:{index}",
                            stream_id=stream_name,
                            event_id=f"{stream_name}:{index}",
                            domain=stream_name,
                            timestamp=time.time(),
                            content_ref=ContentRef(scheme="tensor", locator=f"{stream_name}[{index}]"),
                            routing_vector=_fit_dim(row),
                            projection=_fit_dim(row),
                        )
                    )
        else:
            raise InvalidStreamError("forward() expects a stream mapping or a (q, k, v) tuple.")
        if not query_vectors:
            query_vectors = [(0.0,) * VECTOR_DIM]
        view = self.attend(
            AttentionQuery(
                query_id=f"forward-{uuid.uuid4()}",
                routing_vector=query_vectors[0],
                latency_budget_ms=latency_budget_ms,
                extra_candidates=tuple(extra),
            )
        )
        output = view.integrated_projection or query_vectors
        status: Literal["ok", "empty"] = "ok" if view.status != "empty" else "empty"
        return NumericAttendResult(status=status, output=output, receipt=view.receipt)

    def _plan_attend(self, query: AttentionQuery) -> AttendPlan:
        """Snapshot candidates and choose exactly one kernel. Caller holds the lock."""
        started = time.perf_counter()
        self._preempt.clear()
        self._active_query_id = query.query_id
        candidates = self._collect_candidates(query)
        pressure = query.pressure or sample_resource_pressure(
            queue_depth=self._mailbox.depth(),
            latency_budget_ms=query.latency_budget_ms,
            candidate_population=len(candidates),
        )
        if query.max_candidates is not None:
            budget = min(query.max_candidates, pressure.active_budget())
        else:
            budget = pressure.active_budget()
        self._active_rank = max((item.rank() for item in candidates), default=0.0)
        filtered = self._metadata_filter(candidates, query, budget)
        stream_count = len({item.stream_id for item in filtered})
        topology, reason, rejected = self._selector.select(
            candidate_count=len(filtered),
            stream_count=stream_count,
            pressure=pressure,
            forced=query.topology,
        )
        if topology == "exact_dense" and len(filtered) > self._dense_limit and query.topology is None:
            topology = "sparse_top_k"
            reason = "dense_refused_above_limit_hard_switch_to_sparse"
            rejected = tuple(name for name in rejected if name != "sparse_top_k") + ("exact_dense",)
        kernel = self._instantiate_kernel(topology, budget)
        queries = self._query_vectors(query, filtered)
        return AttendPlan(
            kernel=kernel,
            queries=queries,
            filtered=filtered,
            candidates_before=len(candidates),
            budget=budget,
            topology=topology,
            reason=reason,
            rejected=rejected,
            pressure=pressure,
            started=started,
        )

    def _instantiate_kernel(self, topology: TopologyName, budget: int) -> AttentionKernel:
        """Build a one-shot kernel so registered instances are not mutated underfoot."""
        registered = self._kernels.get(topology)
        if registered is None:
            raise TopologyError(f"No kernel registered for {topology}")
        if type(registered) is SparseTopKAttention:
            return SparseTopKAttention(top_k=budget)
        if type(registered) is RetrievalFirstAttention:
            return RetrievalFirstAttention(top_k=budget)
        if type(registered) is TemporalEventAttention:
            return TemporalEventAttention(top_k=budget)
        if type(registered) is HierarchicalMultiscaleAttention:
            keep = max(1, min(DEFAULT_HIERARCHICAL_BLOCKS, max(1, budget // 4)))
            return HierarchicalMultiscaleAttention(keep_blocks=keep)
        if type(registered) is CrossStreamAttention:
            keep_streams = max(1, min(3, budget // 8 or 1))
            return CrossStreamAttention(keep_streams=keep_streams, inner_k=max(4, budget))
        return registered

    def _commit_view(
        self,
        query: AttentionQuery,
        plan: AttendPlan,
        result: KernelResult,
        late_candidates: tuple[AttentionCandidate, ...] = (),
    ) -> AttendedCognitiveView:
        """Assemble context from kernel output. Caller holds the lock."""
        selected_candidates = [
            plan.filtered[index] for index in result.selected_indices if 0 <= index < len(plan.filtered)
        ]
        if not selected_candidates and plan.filtered:
            selected_candidates = list(plan.filtered[: plan.budget])
        if late_candidates:
            selected_candidates = list(late_candidates) + selected_candidates
        status: Literal["ok", "empty", "preempted", "pressure_reduced"]
        if self._should_abort() or any("preempted" in item for item in result.lossy_transforms):
            status = "preempted"
        elif not selected_candidates:
            status = "empty"
        elif result.work.candidates_selected < plan.candidates_before and plan.pressure.severity() >= 0.6:
            status = "pressure_reduced"
        else:
            status = "ok"
        slots = tuple(
            ContextSlot(
                stream_id=item.stream_id,
                event_id=item.event_id,
                content_ref=item.content_ref,
                domain=item.domain,
                projection=item.projection or item.routing_vector,
                salience=item.rank(),
            )
            for item in selected_candidates
        )
        context = ActiveContext(
            assembled_at=time.time(),
            pressure_severity=plan.pressure.severity(),
            slots=slots,
            query_id=query.query_id,
            reconstructible=True,
        )
        self._context = context
        confidence = 0.0
        if result.weights:
            peak = max((max(row) if row else 0.0) for row in result.weights)
            entropy = 0.0
            for row in result.weights:
                for weight in row:
                    if weight > 1e-12:
                        entropy -= weight * math.log(weight)
            confidence = _clamp01(peak * (1.0 / (1.0 + entropy)))
        salience = max((slot.salience for slot in slots), default=0.0)
        elapsed_ms = (time.perf_counter() - plan.started) * 1000.0
        receipt = AttentionReceipt(
            mechanism=plan.topology,
            complexity_class=result.complexity_class,
            contributing_streams=tuple(dict.fromkeys(item.stream_id for item in selected_candidates)),
            source_event_ids=tuple(item.event_id for item in selected_candidates),
            candidates_before=plan.candidates_before,
            candidates_after=len(selected_candidates),
            work=result.work,
            confidence=confidence,
            salience=salience,
            pressure_severity=plan.pressure.severity(),
            topology_reason=plan.reason,
            rejected_topologies=plan.rejected,
            lossy_transforms=result.lossy_transforms,
            elapsed_ms=elapsed_ms,
            preempted=status == "preempted",
        )
        self._active_query_id = ""
        self._active_rank = 0.0
        return AttendedCognitiveView(
            status=status,
            query_id=query.query_id,
            focus_stream_ids=receipt.contributing_streams,
            selected_event_ids=receipt.source_event_ids,
            slots=slots,
            integrated_projection=result.outputs,
            context=context,
            receipt=receipt,
        )

    def _collect_candidates(self, query: AttentionQuery) -> list[AttentionCandidate]:
        """Assemble live, extra, and optional memory candidates without materializing sources."""
        collected: list[AttentionCandidate] = []
        allowed_streams = set(query.stream_ids) if query.stream_ids is not None else None
        allowed_domains = set(query.domains) if query.domains is not None else None
        for state in self._streams.values():
            if allowed_streams is not None and state.spec_id not in allowed_streams:
                continue
            if state.dormancy is StreamDormancy.DORMANT and (allowed_streams is None or state.spec_id not in allowed_streams):
                continue
            for event in state.events:
                if allowed_domains is not None and event.domain not in allowed_domains:
                    continue
                collected.append(_event_to_candidate(event))
        collected.extend(query.extra_candidates)
        if query.include_memory:
            recalled = self._memory.recall(
                MemoryQuery(
                    text=query.text,
                    stream_ids=query.stream_ids,
                    domains=query.domains,
                    routing_vector=query.routing_vector,
                    limit=64,
                )
            )
            collected.extend(_memory_to_candidate(item) for item in recalled)
        return collected

    def _metadata_filter(
        self,
        candidates: Sequence[AttentionCandidate],
        query: AttentionQuery,
        budget: int,
    ) -> list[AttentionCandidate]:
        """Hard-filter by domain/stream and drop the worst ranks before any kernel."""
        allowed_domains = set(query.domains) if query.domains is not None else None
        filtered = [
            item
            for item in candidates
            if allowed_domains is None or item.domain in allowed_domains
        ]
        if len(filtered) <= max(budget * 4, DEFAULT_DENSE_LIMIT):
            return list(filtered)
        ranked = sorted(filtered, key=lambda item: item.rank(), reverse=True)
        return ranked[: max(budget * 4, DEFAULT_DENSE_LIMIT)]

    def _query_vectors(
        self,
        query: AttentionQuery,
        candidates: Sequence[AttentionCandidate],
    ) -> list[tuple[float, ...]]:
        """Build query vectors from the ask, or from candidate metadata if needed."""
        if query.routing_vector is not None:
            return [_fit_dim(query.routing_vector)]
        if query.text:
            return [_hashed_projection(query.text)]
        if candidates:
            peak = max(candidates, key=lambda item: item.rank())
            return [_candidate_key(peak)]
        return [(0.0,) * VECTOR_DIM]

    def _ensure_stream(self, event: StreamEvent) -> StreamState:
        """Create or return the live stream state for an event."""
        state = self._streams.get(event.stream_id)
        if state is None:
            state = StreamState(
                spec_id=event.stream_id,
                domain=event.domain,
                source=event.source,
                role=event.role,
                persistence=event.persistence,
                priority=event.priority,
                dormancy=StreamDormancy.ACTIVE,
            )
            self._streams[event.stream_id] = state
        return state

    def _adapter_for(self, ingress: NativeIngress) -> StreamAdapter:
        """Return the first adapter that supports native ingress."""
        for adapter in self._adapters:
            if adapter.supports(ingress):
                return adapter
        raise AdapterError(f"No stream adapter supports domain {ingress.domain!r} locator {ingress.locator!r}")

    def _should_abort(self) -> bool:
        """Return True when a higher-rank event requested preemption."""
        if self._preempt.is_set():
            return True
        return self._mailbox.peek_rank() > self._active_rank * INTERRUPT_MARGIN


def describe_path(project_root: str) -> PathStreamSummary:
    """Ingest a real filesystem path as a typed stream and return a bounded attention summary.

    Args:
        project_root: Directory or file to address. The path remains the native source.

    Returns:
        A structured summary with status, normalized path, and selection provenance.
    """
    path = Path(project_root).expanduser().resolve()
    memory_dir = Path("/tmp") / f"attention-describe-{uuid.uuid4().hex}"
    memory_dir.mkdir(parents=True, exist_ok=True)
    fabric = ReactiveAttentionFabric(memory_path=str(memory_dir / "memory.sqlite"))
    domain = "directory" if path.is_dir() else "file"
    events = fabric.ingest_native(
        NativeIngress(domain=domain, source="describe_path", locator=str(path))
    )
    view = fabric.attend(
        AttentionQuery(
            query_id="describe-path",
            text=str(path),
            latency_budget_ms=50.0,
        )
    )
    return PathStreamSummary(
        status="ok" if view.status in {"ok", "pressure_reduced", "preempted"} else view.status,
        normalized_path=str(path),
        stream_id=events[0].stream_id if events else "",
        event_count=len(events),
        domain=domain,
        mechanism=view.receipt.mechanism,
        candidates_before=view.receipt.candidates_before,
        candidates_after=view.receipt.candidates_after,
    )


__all__ = [
    "ActiveContext",
    "AdapterError",
    "AttendedCognitiveView",
    "AttentionCandidate",
    "AttentionFabricError",
    "AttentionQuery",
    "AttentionReceipt",
    "AttentionRole",
    "BlockStreamingExactAttention",
    "ContentRef",
    "CrossStreamAttention",
    "DurableRecord",
    "ExactDenseAttention",
    "FabricEventKind",
    "FileStreamAdapter",
    "HierarchicalMultiscaleAttention",
    "InvalidStreamError",
    "KernelError",
    "LinearKernelAttention",
    "MemoryBoundaryError",
    "MemoryCandidate",
    "MemoryQuery",
    "MemoryReceipt",
    "NativeIngress",
    "NumericAttendResult",
    "ObserveResult",
    "PathStreamSummary",
    "PersistencePolicy",
    "ProvenanceStamp",
    "ReactiveAttentionFabric",
    "ResourcePressure",
    "RetrievalFirstAttention",
    "SparseTopKAttention",
    "SqliteDurableMemory",
    "SqliteStreamAdapter",
    "StreamEvent",
    "StructuredObjectAdapter",
    "TemporalEventAttention",
    "TopologyError",
    "TopologySelector",
    "WorkMeter",
    "describe_path",
    "sample_resource_pressure",
]
