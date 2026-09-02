"""Read-only archive episode substrate for Cherry environment search.

Source: knowledge-semantic-archive ProjectRecorder public read surface, adapted
    through a local Protocol so Cherry has no import-time archive dependency.
Integrated: 2026-07-14
Purpose: Expose frozen signed-archive evidence to Tier-T search while keeping the
    only reversible state in an episode evidence ledger. Every accepted action
    checks the archive's canonical fingerprint before and after execution.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol, Sequence, runtime_checkable

from ...core.errors import SnapshotError, SoundnessError, ValidationError
from ...core.jcs import canonicalize
from ...core.types import (
    ActionCandidate,
    Cost,
    EffectClass,
    EnvDigest,
    Observation,
    SnapshotHandle,
)
from ..base import TransactionalSubstrateBase

ArchiveChannel = Literal["lexical", "graph", "temporal", "recent", "context"]

_READ_ACTIONS = frozenset(
    {
        "archive.search",
        "archive.explore_knowledge_graph",
        "archive.search_conversation_messages",
        "archive.get_recent",
        "archive.build_context",
    }
)
_CANONICAL_MUTATIONS = frozenset(
    {
        "archive.remember",
        "archive.ingest_conversation",
        "archive.ingest_document",
        "archive.link_records",
        "archive.delete",
    }
)
_SELECT_ACTION = "episode.select_evidence"
_ORACLE_WRITE_ACTION = "episode.oracle.write"


@dataclass(frozen=True, slots=True)
class ArchiveEvidence:
    """One immutable, authority-attributed evidence item returned by the client.

    Args:
        channel: Retrieval lane that produced the evidence.
        source_id: Stable canonical record, message, edge, or context identifier.
        content: Bounded text made available to the search episode.
        canonical_hash: Optional signed-record/content hash supplied by the bridge.
        metadata: Sorted string pairs with bounded provenance details.
    """

    channel: ArchiveChannel
    source_id: str
    content: str
    canonical_hash: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValidationError("ArchiveEvidence.source_id must be non-empty")
        if not isinstance(self.content, str):
            raise ValidationError("ArchiveEvidence.content must be a string")
        if self.canonical_hash is not None and not self.canonical_hash:
            raise ValidationError("ArchiveEvidence.canonical_hash must be non-empty when present")
        if tuple(sorted(self.metadata)) != self.metadata:
            raise ValidationError("ArchiveEvidence.metadata must be sorted deterministically")
        keys = tuple(key for key, _value in self.metadata)
        if len(set(keys)) != len(keys):
            raise ValidationError("ArchiveEvidence.metadata keys must be unique")

    @property
    def evidence_id(self) -> str:
        """Return the deterministic identity used by the episode ledger."""

        payload = canonicalize(self.to_payload(include_id=False)).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:24]

    def to_payload(self, *, include_id: bool = True) -> dict[str, object]:
        """Return a JSON-canonical payload suitable for an Observation.

        Args:
            include_id: Include the derived evidence identifier when true.

        Returns:
            Plain JSON-compatible evidence data.
        """

        payload: dict[str, object] = {
            "channel": self.channel,
            "source_id": self.source_id,
            "content": self.content,
            "canonical_hash": self.canonical_hash,
            "metadata": [[key, value] for key, value in self.metadata],
        }
        if include_id:
            payload["evidence_id"] = self.evidence_id
        return payload


@dataclass(frozen=True, slots=True)
class ArchiveEvidenceResult:
    """Immutable normalized result returned by an :class:`ArchiveReadClient`."""

    channel: ArchiveChannel
    evidence: tuple[ArchiveEvidence, ...]
    summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str):
            raise ValidationError("ArchiveEvidenceResult.summary must be a string")
        identifiers: set[str] = set()
        for item in self.evidence:
            if item.channel != self.channel:
                raise ValidationError(
                    f"evidence channel {item.channel!r} does not match result {self.channel!r}"
                )
            if item.evidence_id in identifiers:
                raise ValidationError(
                    f"duplicate evidence id {item.evidence_id!r} in one archive result"
                )
            identifiers.add(item.evidence_id)

    def to_payload(self) -> dict[str, object]:
        """Return deterministic JSON-compatible result data."""

        return {
            "channel": self.channel,
            "summary": self.summary,
            "evidence": [item.to_payload() for item in self.evidence],
        }


@runtime_checkable
class ArchiveReadClient(Protocol):
    """Synchronous bridge contract over a frozen archive/ProjectRecorder.

    A later async bridge may own an event-loop thread, but this substrate remains
    synchronous so it can satisfy Cherry's frozen ExecutionSubstrate contract.
    Implementations must normalize native archive objects into
    :class:`ArchiveEvidenceResult`.
    """

    def canonical_fingerprint(self) -> str:
        """Return a stable fingerprint of signed canonical archive state."""
        ...

    def search(self, query: str, *, limit: int) -> ArchiveEvidenceResult:
        """Run deterministic lexical archive search."""
        ...

    def explore_knowledge_graph(
        self,
        query: str,
        *,
        limit: int,
        graph_limit: int,
        hop_depth: int,
    ) -> ArchiveEvidenceResult:
        """Return authenticated graph evidence without canonical mutation."""
        ...

    def search_conversation_messages(
        self,
        query: str,
        *,
        limit: int,
    ) -> ArchiveEvidenceResult:
        """Return authenticated granular temporal-message evidence."""
        ...

    def get_recent(self, *, limit: int) -> ArchiveEvidenceResult:
        """Return recent authenticated records."""
        ...

    def build_context(
        self,
        query: str,
        *,
        limit: int,
        max_tokens: int,
    ) -> ArchiveEvidenceResult:
        """Return bounded prompt-context evidence."""
        ...


@dataclass(frozen=True, slots=True)
class EpisodeEvidenceLedger:
    """The complete reversible state owned by an archive search episode."""

    observed: tuple[ArchiveEvidence, ...] = ()
    selected_ids: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        """Return deterministic JSON-compatible ledger data."""

        return {
            "observed": [item.to_payload() for item in self.observed],
            "selected_ids": list(self.selected_ids),
        }


class ArchiveEpisodeSubstrate(TransactionalSubstrateBase):
    """Tier-T adapter over frozen archive reads and a reversible evidence ledger.

    The signed archive is never snapshotted or restored by Cherry. Archive reads
    append normalized evidence to the episode ledger, so they are classified
    WRITE_REVERSIBLE even though the underlying archive operation is read-only.
    Canonical archive mutations and protected-oracle writes are rejected by the
    base class effect gate before client code can run.

    Args:
        client: Synchronous read bridge; no archive package import is required.
        oracle_evidence_ids: Protected expected evidence identifiers for verifiers.
        substrate_id: Opaque identity embedded in snapshot handles.
    """

    def __init__(
        self,
        client: ArchiveReadClient,
        *,
        oracle_evidence_ids: Sequence[str] = (),
        substrate_id: str = "archive_episode",
    ) -> None:
        if not isinstance(client, ArchiveReadClient):
            raise ValidationError("client does not satisfy ArchiveReadClient")
        if not substrate_id:
            raise ValidationError("substrate_id must be non-empty")
        oracle = tuple(sorted(set(oracle_evidence_ids)))
        if any(not isinstance(item, str) or not item for item in oracle):
            raise ValidationError("oracle_evidence_ids must contain non-empty strings")
        self._client = client
        self._id = substrate_id
        self._oracle_evidence_ids = oracle
        self._archive_fingerprint = self._initial_fingerprint()
        self._ledger = EpisodeEvidenceLedger()
        self._snapshots: dict[str, EpisodeEvidenceLedger] = {}
        self._seq = 0

    @property
    def ledger(self) -> EpisodeEvidenceLedger:
        """Return the immutable episode evidence ledger."""

        return self._ledger

    @property
    def oracle_evidence_ids(self) -> tuple[str, ...]:
        """Return protected verifier reference ids outside the action-write surface."""

        return self._oracle_evidence_ids

    @property
    def archive_fingerprint(self) -> str:
        """Return the canonical fingerprint frozen at episode construction."""

        return self._archive_fingerprint

    def selected_evidence(self) -> tuple[ArchiveEvidence, ...]:
        """Return selected evidence in deterministic ledger order."""

        selected = frozenset(self._ledger.selected_ids)
        return tuple(item for item in self._ledger.observed if item.evidence_id in selected)

    def effect_class(self, a: ActionCandidate) -> EffectClass:
        """Classify accepted reads/ledger selection and reject mutation surfaces."""

        if a.tool_id in _READ_ACTIONS or a.tool_id == _SELECT_ACTION:
            return EffectClass.WRITE_REVERSIBLE
        if a.tool_id in _CANONICAL_MUTATIONS or a.tool_id == _ORACLE_WRITE_ACTION:
            return EffectClass.WRITE_IRREVERSIBLE
        raise ValidationError(
            f"unknown archive-episode tool {a.tool_id!r}; known reads: "
            f"{sorted(_READ_ACTIONS)}, local: {_SELECT_ACTION!r}"
        )

    def snapshot(self) -> SnapshotHandle:
        """Capture the immutable evidence ledger in the snapshot table."""

        self._assert_archive_frozen("snapshot")
        self._seq += 1
        token = f"archive-{self._seq}"
        self._snapshots[token] = self._ledger
        return SnapshotHandle(substrate_id=self._id, token=token, seq=self._seq)

    def restore(self, h: SnapshotHandle) -> None:
        """Restore any captured ancestor ledger from an arbitrary descendant."""

        self._assert_archive_frozen("restore-before")
        if h.substrate_id != self._id or h.token not in self._snapshots:
            raise SnapshotError(f"handle {h!r} unknown to substrate {self._id!r}")
        self._ledger = self._snapshots[h.token]
        self._assert_archive_frozen("restore-after")

    def digest(self) -> EnvDigest:
        """Hash only the frozen archive identity and reversible episode state."""

        payload = {
            "archive_fingerprint": self._archive_fingerprint,
            "ledger": self._ledger.to_payload(),
        }
        encoded = canonicalize(payload).encode("utf-8")
        return EnvDigest(hashlib.sha256(encoded).hexdigest())

    def snapshot_cost_estimate(self) -> Cost:
        """Declare the immutable-ledger snapshot's bounded local copy cost."""

        return Cost(wall_ms=0.001 * max(1, len(self._ledger.observed)))

    def _do_execute(self, a: ActionCandidate) -> tuple[Observation, Cost]:
        """Execute an already-gated read or episode-local selection."""

        t0 = time.perf_counter()
        self._assert_archive_frozen("action-before")
        if a.tool_id == _SELECT_ACTION:
            try:
                observation = self._select_evidence(a)
            finally:
                self._assert_archive_frozen("action-after")
            return observation, self._cost_since(t0, env_calls=0)

        try:
            result = self._execute_read(a)
        finally:
            self._assert_archive_frozen("action-after")
        self._append_evidence(result)
        observation = Observation(
            kind="result",
            payload={
                "archive_fingerprint": self._archive_fingerprint,
                "result": result.to_payload(),
                "ledger_digest": str(self.digest()),
            },
        )
        return observation, self._cost_since(t0, env_calls=1)

    def _execute_read(self, a: ActionCandidate) -> ArchiveEvidenceResult:
        """Validate arguments and dispatch one synchronous client read."""

        args = a.args
        if a.tool_id == "archive.search":
            result = self._client.search(
                self._query(args),
                limit=self._positive_int(args, "limit", default=10, maximum=100),
            )
            return self._validate_result(result, "lexical")
        if a.tool_id == "archive.explore_knowledge_graph":
            result = self._client.explore_knowledge_graph(
                self._query(args),
                limit=self._positive_int(args, "limit", default=10, maximum=100),
                graph_limit=self._positive_int(args, "graph_limit", default=100, maximum=500),
                hop_depth=self._positive_int(args, "hop_depth", default=2, maximum=4),
            )
            return self._validate_result(result, "graph")
        if a.tool_id == "archive.search_conversation_messages":
            result = self._client.search_conversation_messages(
                self._query(args),
                limit=self._positive_int(args, "limit", default=10, maximum=100),
            )
            return self._validate_result(result, "temporal")
        if a.tool_id == "archive.get_recent":
            result = self._client.get_recent(
                limit=self._positive_int(args, "limit", default=10, maximum=100)
            )
            return self._validate_result(result, "recent")
        if a.tool_id == "archive.build_context":
            result = self._client.build_context(
                self._query(args),
                limit=self._positive_int(args, "limit", default=10, maximum=100),
                max_tokens=self._positive_int(args, "max_tokens", default=2_000, maximum=100_000),
            )
            return self._validate_result(result, "context")
        raise ValidationError(f"unhandled archive read {a.tool_id!r}")

    def _append_evidence(self, result: ArchiveEvidenceResult) -> None:
        """Append first-seen evidence while preserving deterministic order."""

        observed = list(self._ledger.observed)
        identifiers = {item.evidence_id for item in observed}
        for item in result.evidence:
            if item.evidence_id not in identifiers:
                observed.append(item)
                identifiers.add(item.evidence_id)
        self._ledger = EpisodeEvidenceLedger(
            observed=tuple(observed),
            selected_ids=self._ledger.selected_ids,
        )

    def _select_evidence(self, a: ActionCandidate) -> Observation:
        """Select only evidence previously observed through the frozen client."""

        raw_ids = a.args.get("evidence_ids")
        if (
            not isinstance(raw_ids, Sequence)
            or isinstance(raw_ids, (str, bytes, bytearray))
            or not raw_ids
        ):
            raise ValidationError(
                "episode.select_evidence requires a non-empty sequence 'evidence_ids'"
            )
        requested: list[str] = []
        for item in raw_ids:
            if not isinstance(item, str) or not item:
                raise ValidationError("selected evidence ids must be non-empty strings")
            if item not in requested:
                requested.append(item)

        available = {item.evidence_id: item for item in self._ledger.observed}
        missing = [item for item in requested if item not in available]
        if missing:
            raise ValidationError(f"cannot select evidence not observed in this episode: {missing}")
        selected = list(self._ledger.selected_ids)
        for item in requested:
            if item not in selected:
                selected.append(item)
        self._ledger = EpisodeEvidenceLedger(
            observed=self._ledger.observed,
            selected_ids=tuple(selected),
        )
        return Observation(
            kind="result",
            payload={
                "selected_ids": list(self._ledger.selected_ids),
                "selected": [available[item].to_payload() for item in requested],
                "ledger_digest": str(self.digest()),
            },
        )

    def _initial_fingerprint(self) -> str:
        """Read and validate the episode's canonical archive identity."""

        value = self._client.canonical_fingerprint()
        if not isinstance(value, str) or not value:
            raise ValidationError(
                "ArchiveReadClient.canonical_fingerprint() must return a non-empty string"
            )
        return value

    def _assert_archive_frozen(self, phase: str) -> None:
        """Hard-fail when canonical archive state diverges during the episode."""

        current = self._client.canonical_fingerprint()
        if not isinstance(current, str) or current != self._archive_fingerprint:
            raise SoundnessError(
                f"canonical archive fingerprint diverged at {phase}; "
                "the read-only episode is no longer sound"
            )

    @staticmethod
    def _query(args: Mapping[str, object]) -> str:
        """Return a validated non-empty query string."""

        value = args.get("query")
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("archive read requires a non-empty string 'query'")
        return value

    @staticmethod
    def _positive_int(
        args: Mapping[str, object],
        name: str,
        *,
        default: int,
        maximum: int,
    ) -> int:
        """Return one bounded positive integer argument."""

        value = args.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValidationError(f"{name} must be an integer in [1, {maximum}], got {value!r}")
        return value

    @staticmethod
    def _validate_result(
        result: ArchiveEvidenceResult,
        expected_channel: ArchiveChannel,
    ) -> ArchiveEvidenceResult:
        """Require normalized immutable evidence on the expected retrieval lane."""

        if not isinstance(result, ArchiveEvidenceResult):
            raise ValidationError("ArchiveReadClient methods must return ArchiveEvidenceResult")
        if result.channel != expected_channel:
            raise ValidationError(
                f"client returned channel {result.channel!r}; expected {expected_channel!r}"
            )
        return result

    @staticmethod
    def _cost_since(t0: float, *, env_calls: int) -> Cost:
        """Build the uncollapsed vector cost for one adapter transition."""

        return Cost(
            wall_ms=(time.perf_counter() - t0) * 1000.0,
            env_calls=env_calls,
            risk=0.0,
        )


__all__ = [
    "ArchiveChannel",
    "ArchiveEpisodeSubstrate",
    "ArchiveEvidence",
    "ArchiveEvidenceResult",
    "ArchiveReadClient",
    "EpisodeEvidenceLedger",
]
