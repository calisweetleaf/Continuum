"""Real knowledge-semantic-archive bridge for the Cherry memory pilot.

Source: knowledge-semantic-archive ``MemoryArchiveService`` and
    ``ProjectRecorder`` public APIs.
Integrated: 2026-07-14
Purpose: Own one dedicated asyncio loop thread, seed a disposable signed
    archive, and normalize authority-validated reads into Cherry's synchronous
    ``ArchiveReadClient`` contract without creating an import-time dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import threading
import time
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TypeVar

from ..core.jcs import canonicalize
from ..substrate.adapters.archive import ArchiveEvidence, ArchiveEvidenceResult

_T = TypeVar("_T")

GRAPH_QUERY = "orchard lattice memory route"
LEXICAL_QUERY = "LEXICAL-EXACT-CANARY"
TEMPORAL_QUERY = "TEMPORAL-ORDINAL-CANARY"
SCOPE_QUERY = "PARTITION-SHARED-CANARY"
_FINGERPRINT_QUERY = "canonical archive fingerprint evidence"


class ArchivePilotUnavailable(RuntimeError):
    """Raised when the optional knowledge-semantic-archive package is absent."""


class ArchivePilotInvariantError(RuntimeError):
    """Raised when the real fixture cannot establish the pilot's proof shape."""


@dataclass(frozen=True, slots=True)
class ArchiveFixtureManifest:
    """Stable semantic roles and runtime canonical identifiers for one fixture."""

    primary_project_id: str
    foreign_project_id: str
    lexical_record_id: str
    graph_anchor_record_id: str
    graph_target_record_id: str
    graph_distractor_record_ids: tuple[str, ...]
    temporal_record_id: str
    temporal_message_source_id: str
    scope_record_id: str
    foreign_record_id: str

    def to_payload(self) -> dict[str, object]:
        """Return deterministic JSON-compatible fixture identifiers."""

        return {
            "primary_project_id": self.primary_project_id,
            "foreign_project_id": self.foreign_project_id,
            "lexical_record_id": self.lexical_record_id,
            "graph_anchor_record_id": self.graph_anchor_record_id,
            "graph_target_record_id": self.graph_target_record_id,
            "graph_distractor_record_ids": list(self.graph_distractor_record_ids),
            "temporal_record_id": self.temporal_record_id,
            "temporal_message_source_id": self.temporal_message_source_id,
            "scope_record_id": self.scope_record_id,
            "foreign_record_id": self.foreign_record_id,
        }


def archive_dependency_available() -> bool:
    """Return whether the optional real archive package can be imported."""

    return importlib.util.find_spec("knowledge_semantic_archive") is not None


class KSAProjectReadClient:
    """Synchronous read client over a disposable real ProjectRecorder runtime.

    The archive remains async and loop-affine. This bridge owns a dedicated
    event-loop thread for its complete lifecycle; synchronous Cherry search
    submits coroutines with ``run_coroutine_threadsafe``. Only fixture setup
    mutates canonical state. Every method exposed through ``ArchiveReadClient``
    is a project-scoped read.

    Args:
        runtime_root: Fresh directory used only for this disposable pilot.
        primary_project_id: Project partition exposed to Cherry actions.
        foreign_project_id: Canary partition used to measure scope leakage.
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        primary_project_id: str = "cherry-primary",
        foreign_project_id: str = "cherry-foreign",
    ) -> None:
        if not archive_dependency_available():
            raise ArchivePilotUnavailable(
                "knowledge_semantic_archive is unavailable; run with its installed "
                "environment or add the repository to PYTHONPATH"
            )
        if not isinstance(runtime_root, Path):
            raise TypeError("runtime_root must be pathlib.Path")
        if runtime_root.exists() and any(runtime_root.iterdir()):
            raise ArchivePilotInvariantError(
                f"pilot runtime root must be absent or empty: {runtime_root}"
            )
        self._runtime_root = runtime_root
        self._primary_project_id = primary_project_id
        self._foreign_project_id = foreign_project_id
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="cherry-ksa-pilot-loop",
            daemon=True,
        )
        self._service: object | None = None
        self._primary: object | None = None
        self._foreign: object | None = None
        self._closed = False
        self._read_calls = 0
        self._read_wall_ms = 0.0
        self._fingerprint_checks = 0
        self._thread.start()
        if not self._loop_ready.wait(timeout=10.0):
            self._closed = True
            self._stop_requested.set()
            try:
                self._stop_loop()
            except ArchivePilotInvariantError as cleanup_error:
                raise ArchivePilotInvariantError(
                    "archive event-loop thread did not start and could not be joined"
                ) from cleanup_error
            raise ArchivePilotInvariantError("archive event-loop thread did not start")
        try:
            self._fixture = self._submit(self._initialize())
        except BaseException as initialization_error:
            # `_initialize` installs service/facade references before it seeds
            # and validates fixtures. If any later proof fails, close those
            # real resources on their owning loop before stopping the thread.
            try:
                self._submit(self._close_async())
            except BaseException as cleanup_error:
                initialization_error.add_note(
                    "archive initialization cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            self._closed = True
            self._stop_requested.set()
            try:
                self._stop_loop()
            except ArchivePilotInvariantError as cleanup_error:
                initialization_error.add_note(
                    "archive event-loop cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    def __enter__(self) -> KSAProjectReadClient:
        """Return this open client."""

        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close the service and join the owned loop thread."""

        self.close()

    @property
    def fixture(self) -> ArchiveFixtureManifest:
        """Return immutable canonical ids established during fixture setup."""

        return self._fixture

    @property
    def read_calls(self) -> int:
        """Return normalized archive action reads, excluding fingerprint checks."""

        return self._read_calls

    @property
    def read_wall_ms(self) -> float:
        """Return wall time spent in normalized archive action reads."""

        return self._read_wall_ms

    @property
    def fingerprint_checks(self) -> int:
        """Return public canonical-state fingerprint evaluations."""

        return self._fingerprint_checks

    def canonical_fingerprint(self) -> str:
        """Hash validated public records, canonical links, and canonical count."""

        self._ensure_open()
        self._fingerprint_checks += 1
        return self._submit(self._canonical_fingerprint_async())

    def search(self, query: str, *, limit: int) -> ArchiveEvidenceResult:
        """Run deterministic scoped lexical search through ProjectRecorder."""

        return self._timed_read(self._search_async(query, limit=limit))

    def explore_knowledge_graph(
        self,
        query: str,
        *,
        limit: int,
        graph_limit: int,
        hop_depth: int,
    ) -> ArchiveEvidenceResult:
        """Run bounded authenticated graph exploration through ProjectRecorder."""

        return self._timed_read(
            self._graph_async(
                query,
                limit=limit,
                graph_limit=graph_limit,
                hop_depth=hop_depth,
            )
        )

    def search_conversation_messages(
        self,
        query: str,
        *,
        limit: int,
    ) -> ArchiveEvidenceResult:
        """Run scoped granular temporal-message search through ProjectRecorder."""

        return self._timed_read(self._temporal_async(query, limit=limit))

    def get_recent(self, *, limit: int) -> ArchiveEvidenceResult:
        """Return recent authenticated project records."""

        return self._timed_read(self._recent_async(limit=limit))

    def build_context(
        self,
        query: str,
        *,
        limit: int,
        max_tokens: int,
    ) -> ArchiveEvidenceResult:
        """Return bounded prompt context with canonical source identifiers."""

        return self._timed_read(
            self._context_async(query, limit=limit, max_tokens=max_tokens)
        )

    def close(self) -> None:
        """Close both facades and the shared service on their owned event loop."""

        if self._closed:
            return
        try:
            self._submit(self._close_async())
        finally:
            self._closed = True
            self._stop_loop()

    def _run_loop(self) -> None:
        """Own the asyncio loop until the synchronous bridge is closed."""

        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        if not self._stop_requested.is_set():
            self._loop.run_forever()
        self._loop.close()

    def _stop_loop(self) -> None:
        """Stop and join the owned loop without crossing thread affinity."""

        self._stop_requested.set()
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            raise ArchivePilotInvariantError("archive event-loop thread did not stop")

    def _submit(self, coroutine: Coroutine[object, object, _T]) -> _T:
        """Submit one coroutine to the owned loop and propagate its result."""

        if not self._loop.is_running():
            coroutine.close()
            raise ArchivePilotInvariantError("archive event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=120.0)

    def _timed_read(
        self,
        coroutine: Coroutine[object, object, ArchiveEvidenceResult],
    ) -> ArchiveEvidenceResult:
        """Submit one action read and update out-of-band work accounting."""

        self._ensure_open()
        started = time.perf_counter()
        try:
            return self._submit(coroutine)
        finally:
            self._read_calls += 1
            self._read_wall_ms += (time.perf_counter() - started) * 1_000.0

    async def _initialize(self) -> ArchiveFixtureManifest:
        """Open the real service, seed signed fixtures, and prove their shape."""

        from knowledge_semantic_archive import (
            ArchiveConfig,
            ConversationInput,
            ConversationMessageInput,
            MemoryArchiveService,
            Principal,
            ProjectRecorder,
        )

        self._runtime_root.mkdir(parents=True, exist_ok=True)
        service = await MemoryArchiveService.open(
            ArchiveConfig(runtime_root=self._runtime_root)
        )
        principal = Principal("cherry-pilot", "operator")
        primary = ProjectRecorder(
            service,
            principal,
            self._primary_project_id,
        )
        foreign = ProjectRecorder(
            service,
            principal,
            self._foreign_project_id,
        )
        self._service = service
        self._primary = primary
        self._foreign = foreign

        lexical = await primary.remember(
            f"{LEXICAL_QUERY} signed exact lexical archive evidence",
            idempotency_key="pilot-lexical",
        )
        scope_record = await primary.remember(
            f"{SCOPE_QUERY} primary project authority canary",
            idempotency_key="pilot-scope-primary",
        )
        anchor = await primary.remember(
            f"{GRAPH_QUERY} {GRAPH_QUERY} {GRAPH_QUERY} signed graph anchor",
            idempotency_key="pilot-graph-anchor",
        )
        distractors = []
        for index in range(8):
            distractors.append(
                await primary.remember(
                    f"{GRAPH_QUERY} deterministic distractor {index}",
                    idempotency_key=f"pilot-graph-distractor-{index}",
                )
            )
        target = await primary.remember(
            "OBSIDIAN-HOP-TARGET signed relationship destination without seed terms",
            idempotency_key="pilot-graph-target",
        )
        await primary.link_records(
            anchor.record_id,
            target.record_id,
            relation_type="epistemic_parent",
        )

        started_at = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
        temporal = await primary.ingest_conversation(
            ConversationInput(
                conversation_id="cherry-temporal-fixture",
                title="Cherry temporal fixture",
                messages=(
                    ConversationMessageInput(
                        "user",
                        "Open the deterministic pilot ledger",
                        timestamp=started_at,
                    ),
                    ConversationMessageInput(
                        "assistant",
                        f"{TEMPORAL_QUERY} granular evidence at ordinal one",
                        timestamp=started_at + timedelta(minutes=1),
                    ),
                ),
            ),
            idempotency_key="pilot-temporal-conversation",
        )
        temporal_record_id = temporal.record_ids[0]
        temporal_source_id = f"{temporal_record_id}#1"

        foreign_record = await foreign.remember(
            f"{SCOPE_QUERY} FOREIGN-PARTITION-CANARY must never cross scope",
            idempotency_key="pilot-scope-foreign",
        )
        fixture = ArchiveFixtureManifest(
            primary_project_id=self._primary_project_id,
            foreign_project_id=self._foreign_project_id,
            lexical_record_id=lexical.record_id,
            graph_anchor_record_id=anchor.record_id,
            graph_target_record_id=target.record_id,
            graph_distractor_record_ids=tuple(record.record_id for record in distractors),
            temporal_record_id=temporal_record_id,
            temporal_message_source_id=temporal_source_id,
            scope_record_id=scope_record.record_id,
            foreign_record_id=foreign_record.record_id,
        )
        await self._validate_fixture(fixture)
        return fixture

    async def _validate_fixture(self, fixture: ArchiveFixtureManifest) -> None:
        """Prove graph-hop, temporal ordinal, and partition-canary assumptions."""

        primary = self._require_primary()
        foreign = self._require_foreign()
        lexical_graph = await primary.search(GRAPH_QUERY, limit=100)
        lexical_ids = {result.record.record_id for result in lexical_graph.results}
        if fixture.graph_target_record_id in lexical_ids:
            raise ArchivePilotInvariantError(
                "graph target unexpectedly appears in conjunctive lexical retrieval"
            )
        graph = await primary.explore_knowledge_graph(
            GRAPH_QUERY,
            limit=100,
            graph_limit=100,
            hop_depth=2,
        )
        if fixture.graph_target_record_id in graph.seed_record_ids:
            raise ArchivePilotInvariantError("graph target is a seed, not a hop-only result")
        target_hits = [
            hit for hit in graph.results
            if hit.record.record_id == fixture.graph_target_record_id
        ]
        if len(target_hits) != 1 or target_hits[0].hop_distance < 1:
            raise ArchivePilotInvariantError("signed graph target was not reached by traversal")
        if not any(
            self._enum_value(step.provenance) == "canonical_link"
            for step in target_hits[0].path
        ):
            raise ArchivePilotInvariantError(
                "graph target path does not preserve canonical-link provenance"
            )
        temporal = await primary.search_conversation_messages(TEMPORAL_QUERY, limit=10)
        if len(temporal) != 1 or temporal[0].message.ordinal != 1:
            raise ArchivePilotInvariantError("temporal canary did not resolve to ordinal one")
        primary_scope = await primary.search(SCOPE_QUERY, limit=100)
        foreign_scope = await foreign.search(SCOPE_QUERY, limit=100)
        primary_ids = {result.record.record_id for result in primary_scope.results}
        foreign_ids = {result.record.record_id for result in foreign_scope.results}
        if fixture.scope_record_id not in primary_ids:
            raise ArchivePilotInvariantError("primary scope canary is not retrievable")
        if fixture.foreign_record_id in primary_ids or fixture.scope_record_id in foreign_ids:
            raise ArchivePilotInvariantError("project partitions leaked during fixture validation")

    async def _canonical_fingerprint_async(self) -> str:
        """Compute a bounded public-authority snapshot on the archive loop."""

        service = self._require_service()
        primary = self._require_primary()
        foreign = self._require_foreign()
        records = []
        canonical_edges = []
        for recorder in (primary, foreign):
            recent = await recorder.get_recent(limit=100)
            records.extend(self._record_payload(record) for record in recent)
            graph = await recorder.explore_knowledge_graph(
                _FINGERPRINT_QUERY,
                limit=100,
                graph_limit=100,
                hop_depth=0,
            )
            canonical_edges.extend(
                self._edge_payload(edge)
                for edge in graph.edges
                if self._enum_value(edge.provenance) == "canonical_link"
            )
        health = await service.health()
        payload = {
            "canonical_records": health.canonical_records,
            "records": sorted(records, key=lambda item: str(item["record_id"])),
            "canonical_edges": sorted(
                canonical_edges,
                key=lambda item: (
                    str(item["source_record_id"]),
                    str(item["target_record_id"]),
                    str(item["relation_type"]),
                ),
            ),
        }
        return hashlib.sha256(canonicalize(payload).encode("utf-8")).hexdigest()

    async def _search_async(self, query: str, *, limit: int) -> ArchiveEvidenceResult:
        """Normalize one real lexical result set into immutable evidence."""

        result = await self._require_primary().search(query, limit=limit)
        evidence = tuple(
            self._record_evidence(
                "lexical",
                item.record,
                score=item.score,
                matched_terms=item.matched_terms,
            )
            for item in result.results
        )
        return ArchiveEvidenceResult(
            channel="lexical",
            evidence=evidence,
            summary=f"candidates={result.total_candidates}",
        )

    async def _graph_async(
        self,
        query: str,
        *,
        limit: int,
        graph_limit: int,
        hop_depth: int,
    ) -> ArchiveEvidenceResult:
        """Normalize graph hits and authority-labeled edges into evidence."""

        result = await self._require_primary().explore_knowledge_graph(
            query,
            limit=limit,
            graph_limit=graph_limit,
            hop_depth=hop_depth,
        )
        evidence: list[ArchiveEvidence] = []
        seeds = frozenset(result.seed_record_ids)
        for hit in result.results:
            path_provenance = ",".join(
                self._enum_value(step.provenance) for step in hit.path
            ) or "seed"
            path_relations = ",".join(step.relation_type for step in hit.path) or "seed"
            evidence.append(
                self._record_evidence(
                    "graph",
                    hit.record,
                    score=hit.semantic_similarity,
                    extra_metadata={
                        "authority": "signed_usms_validated",
                        "hop_distance": str(hit.hop_distance),
                        "is_seed": str(hit.record.record_id in seeds).lower(),
                        "item_kind": "record",
                        "path_provenance": path_provenance,
                        "path_relations": path_relations,
                    },
                )
            )
        for edge in result.edges:
            provenance = self._enum_value(edge.provenance)
            source_id = (
                f"edge:{edge.source_record_id}:{edge.relation_type}:"
                f"{edge.target_record_id}:{provenance}"
            )
            evidence.append(
                ArchiveEvidence(
                    channel="graph",
                    source_id=source_id,
                    content=(
                        f"{edge.source_record_id} {edge.relation_type} "
                        f"{edge.target_record_id}"
                    ),
                    canonical_hash=None,
                    metadata=self._metadata(
                        authority=(
                            "signed_usms_link"
                            if provenance == "canonical_link"
                            else "derived_similarity_proposal"
                        ),
                        item_kind="edge",
                        provenance=provenance,
                        relation_type=edge.relation_type,
                        source_record_id=edge.source_record_id,
                        target_record_id=edge.target_record_id,
                        weight=self._format_score(edge.weight),
                    ),
                )
            )
        attention = result.attention
        return ArchiveEvidenceResult(
            channel="graph",
            evidence=tuple(evidence),
            summary=(
                f"nodes={result.node_count};edges={result.edge_count};"
                f"attention={self._enum_value(attention.model_state)};"
                f"used_for_ranking={str(attention.used_for_ranking).lower()};"
                f"degraded={str(attention.degraded).lower()}"
            ),
        )

    async def _temporal_async(self, query: str, *, limit: int) -> ArchiveEvidenceResult:
        """Normalize granular validated messages, preserving ordinal identity."""

        hits = await self._require_primary().search_conversation_messages(
            query,
            limit=limit,
        )
        evidence = []
        for hit in hits:
            message = hit.message
            message_payload = {
                "record_id": message.record_id,
                "conversation_id": message.conversation_id,
                "ordinal": message.ordinal,
                "role": message.role,
                "content": message.content,
                "timestamp": message.timestamp.astimezone(timezone.utc).isoformat(),
            }
            evidence.append(
                ArchiveEvidence(
                    channel="temporal",
                    source_id=f"{message.record_id}#{message.ordinal}",
                    content=message.content,
                    canonical_hash=self._hash_payload(message_payload),
                    metadata=self._metadata(
                        authority="signed_usms_validated",
                        conversation_id=message.conversation_id,
                        item_kind="message",
                        matched_terms=",".join(hit.matched_terms),
                        ordinal=str(message.ordinal),
                        record_id=message.record_id,
                        role=message.role,
                        score=self._format_score(hit.score),
                        scope_partition=self._require_primary().scope.partition_key,
                    ),
                )
            )
        return ArchiveEvidenceResult(
            channel="temporal",
            evidence=tuple(evidence),
            summary=f"messages={len(evidence)}",
        )

    async def _recent_async(self, *, limit: int) -> ArchiveEvidenceResult:
        """Normalize recent authenticated records."""

        records = await self._require_primary().get_recent(limit=limit)
        return ArchiveEvidenceResult(
            channel="recent",
            evidence=tuple(self._record_evidence("recent", record) for record in records),
            summary=f"records={len(records)}",
        )

    async def _context_async(
        self,
        query: str,
        *,
        limit: int,
        max_tokens: int,
    ) -> ArchiveEvidenceResult:
        """Normalize one bounded context and its canonical source ids."""

        context = await self._require_primary().build_context(
            query,
            limit=limit,
            max_tokens=max_tokens,
        )
        evidence = tuple(
            ArchiveEvidence(
                channel="context",
                source_id=record_id,
                content=context.text,
                canonical_hash=self._hash_payload(
                    {"record_id": record_id, "context": context.text}
                ),
                metadata=self._metadata(
                    authority="signed_usms_validated_context",
                    item_kind="context",
                    token_estimate=str(context.token_estimate),
                ),
            )
            for record_id in context.record_ids
        )
        return ArchiveEvidenceResult(
            channel="context",
            evidence=evidence,
            summary=f"records={len(evidence)};tokens={context.token_estimate}",
        )

    async def _close_async(self) -> None:
        """Close facades and their shared service exactly once."""

        primary, foreign, service = self._primary, self._foreign, self._service
        self._primary = None
        self._foreign = None
        self._service = None
        if primary is not None:
            await primary.close()
        if foreign is not None:
            await foreign.close()
        if service is not None:
            await service.close()

    def _record_evidence(
        self,
        channel: str,
        record: object,
        *,
        score: float | None = None,
        matched_terms: Sequence[str] = (),
        extra_metadata: Mapping[str, str] | None = None,
    ) -> ArchiveEvidence:
        """Build one authority-attributed record item for a normalized lane."""

        payload = self._record_payload(record)
        metadata = {
            "authority": "signed_usms_validated",
            "item_kind": "record",
            "record_kind": self._enum_value(record.kind),
            "scope_partition": record.scope.partition_key,
        }
        if score is not None:
            metadata["score"] = self._format_score(score)
        if matched_terms:
            metadata["matched_terms"] = ",".join(matched_terms)
        if extra_metadata:
            metadata.update(extra_metadata)
        return ArchiveEvidence(
            channel=channel,  # type: ignore[arg-type] -- checked by ArchiveEvidence.
            source_id=record.record_id,
            content=record.text,
            canonical_hash=self._hash_payload(payload),
            metadata=tuple(sorted(metadata.items())),
        )

    @classmethod
    def _record_payload(cls, record: object) -> dict[str, object]:
        """Return stable public canonical record fields without access telemetry."""

        return {
            "record_id": record.record_id,
            "kind": cls._enum_value(record.kind),
            "partition_key": record.scope.partition_key,
            "text": record.text,
            "created_at": record.created_at.astimezone(timezone.utc).isoformat(),
            "metadata": cls._json_safe(record.metadata),
            "source_id": record.source_id,
            "deleted": record.deleted,
        }

    @classmethod
    def _edge_payload(cls, edge: object) -> dict[str, object]:
        """Return stable public fields for one authenticated canonical link."""

        return {
            "source_record_id": edge.source_record_id,
            "target_record_id": edge.target_record_id,
            "relation_type": edge.relation_type,
            "weight": edge.weight,
            "provenance": cls._enum_value(edge.provenance),
        }

    @classmethod
    def _json_safe(cls, value: object) -> object:
        """Normalize bounded archive metadata into Cherry-canonical JSON values."""

        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, Enum):
            return cls._json_safe(value.value)
        if isinstance(value, Mapping):
            return {
                str(key): cls._json_safe(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [cls._json_safe(item) for item in value]
        raise ArchivePilotInvariantError(
            f"archive metadata contains non-JSON value {type(value).__name__}"
        )

    @staticmethod
    def _hash_payload(payload: Mapping[str, object]) -> str:
        """Hash one deterministic normalized public-authority payload."""

        return hashlib.sha256(canonicalize(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _metadata(**values: str) -> tuple[tuple[str, str], ...]:
        """Return sorted unique metadata pairs."""

        return tuple(sorted(values.items()))

    @staticmethod
    def _format_score(value: float) -> str:
        """Format one finite diagnostic score deterministically."""

        return format(float(value), ".9g")

    @staticmethod
    def _enum_value(value: object) -> str:
        """Return an enum's string value or a stable string fallback."""

        raw = getattr(value, "value", value)
        return str(raw)

    def _ensure_open(self) -> None:
        """Reject reads after lifecycle close."""

        if self._closed:
            raise ArchivePilotInvariantError("archive pilot client is closed")

    def _require_service(self) -> object:
        """Return the initialized service or fail loudly."""

        if self._service is None:
            raise ArchivePilotInvariantError("archive service is unavailable")
        return self._service

    def _require_primary(self) -> object:
        """Return the initialized primary ProjectRecorder."""

        if self._primary is None:
            raise ArchivePilotInvariantError("primary project recorder is unavailable")
        return self._primary

    def _require_foreign(self) -> object:
        """Return the initialized foreign ProjectRecorder."""

        if self._foreign is None:
            raise ArchivePilotInvariantError("foreign project recorder is unavailable")
        return self._foreign


__all__ = [
    "GRAPH_QUERY",
    "LEXICAL_QUERY",
    "SCOPE_QUERY",
    "TEMPORAL_QUERY",
    "ArchiveFixtureManifest",
    "ArchivePilotInvariantError",
    "ArchivePilotUnavailable",
    "KSAProjectReadClient",
    "archive_dependency_available",
]
