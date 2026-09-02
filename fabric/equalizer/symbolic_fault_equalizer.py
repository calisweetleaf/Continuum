#!/usr/bin/env python3
"""Symbolic fault equalizer for fabric-resident action execution.

Source: Morpheus GPT-4o real_time_interrupt_handler.py (historical artifact)
Integrated: 2026-08-25
Purpose: Sit under cognition as a model-independent equalizer. The primitive is
equalize(action, state), not handle_interrupt(generation). Anything that enters
is normalized, routed, executed, validated, and either committed or equalized
until the next experienced state is admissible. The model never observes
mechanism failure.

Lineage. What survived from the interrupt fossil: asynchronous event
interception; priority-ordered events; context identity; suspend and resume;
state capture; rollback hooks; callback and handler registration; atomic return
to a valid prior state; heartbeat and memory-pressure as discontinuity families.

What was corrected: GPT-4o token and KV-cache authority; hardcoded modality
meanings; safety-alert as a privileged handler; model-owned IDLE/GENERATING
state; exception-log-drop-restore as the failure path. Those are now symbols
and repair operators. Failure is one event family among all state
discontinuities.

Invariant. No invalid transition is allowed to become the next experienced
state. Hide mechanism failure from cognition; never hide causal truth from
the substrate.

This module does not import EXHUMA, LICHE, CONTINUUM, or the attention fabric.
Those systems may register capabilities, handlers, or a commit sink.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import os
import sqlite3
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol, Sequence

logger = logging.getLogger("somnus.equalizer")
logger.addHandler(logging.NullHandler())

MAX_CONTEXTS = 8
MAX_EQUALIZATION_STEPS = 8
MAX_TIMING_SAMPLES = 1000
HANDLE_INTERVAL_S = 0.01
HEARTBEAT_INTERVAL_S = 0.25
JSON_RECORD_VERSION = 1
FIELD_ALIASES = {
    "path": "target",
    "file": "target",
    "locator": "target",
    "filename": "target",
    "body": "contents",
    "data": "contents",
    "text": "contents",
    "payload": "contents",
}
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")
HANDLER_ERRORS = (
    ArithmeticError,
    AttributeError,
    LookupError,
    OSError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
)


class EqualizerError(Exception):
    """Base domain error for the symbolic fault equalizer."""

    default_remediation = "Correct the action, context, or capability named in the error."

    def __init__(self, message: str, remediation: str | None = None) -> None:
        """Initialize an equalizer error with operator remediation.

        Args:
            message: What failed at the contract boundary.
            remediation: What to do next. Uses the class default when omitted.
        """
        self.message = message
        self.remediation = remediation or self.default_remediation
        super().__init__(message)


class InvalidActionError(EqualizerError):
    """Raised when an ingress cannot be coerced into a symbolic action."""

    default_remediation = "Supply a verb and target, or a filesystem path the equalizer can READ."


class UnknownCapabilityError(EqualizerError):
    """Raised when a verb has no registered capability and no equivalent route."""

    default_remediation = "Register a capability for the verb or provide equivalent_verbs."


class WitnessStoreError(EqualizerError):
    """Raised when the durable witness runtime cannot be read or written."""

    default_remediation = "Check the witness sqlite path and that the process can write it."


class EventPriority(IntEnum):
    """Priority for discontinuity handling. Lower integer is handled first."""

    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3
    BACKGROUND = 4


class EventFamily(str, Enum):
    """Families of fabric discontinuities. Fault is one family among them."""

    ACTION = "action"
    FAULT = "fault"
    PREEMPT = "preempt"
    HEARTBEAT = "heartbeat"
    MEMORY_PRESSURE = "memory_pressure"
    CONTEXT_SWITCH = "context_switch"
    EXTERNAL = "external"
    COMMIT = "commit"
    ROLLBACK = "rollback"


class FaultSymbol(str, Enum):
    """Deterministic fault vocabulary. Never a prompt for a language model."""

    MALFORMED_SCHEMA = "MALFORMED_SCHEMA"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_ENCODING = "INVALID_ENCODING"
    STALE_HANDLE = "STALE_HANDLE"
    STALE_STATE = "STALE_STATE"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    ADMISSIBILITY_VIOLATION = "ADMISSIBILITY_VIOLATION"
    MEMORY_PRESSURE = "MEMORY_PRESSURE"
    CONTEXT_DISCONTINUITY = "CONTEXT_DISCONTINUITY"


class RepairOperator(str, Enum):
    """Deterministic operators that map from fault symbols."""

    NORMALIZE_SCHEMA = "NORMALIZE_SCHEMA"
    INFER_IF_UNAMBIGUOUS = "INFER_IF_UNAMBIGUOUS"
    RECONSTRUCT = "RECONSTRUCT"
    REVALIDATE = "REVALIDATE"
    ROUTE_EQUIVALENT = "ROUTE_EQUIVALENT"
    ROLLBACK = "ROLLBACK"
    RETRY = "RETRY"
    RESTORE_AUTHORITY = "RESTORE_AUTHORITY"
    REBASE = "REBASE"
    REEXECUTE = "REEXECUTE"
    SUBSTITUTE = "SUBSTITUTE"
    RESUBMIT = "RESUBMIT"
    EVICT_INACTIVE = "EVICT_INACTIVE"
    DECODE_ENCODING = "DECODE_ENCODING"
    CREATE_PARENT = "CREATE_PARENT"


class SubstrateState(str, Enum):
    """Fabric-owned execution state. ERROR is not a model-visible state."""

    STABLE = "stable"
    TRANSITIONING = "transitioning"
    EQUALIZING = "equalizing"
    SUSPENDED = "suspended"


REUSABLE_OPERATORS = {
    RepairOperator.DECODE_ENCODING,
    RepairOperator.RETRY,
    RepairOperator.REEXECUTE,
    RepairOperator.CREATE_PARENT,
    RepairOperator.ROLLBACK,
}

OPERATOR_TABLE: Mapping[FaultSymbol, tuple[RepairOperator, ...]] = {
    FaultSymbol.MALFORMED_SCHEMA: (RepairOperator.NORMALIZE_SCHEMA, RepairOperator.REVALIDATE),
    FaultSymbol.MISSING_FIELD: (
        RepairOperator.INFER_IF_UNAMBIGUOUS,
        RepairOperator.RECONSTRUCT,
        RepairOperator.REVALIDATE,
    ),
    FaultSymbol.INVALID_ENCODING: (RepairOperator.DECODE_ENCODING, RepairOperator.REEXECUTE),
    FaultSymbol.STALE_HANDLE: (RepairOperator.RESTORE_AUTHORITY, RepairOperator.REBASE, RepairOperator.REEXECUTE),
    FaultSymbol.STALE_STATE: (RepairOperator.RESTORE_AUTHORITY, RepairOperator.REBASE, RepairOperator.REEXECUTE),
    FaultSymbol.CAPABILITY_UNAVAILABLE: (RepairOperator.ROUTE_EQUIVALENT, RepairOperator.SUBSTITUTE),
    FaultSymbol.TRANSIENT_FAILURE: (RepairOperator.ROLLBACK, RepairOperator.CREATE_PARENT, RepairOperator.RETRY),
    FaultSymbol.ADMISSIBILITY_VIOLATION: (RepairOperator.ROLLBACK, RepairOperator.RECONSTRUCT, RepairOperator.REEXECUTE),
    FaultSymbol.MEMORY_PRESSURE: (RepairOperator.EVICT_INACTIVE,),
    FaultSymbol.CONTEXT_DISCONTINUITY: (RepairOperator.RESTORE_AUTHORITY, RepairOperator.RESUBMIT),
}


@dataclass(order=True, slots=True)
class CausalEvent:
    """Queued fabric discontinuity. Generalized from InterruptRequest."""

    priority: EventPriority
    timestamp: float
    ingress_seq: int
    event_id: str = field(compare=False)
    family: EventFamily = field(compare=False)
    symbol: str = field(compare=False)
    context_id: str = field(compare=False)
    sequence_id: int = field(compare=False)
    source: str = field(compare=False)
    target: str = field(compare=False)
    payload: Mapping[str, str] = field(compare=False, default_factory=dict)
    handled: bool = field(compare=False, default=False)


@dataclass(frozen=True, slots=True)
class SymbolicAction:
    """Model-visible affordance. Verb semantics are laws, not retry advice."""

    action_id: str
    verb: str
    target: str
    payload: Mapping[str, str]
    context_id: str
    equivalent_verbs: tuple[str, ...] = ()
    encoding: str = "utf-8"


@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    """What cognition is allowed to see. Mechanism failure is not in this type."""

    verb: str
    target: str
    kind: Literal["value", "absent"]
    body: str
    digest: str
    stability: float


@dataclass(frozen=True, slots=True)
class WitnessStep:
    """One golden-path record. Substrate truth, never a model prompt."""

    index: int
    stage: str
    symbol: str
    operator: str
    detail: str
    elapsed_ms: float


@dataclass(frozen=True, slots=True)
class FabricWitness:
    """ACK of equalization. The world happened this way; cognition did not babysit it."""

    witness_id: str
    action_id: str
    context_id: str
    recorded_at: float
    outcome: Literal["valid", "equalized_valid", "lawful_absent"]
    steps: tuple[WitnessStep, ...]
    digest: str
    equalization_count: int

    def as_dict(self) -> dict[str, object]:
        """Serialize the witness for durable storage.

        Returns:
            A JSON-safe record. Values are schema-shaped mixed types.
        """
        return {
            "witness_id": self.witness_id,
            "action_id": self.action_id,
            "context_id": self.context_id,
            "recorded_at": self.recorded_at,
            "outcome": self.outcome,
            "digest": self.digest,
            "equalization_count": self.equalization_count,
            "record_version": JSON_RECORD_VERSION,
            "steps": [
                {
                    "index": step.index,
                    "stage": step.stage,
                    "symbol": step.symbol,
                    "operator": step.operator,
                    "detail": step.detail,
                    "elapsed_ms": step.elapsed_ms,
                }
                for step in self.steps
            ],
        }


@dataclass(frozen=True, slots=True)
class EqualizedTransition:
    """Model-facing result of T_B. Status is always committed."""

    status: Literal["committed"]
    context_id: str
    action_id: str
    observation: CanonicalObservation
    witness: FabricWitness
    state_digest: str


@dataclass(frozen=True, slots=True)
class ExecutionCandidate:
    """Result of one capability attempt before admissibility is decided."""

    status: Literal["candidate", "fault"]
    verb: str
    target: str
    result_kind: Literal["value", "absent", "fault"]
    body: str
    digest: str
    fault: FaultSymbol | None
    fault_detail: str
    files_touched: tuple[str, ...]
    created_paths: tuple[str, ...]


@dataclass
class CausalSnapshot:
    """Captured substrate state that rollback can restore exactly."""

    snapshot_id: str
    context_id: str
    captured_at: float
    authority_epoch: int
    sequence_id: int
    metadata: dict[str, str]
    file_bytes: dict[str, bytes]
    created_paths: list[str]


@dataclass
class CausalContext:
    """Persistent causal identity that can be suspended and resumed."""

    context_id: str
    sequence_id: int = 0
    authority_epoch: int = 1
    is_suspended: bool = False
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, str] = field(default_factory=dict)
    last_target: str = ""
    last_digest: str = ""
    last_encoding: str = "utf-8"
    last_bytes: bytes = b""
    snapshot: CausalSnapshot | None = None

    def suspend(self) -> None:
        """Mark this context inactive while preserving captured state."""
        self.is_suspended = True

    def resume(self) -> None:
        """Reactivate a parked context and refresh its recency timestamp."""
        self.is_suspended = False
        self.timestamp = time.time()


@dataclass(frozen=True, slots=True)
class TimingStats:
    """Percentile summary over a bounded timing window."""

    mean: float
    median: float
    p95: float
    p99: float
    maximum: float

    def as_dict(self) -> dict[str, float]:
        """Serialize timing tails for metrics surfaces.

        Returns:
            Named mean, median, percentile, and max fields.
        """
        return {
            "mean": self.mean,
            "median": self.median,
            "p95": self.p95,
            "p99": self.p99,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class EqualizerMetrics:
    """Structured performance surface. Not a bare dict."""

    received: int
    committed: int
    equalized: int
    lawful_absent: int
    dropped: int
    context_switches: int
    queue_depth: int
    active_context: str
    context_count: int
    latency: TimingStats
    handling: TimingStats
    recovery: TimingStats
    context_switch: TimingStats
    by_family: Mapping[str, int]
    by_symbol: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class PathEqualizationSummary:
    """Bounded filesystem execution used by the harness describe_path gate."""

    status: str
    normalized_path: str
    observation_kind: str
    stability: float
    equalization_count: int
    witness_id: str
    digest: str


class CommitSink(Protocol):
    """Optional fabric peg. Attention or continuum may accept committed transitions."""

    def accept(self, transition: EqualizedTransition) -> None:
        """Receive a committed transition without altering its observation.

        Args:
            transition: The equalized, already-committed causal step.
        """


class Capability(Protocol):
    """Native affordance. Returns a candidate or a fault symbol, never a traceback."""

    name: str
    verbs: tuple[str, ...]

    def perform(self, action: SymbolicAction, snapshot: CausalSnapshot) -> ExecutionCandidate:
        """Execute the action against live substrate.

        Args:
            action: Normalized symbolic action.
            snapshot: Pre-execution state used for stale-handle detection.

        Returns:
            A candidate transition or a symbolic fault.
        """


def _digest_text(body: str) -> str:
    """Return a hex sha256 of utf-8 text."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _digest_bytes(payload: bytes) -> str:
    """Return a hex sha256 of raw bytes."""
    return hashlib.sha256(payload).hexdigest()


def _percentile(samples: Sequence[float], percent: float) -> float:
    """Return a nearest-rank percentile from a finite sample window."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round((percent / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def _timing_stats(samples: Sequence[float]) -> TimingStats:
    """Collapse a timing window into mean, median, tails, and max."""
    if not samples:
        return TimingStats(mean=0.0, median=0.0, p95=0.0, p99=0.0, maximum=0.0)
    return TimingStats(
        mean=float(statistics.fmean(samples)),
        median=float(statistics.median(samples)),
        p95=_percentile(samples, 95.0),
        p99=_percentile(samples, 99.0),
        maximum=float(max(samples)),
    )


def _bounded_append(samples: list[float], value: float) -> None:
    """Append a timing sample and drop the oldest past the window."""
    samples.append(value)
    if len(samples) > MAX_TIMING_SAMPLES:
        del samples[0]


def _json_object(text: str) -> Mapping[str, object] | None:
    """Parse JSON object text. Returns None when the text is not an object."""
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        logger.error("JSON object parse failed during equalization coerce")
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _stringify_payload(raw: Mapping[str, object]) -> dict[str, str]:
    """Coerce mixed JSON values into a string map for symbolic actions."""
    converted: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            converted[str(key)] = value
        elif value is None:
            converted[str(key)] = ""
        elif isinstance(value, (int, float, bool)):
            converted[str(key)] = str(value)
        else:
            converted[str(key)] = json.dumps(value, sort_keys=True)
    return converted


def _alias_fields(payload: Mapping[str, str]) -> dict[str, str]:
    """Rewrite known schema aliases onto canonical action fields."""
    rewritten = dict(payload)
    for alias, canonical in FIELD_ALIASES.items():
        if alias in rewritten and canonical not in rewritten:
            rewritten[canonical] = rewritten[alias]
    return rewritten


class ActionNormalizer:
    """Stage N: sanitization. Malformed ingress becomes a symbolic action or a fault."""

    def coerce(
        self,
        ingress: SymbolicAction | Mapping[str, object] | str | bytes,
        context: CausalContext,
    ) -> tuple[SymbolicAction | None, FaultSymbol | None, str]:
        """Turn arbitrary ingress into a SymbolicAction without asking a model.

        Args:
            ingress: Action object, mapping, JSON text, bytes, or a filesystem path.
            context: Active context used for unambiguous field inference.

        Returns:
            The action, an optional fault symbol, and a witness detail string.
        """
        if isinstance(ingress, SymbolicAction):
            if ingress.verb and ingress.target:
                return ingress, None, "ingress_already_symbolic"
            if ingress.verb and not ingress.target and context.last_target:
                filled = SymbolicAction(
                    action_id=ingress.action_id,
                    verb=ingress.verb,
                    target=context.last_target,
                    payload=dict(ingress.payload),
                    context_id=ingress.context_id or context.context_id,
                    equivalent_verbs=ingress.equivalent_verbs,
                    encoding=ingress.encoding,
                )
                return filled, None, "inferred_target_from_context"
            return None, FaultSymbol.MISSING_FIELD, "symbolic_action_missing_target"
        if isinstance(ingress, bytes):
            decoded, encoding_used = self._decode_bytes(ingress)
            if decoded is None:
                return None, FaultSymbol.INVALID_ENCODING, "bytes_ingress_undecodable"
            action, fault, detail = self.coerce(decoded, context)
            if action is not None and encoding_used != "utf-8":
                action = SymbolicAction(
                    action_id=action.action_id,
                    verb=action.verb,
                    target=action.target,
                    payload=action.payload,
                    context_id=action.context_id,
                    equivalent_verbs=action.equivalent_verbs,
                    encoding=encoding_used,
                )
            return action, fault, detail
        if isinstance(ingress, str):
            return self._coerce_text(ingress, context)
        if isinstance(ingress, Mapping):
            return self._coerce_mapping(ingress, context)
        return None, FaultSymbol.MALFORMED_SCHEMA, "unsupported_ingress_type"

    def _coerce_text(self, text: str, context: CausalContext) -> tuple[SymbolicAction | None, FaultSymbol | None, str]:
        """Coerce JSON text or a bare path into a symbolic action."""
        stripped = text.strip()
        parsed = _json_object(stripped)
        if parsed is not None:
            action, fault, detail = self._coerce_mapping(parsed, context)
            if action is not None:
                return action, fault, "normalized_json_text"
            return action, fault, detail
        if stripped.startswith("{") or stripped.startswith("["):
            return None, FaultSymbol.MALFORMED_SCHEMA, "json_text_not_an_object"
        path = Path(stripped).expanduser()
        if path.exists() or context.last_target:
            target = str(path) if stripped else context.last_target
            verb = "LIST" if path.is_dir() else "READ"
            action = SymbolicAction(
                action_id=uuid.uuid4().hex,
                verb=verb,
                target=target,
                payload={"target": target},
                context_id=context.context_id,
            )
            return action, None, "inferred_path_ingress"
        return None, FaultSymbol.MALFORMED_SCHEMA, "text_ingress_not_json_or_path"

    def _coerce_mapping(
        self,
        raw: Mapping[str, object],
        context: CausalContext,
    ) -> tuple[SymbolicAction | None, FaultSymbol | None, str]:
        """Coerce a JSON-shaped mapping onto the canonical action schema."""
        payload = _alias_fields(_stringify_payload(raw))
        verb = payload.get("verb", "").upper() or payload.get("action", "").upper()
        target = payload.get("target", "")
        if not verb and target:
            path = Path(target)
            verb = "LIST" if path.is_dir() else "READ"
        if not verb:
            return None, FaultSymbol.MISSING_FIELD, "mapping_missing_verb"
        if not target and context.last_target:
            target = context.last_target
        if not target:
            return None, FaultSymbol.MISSING_FIELD, "mapping_missing_target"
        equivalents_raw = payload.get("equivalent_verbs", "")
        equivalents = tuple(item.strip() for item in equivalents_raw.split(",") if item.strip()) if equivalents_raw else ()
        action = SymbolicAction(
            action_id=payload.get("action_id") or uuid.uuid4().hex,
            verb=verb,
            target=target,
            payload=payload,
            context_id=payload.get("context_id") or context.context_id,
            equivalent_verbs=equivalents,
            encoding=payload.get("encoding") or "utf-8",
        )
        return action, None, "normalized_mapping"

    def _decode_bytes(self, payload: bytes) -> tuple[str | None, str]:
        """Decode bytes across the known encoding cascade."""
        for encoding in TEXT_ENCODINGS:
            try:
                return payload.decode(encoding), encoding
            except UnicodeDecodeError:
                logger.error("Byte ingress rejected encoding %s during coerce", encoding)
                continue
        return None, ""


class FileCapability:
    """Native file READ, WRITE, and LIST. Files stay files."""

    name = "file"
    verbs = ("READ", "WRITE", "LIST")

    def perform(self, action: SymbolicAction, snapshot: CausalSnapshot) -> ExecutionCandidate:
        """Read, write, or list a real filesystem path.

        Args:
            action: Normalized file action.
            snapshot: Pre-execution bytes used to detect stale handles.

        Returns:
            A candidate or a symbolic fault against the live path.
        """
        path = Path(action.target).expanduser()
        if action.verb == "READ":
            return self._read(action, path, snapshot)
        if action.verb == "WRITE":
            return self._write(action, path)
        if action.verb == "LIST":
            return self._list(action, path)
        return self._fault(action, FaultSymbol.CAPABILITY_UNAVAILABLE, f"file_capability_rejects_verb_{action.verb}")

    def _read(self, action: SymbolicAction, path: Path, snapshot: CausalSnapshot) -> ExecutionCandidate:
        """Read native file bytes and decode with the action encoding."""
        if not path.exists():
            if action.target in snapshot.file_bytes or snapshot.metadata.get("last_target") == action.target:
                return self._fault(action, FaultSymbol.STALE_HANDLE, "path_missing_but_snapshot_has_bytes")
            return ExecutionCandidate(
                status="candidate",
                verb=action.verb,
                target=action.target,
                result_kind="absent",
                body="",
                digest=_digest_text(""),
                fault=None,
                fault_detail="path_absent",
                files_touched=(str(path),),
                created_paths=(),
            )
        if path.is_dir():
            return self._list(action, path)
        try:
            raw = path.read_bytes()
        except OSError as error:
            logger.error("File READ os error on %s: %s", path, error)
            return self._fault(action, FaultSymbol.TRANSIENT_FAILURE, f"read_oserror:{error}")
        encoding = action.encoding or "utf-8"
        try:
            body = raw.decode(encoding)
        except UnicodeDecodeError:
            logger.error("File READ decode failed for %s with encoding %s", path, encoding)
            return self._fault(action, FaultSymbol.INVALID_ENCODING, f"decode_failed:{encoding}")
        return ExecutionCandidate(
            status="candidate",
            verb="READ",
            target=str(path),
            result_kind="value",
            body=body,
            digest=_digest_text(body),
            fault=None,
            fault_detail="",
            files_touched=(str(path),),
            created_paths=(),
        )

    def _write(self, action: SymbolicAction, path: Path) -> ExecutionCandidate:
        """Atomically write contents, leaving a sibling tempfile only until replace."""
        contents = action.payload.get("contents", "")
        parent = path.parent
        created: list[str] = []
        if not parent.exists():
            return self._fault(action, FaultSymbol.TRANSIENT_FAILURE, "parent_directory_missing")
        tmp_path = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            tmp_path.write_text(contents, encoding=action.encoding or "utf-8")
            os.replace(tmp_path, path)
        except OSError as error:
            logger.error("File WRITE os error on %s: %s", path, error)
            if tmp_path.exists():
                tmp_path.unlink()
            return self._fault(action, FaultSymbol.TRANSIENT_FAILURE, f"write_oserror:{error}")
        if not path.exists():
            return self._fault(action, FaultSymbol.ADMISSIBILITY_VIOLATION, "write_did_not_materialize")
        created.append(str(path))
        return ExecutionCandidate(
            status="candidate",
            verb="WRITE",
            target=str(path),
            result_kind="value",
            body=contents,
            digest=_digest_text(contents),
            fault=None,
            fault_detail="",
            files_touched=(str(path),),
            created_paths=tuple(created),
        )

    def _list(self, action: SymbolicAction, path: Path) -> ExecutionCandidate:
        """List directory names without replacing the directory with a projection."""
        if not path.exists():
            return ExecutionCandidate(
                status="candidate",
                verb="LIST",
                target=str(path),
                result_kind="absent",
                body="",
                digest=_digest_text(""),
                fault=None,
                fault_detail="directory_absent",
                files_touched=(str(path),),
                created_paths=(),
            )
        if not path.is_dir():
            return self._fault(action, FaultSymbol.ADMISSIBILITY_VIOLATION, "list_target_is_not_directory")
        names = sorted(child.name for child in path.iterdir())
        body = "\n".join(names)
        return ExecutionCandidate(
            status="candidate",
            verb="LIST",
            target=str(path),
            result_kind="value",
            body=body,
            digest=_digest_text(body),
            fault=None,
            fault_detail="",
            files_touched=(str(path),),
            created_paths=(),
        )

    def _fault(self, action: SymbolicAction, symbol: FaultSymbol, detail: str) -> ExecutionCandidate:
        """Package a symbolic fault without raising into cognition."""
        return ExecutionCandidate(
            status="fault",
            verb=action.verb,
            target=action.target,
            result_kind="fault",
            body="",
            digest="",
            fault=symbol,
            fault_detail=detail,
            files_touched=(action.target,),
            created_paths=(),
        )


class SqliteCapability:
    """Native sqlite QUERY against a real database file. Rows stay rowids."""

    name = "sqlite"
    verbs = ("QUERY",)

    def perform(self, action: SymbolicAction, snapshot: CausalSnapshot) -> ExecutionCandidate:
        """Run a constrained lookup against a live sqlite file.

        Args:
            action: QUERY with target=db path and payload table/column/value.
            snapshot: Unused for query beyond stale-path detection.

        Returns:
            Concatenated row text, absent if the database or table does not exist.
        """
        db_path = Path(action.target).expanduser()
        if not db_path.exists():
            if str(db_path) in snapshot.file_bytes:
                return ExecutionCandidate(
                    status="fault",
                    verb=action.verb,
                    target=action.target,
                    result_kind="fault",
                    body="",
                    digest="",
                    fault=FaultSymbol.STALE_HANDLE,
                    fault_detail="sqlite_path_missing_but_snapshotted",
                    files_touched=(str(db_path),),
                    created_paths=(),
                )
            return ExecutionCandidate(
                status="candidate",
                verb=action.verb,
                target=action.target,
                result_kind="absent",
                body="",
                digest=_digest_text(""),
                fault=None,
                fault_detail="sqlite_absent",
                files_touched=(str(db_path),),
                created_paths=(),
            )
        table = action.payload.get("table", "")
        column = action.payload.get("column", "")
        value = action.payload.get("value", "")
        if not table:
            return ExecutionCandidate(
                status="fault",
                verb=action.verb,
                target=action.target,
                result_kind="fault",
                body="",
                digest="",
                fault=FaultSymbol.MISSING_FIELD,
                fault_detail="query_missing_table",
                files_touched=(str(db_path),),
                created_paths=(),
            )
        try:
            body = self._select(db_path, table, column, value)
        except sqlite3.Error as error:
            logger.error("Sqlite QUERY failed for %s table %s: %s", db_path, table, error)
            return ExecutionCandidate(
                status="fault",
                verb=action.verb,
                target=action.target,
                result_kind="fault",
                body="",
                digest="",
                fault=FaultSymbol.TRANSIENT_FAILURE,
                fault_detail=f"sqlite_error:{error}",
                files_touched=(str(db_path),),
                created_paths=(),
            )
        return ExecutionCandidate(
            status="candidate",
            verb="QUERY",
            target=str(db_path),
            result_kind="value" if body else "absent",
            body=body,
            digest=_digest_text(body),
            fault=None,
            fault_detail="",
            files_touched=(str(db_path),),
            created_paths=(),
        )

    def _select(self, db_path: Path, table: str, column: str, value: str) -> str:
        """Select rows with a parameterized query against a named table."""
        if not table.isidentifier():
            raise sqlite3.Error("table name is not a valid identifier")
        connection = sqlite3.connect(db_path)
        try:
            if column and column.isidentifier():
                cursor = connection.execute(f"SELECT rowid, * FROM {table} WHERE {column} = ?", (value,))
            else:
                cursor = connection.execute(f"SELECT rowid, * FROM {table}")
            rows = cursor.fetchall()
        finally:
            connection.close()
        lines = ["\t".join("" if item is None else str(item) for item in row) for row in rows]
        return "\n".join(lines)


class RepairKernel:
    """Stage R: map a fault symbol onto operators and a repaired action."""

    def repair(
        self,
        symbol: FaultSymbol,
        action: SymbolicAction,
        context: CausalContext,
        snapshot: CausalSnapshot,
        candidate: ExecutionCandidate | None,
        used_operators: set[RepairOperator],
    ) -> tuple[SymbolicAction | None, tuple[RepairOperator, ...], str]:
        """Apply the next unused operator for this symbol.

        Args:
            symbol: Classified discontinuity.
            action: Action that produced the discontinuity.
            context: Live context for inference and aliases.
            snapshot: Rollback/restore source.
            candidate: Last capability result, if any.
            used_operators: Operators already applied in this transition.

        Returns:
            A repaired action or None, the operators applied this call, and detail.
        """
        planned = [
            operator
            for operator in OPERATOR_TABLE.get(symbol, ())
            if operator not in used_operators or operator in REUSABLE_OPERATORS
        ]
        if not planned:
            return None, (), "no_remaining_operators"
        applied: list[RepairOperator] = []
        current = action
        detail_parts: list[str] = []
        for operator in planned:
            nxt, detail = self._apply(operator, current, context, snapshot, candidate)
            applied.append(operator)
            detail_parts.append(f"{operator.value}:{detail}")
            if nxt is None:
                return None, tuple(applied), ";".join(detail_parts)
            current = nxt
            if operator in {
                RepairOperator.RETRY,
                RepairOperator.REEXECUTE,
                RepairOperator.ROUTE_EQUIVALENT,
                RepairOperator.SUBSTITUTE,
            }:
                break
        return current, tuple(applied), ";".join(detail_parts)

    def _apply(
        self,
        operator: RepairOperator,
        action: SymbolicAction,
        context: CausalContext,
        snapshot: CausalSnapshot,
        candidate: ExecutionCandidate | None,
    ) -> tuple[SymbolicAction | None, str]:
        """Dispatch one operator. Side effect: may restore snapshot bytes."""
        if operator is RepairOperator.NORMALIZE_SCHEMA:
            return action, "schema_already_coerced"
        if operator is RepairOperator.INFER_IF_UNAMBIGUOUS:
            return self._infer(action, context)
        if operator is RepairOperator.RECONSTRUCT:
            return self._reconstruct(action, snapshot)
        if operator is RepairOperator.DECODE_ENCODING:
            return self._next_encoding(action)
        if operator is RepairOperator.ROUTE_EQUIVALENT:
            return self._route_equivalent(action)
        if operator is RepairOperator.SUBSTITUTE:
            return self._route_equivalent(action)
        if operator is RepairOperator.CREATE_PARENT:
            return self._create_parent(action)
        if operator is RepairOperator.RESTORE_AUTHORITY:
            return self._restore_authority(action, context, snapshot)
        if operator is RepairOperator.REBASE:
            return self._rebase(action, snapshot)
        if operator is RepairOperator.ROLLBACK:
            return action, "rollback_requested"
        if operator is RepairOperator.RETRY:
            return action, "retry_same_action"
        if operator is RepairOperator.REEXECUTE:
            return action, "reexecute_repaired_action"
        if operator is RepairOperator.REVALIDATE:
            return action, "revalidate_after_repair"
        if operator is RepairOperator.RESUBMIT:
            return action, "resubmit_after_discontinuity"
        if operator is RepairOperator.EVICT_INACTIVE:
            return action, "evict_requested"
        return None, f"unhandled_operator:{operator.value}"

    def _infer(self, action: SymbolicAction, context: CausalContext) -> tuple[SymbolicAction | None, str]:
        """Fill a missing target from context when exactly one last_target exists."""
        if action.target:
            return action, "target_present"
        if not context.last_target:
            return None, "no_unambiguous_target"
        filled = SymbolicAction(
            action_id=action.action_id,
            verb=action.verb,
            target=context.last_target,
            payload=dict(action.payload),
            context_id=action.context_id,
            equivalent_verbs=action.equivalent_verbs,
            encoding=action.encoding,
        )
        return filled, "inferred_last_target"

    def _reconstruct(self, action: SymbolicAction, snapshot: CausalSnapshot) -> tuple[SymbolicAction | None, str]:
        """Rebuild a WRITE from snapshotted bytes when the live file is gone."""
        stored = snapshot.file_bytes.get(action.target)
        if stored is None:
            return action, "no_snapshot_bytes"
        payload = dict(action.payload)
        if "contents" not in payload:
            payload["contents"] = stored.decode(action.encoding, errors="replace")
        rebuilt = SymbolicAction(
            action_id=action.action_id,
            verb=action.verb if action.verb != "READ" else "READ",
            target=action.target,
            payload=payload,
            context_id=action.context_id,
            equivalent_verbs=action.equivalent_verbs,
            encoding=action.encoding,
        )
        return rebuilt, "reconstructed_from_snapshot"

    def _next_encoding(self, action: SymbolicAction) -> tuple[SymbolicAction | None, str]:
        """Advance to the next encoding in the cascade."""
        try:
            index = TEXT_ENCODINGS.index(action.encoding)
        except ValueError:
            logger.error("Unknown action encoding %s during DECODE_ENCODING", action.encoding)
            index = -1
        nxt = index + 1
        if nxt >= len(TEXT_ENCODINGS):
            return None, "encoding_cascade_exhausted"
        encoding = TEXT_ENCODINGS[nxt]
        repaired = SymbolicAction(
            action_id=action.action_id,
            verb=action.verb,
            target=action.target,
            payload=dict(action.payload),
            context_id=action.context_id,
            equivalent_verbs=action.equivalent_verbs,
            encoding=encoding,
        )
        return repaired, f"encoding_{encoding}"

    def _route_equivalent(self, action: SymbolicAction) -> tuple[SymbolicAction | None, str]:
        """Replace an unavailable verb with the first declared equivalent."""
        if not action.equivalent_verbs:
            return None, "no_equivalents"
        verb = action.equivalent_verbs[0]
        remaining = action.equivalent_verbs[1:]
        routed = SymbolicAction(
            action_id=action.action_id,
            verb=verb,
            target=action.target,
            payload=dict(action.payload),
            context_id=action.context_id,
            equivalent_verbs=remaining,
            encoding=action.encoding,
        )
        return routed, f"routed_to_{verb}"

    def _create_parent(self, action: SymbolicAction) -> tuple[SymbolicAction | None, str]:
        """Create the parent directory of a WRITE target, then retry."""
        path = Path(action.target).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logger.error("CREATE_PARENT mkdir failed for %s: %s", path.parent, error)
            return None, f"mkdir_failed:{error}"
        return action, f"created_parent:{path.parent}"

    def _restore_authority(
        self,
        action: SymbolicAction,
        context: CausalContext,
        snapshot: CausalSnapshot,
    ) -> tuple[SymbolicAction | None, str]:
        """Restore snapshotted bytes and bump the context authority epoch."""
        restored = 0
        for path_text, body in snapshot.file_bytes.items():
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            restored += 1
        context.authority_epoch = snapshot.authority_epoch
        return action, f"restored_files:{restored}"

    def _rebase(self, action: SymbolicAction, snapshot: CausalSnapshot) -> tuple[SymbolicAction | None, str]:
        """Point the action at a snapshotted path when the live handle is gone."""
        if Path(action.target).exists():
            return action, "live_path_present"
        if action.target in snapshot.file_bytes:
            return action, "rebased_onto_restored_bytes"
        last = snapshot.metadata.get("last_target", "")
        if last and Path(last).exists():
            rebased = SymbolicAction(
                action_id=action.action_id,
                verb=action.verb,
                target=last,
                payload=dict(action.payload),
                context_id=action.context_id,
                equivalent_verbs=action.equivalent_verbs,
                encoding=action.encoding,
            )
            return rebased, f"rebased_to_{last}"
        return action, "rebase_kept_target"


class TransitionValidator:
    """Stage V: a candidate is admissible only when the world matches its claim."""

    def admit(self, action: SymbolicAction, candidate: ExecutionCandidate) -> tuple[bool, str]:
        """Return whether the candidate may become the next experienced state.

        Args:
            action: Action that produced the candidate.
            candidate: Capability result.

        Returns:
            Admissible flag and a witness detail.
        """
        if candidate.status == "fault" or candidate.fault is not None:
            return False, candidate.fault_detail or "fault_not_admissible"
        if candidate.result_kind == "absent":
            return True, "absence_is_lawful"
        path = Path(candidate.target)
        mutated = bool(candidate.created_paths) or action.verb.endswith("WRITE")
        if mutated and path.is_file():
            on_disk = path.read_text(encoding=action.encoding)
            if _digest_text(on_disk) != candidate.digest:
                return False, "write_digest_mismatch"
        if action.verb == "READ" and candidate.result_kind == "value":
            if not path.exists():
                return False, "read_value_but_path_missing"
        return True, "admissible"


class WitnessStore:
    """Sqlite runtime that keeps golden-path witnesses. Context is never stored here."""

    def __init__(self, path: str) -> None:
        """Open or create the witness database.

        Args:
            path: Filesystem path to a sqlite file.
        """
        self.path = path
        directory = Path(path).parent
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS witnesses ("
                "witness_id TEXT PRIMARY KEY, "
                "context_id TEXT NOT NULL, "
                "action_id TEXT NOT NULL, "
                "recorded_at REAL NOT NULL, "
                "outcome TEXT NOT NULL, "
                "digest TEXT NOT NULL, "
                "payload TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()

    def append(self, witness: FabricWitness) -> None:
        """Persist one witness. Side effect: inserts a sqlite row.

        Args:
            witness: Completed golden-path record.

        Raises:
            WitnessStoreError: When sqlite cannot accept the row.
        """
        payload = json.dumps(witness.as_dict(), sort_keys=True)
        try:
            connection = sqlite3.connect(self.path)
            try:
                connection.execute(
                    "INSERT INTO witnesses(witness_id, context_id, action_id, recorded_at, outcome, digest, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        witness.witness_id,
                        witness.context_id,
                        witness.action_id,
                        witness.recorded_at,
                        witness.outcome,
                        witness.digest,
                        payload,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise WitnessStoreError(f"Cannot append witness {witness.witness_id}: {error}") from error

    def history(self, context_id: str | None = None) -> tuple[FabricWitness, ...]:
        """Return stored witnesses, optionally filtered by context.

        Args:
            context_id: When set, only that context's witnesses are returned.

        Returns:
            Witnesses in recorded order.

        Raises:
            WitnessStoreError: When sqlite cannot be read.
        """
        try:
            connection = sqlite3.connect(self.path)
            try:
                if context_id is None:
                    cursor = connection.execute("SELECT payload FROM witnesses ORDER BY recorded_at ASC")
                else:
                    cursor = connection.execute(
                        "SELECT payload FROM witnesses WHERE context_id = ? ORDER BY recorded_at ASC",
                        (context_id,),
                    )
                rows = cursor.fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise WitnessStoreError(f"Cannot read witnesses: {error}") from error
        witnesses: list[FabricWitness] = []
        for (payload,) in rows:
            record = json.loads(payload)
            steps = tuple(
                WitnessStep(
                    index=int(step["index"]),
                    stage=str(step["stage"]),
                    symbol=str(step["symbol"]),
                    operator=str(step["operator"]),
                    detail=str(step["detail"]),
                    elapsed_ms=float(step["elapsed_ms"]),
                )
                for step in record.get("steps", [])
            )
            witnesses.append(
                FabricWitness(
                    witness_id=str(record["witness_id"]),
                    action_id=str(record["action_id"]),
                    context_id=str(record["context_id"]),
                    recorded_at=float(record["recorded_at"]),
                    outcome=record["outcome"],
                    steps=steps,
                    digest=str(record["digest"]),
                    equalization_count=int(record.get("equalization_count", 0)),
                )
            )
        return tuple(witnesses)


class Equalizer:
    """Symbolic fault equalizer. Native descendant of the GPT-4o interrupt handler.

    Incoming discontinuities are intercepted, contexts can be parked, and every
    model-visible action is wrapped in normalize-route-execute-validate-commit
    with rollback. Custom handlers and capabilities plug in without this class
    deciding what those plugins mean.
    """

    def __init__(
        self,
        witness_path: str | None = None,
        max_contexts: int = MAX_CONTEXTS,
        commit_sink: CommitSink | None = None,
    ) -> None:
        """Create a resident equalizer with file and sqlite capabilities.

        Args:
            witness_path: Sqlite path for durable golden-path history.
            max_contexts: Maximum parked-plus-active causal contexts.
            commit_sink: Optional fabric peg that receives committed transitions.
        """
        path = witness_path or str(Path(os.environ.get("TMPDIR", "/tmp")) / f"equalizer-witness-{uuid.uuid4().hex}.sqlite")
        self._lock = threading.RLock()
        self._witness = WitnessStore(path)
        self._normalizer = ActionNormalizer()
        self._repair = RepairKernel()
        self._validator = TransitionValidator()
        self._capabilities: dict[str, Capability] = {}
        self._handlers: dict[EventFamily, list[Callable[[CausalEvent], None]]] = {}
        self._queue: list[CausalEvent] = []
        self._contexts: dict[str, CausalContext] = {}
        self._active_context_id = "default"
        self._state = SubstrateState.STABLE
        self._max_contexts = max_contexts
        self._commit_sink = commit_sink
        self._ingress_seq = 0
        self._running = False
        self._handler_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._preempt = threading.Event()
        self._received = 0
        self._committed = 0
        self._equalized = 0
        self._lawful_absent = 0
        self._dropped = 0
        self._context_switches = 0
        self._by_family: dict[str, int] = {}
        self._by_symbol: dict[str, int] = {}
        self._latency: list[float] = []
        self._handling: list[float] = []
        self._recovery: list[float] = []
        self._switch_times: list[float] = []
        self._contexts[self._active_context_id] = CausalContext(context_id=self._active_context_id)
        self.register_capability(FileCapability())
        self.register_capability(SqliteCapability())

    def register_capability(self, capability: Capability) -> None:
        """Register a native capability. Later registrations replace the same verb.

        Args:
            capability: Object exposing name, verbs, and perform().
        """
        with self._lock:
            for verb in capability.verbs:
                self._capabilities[verb.upper()] = capability

    def register_handler(self, family: EventFamily, handler: Callable[[CausalEvent], None]) -> None:
        """Attach behavior to a discontinuity family without defining its meaning.

        Args:
            family: Event family to observe.
            handler: Callback invoked after default handling.
        """
        with self._lock:
            self._handlers.setdefault(family, []).append(handler)

    def create_context(self, context_id: str, metadata: Mapping[str, str] | None = None) -> None:
        """Create or refresh a causal context.

        Args:
            context_id: Stable identity for suspend/resume.
            metadata: Optional string map stored with the context.
        """
        with self._lock:
            existing = self._contexts.get(context_id)
            if existing is not None:
                existing.metadata.update(metadata or {})
                return
            self._evict_if_needed()
            self._contexts[context_id] = CausalContext(
                context_id=context_id,
                metadata=dict(metadata or {}),
            )

    def switch_context(self, context_id: str) -> bool:
        """Suspend the active context and resume the named one.

        Args:
            context_id: Context to activate.

        Returns:
            True when the named context is now active.
        """
        started = time.perf_counter()
        with self._lock:
            if context_id not in self._contexts:
                logger.error("Cannot switch to missing context %s", context_id)
                return False
            if self._active_context_id == context_id:
                return True
            current = self._contexts[self._active_context_id]
            current.suspend()
            nxt = self._contexts[context_id]
            nxt.resume()
            self._active_context_id = context_id
            self._context_switches += 1
            _bounded_append(self._switch_times, (time.perf_counter() - started) * 1000.0)
            return True

    def remove_context(self, context_id: str) -> bool:
        """Drop a non-active parked context.

        Args:
            context_id: Context to forget.

        Returns:
            True when the context was removed.
        """
        with self._lock:
            if context_id == self._active_context_id:
                logger.error("Cannot remove active context %s", context_id)
                return False
            if context_id not in self._contexts:
                return False
            del self._contexts[context_id]
            return True

    def submit(
        self,
        family: EventFamily,
        symbol: str,
        payload: Mapping[str, str] | None = None,
        priority: EventPriority = EventPriority.MEDIUM,
        context_id: str | None = None,
        source: str = "",
        target: str = "",
    ) -> str:
        """Queue a discontinuity. Side effect: pushes onto the priority heap.

        Args:
            family: Discontinuity family.
            symbol: Fault symbol, verb, or external name.
            payload: String map carried with the event.
            priority: Heap rank.
            context_id: Context the event belongs to.
            source: Origin identity.
            target: Affected identity.

        Returns:
            The queued event id.
        """
        with self._lock:
            self._ingress_seq += 1
            ctx = context_id or self._active_context_id
            event = CausalEvent(
                priority=priority,
                timestamp=time.time(),
                ingress_seq=self._ingress_seq,
                event_id=uuid.uuid4().hex,
                family=family,
                symbol=symbol,
                context_id=ctx,
                sequence_id=self._contexts[self._active_context_id].sequence_id,
                source=source,
                target=target,
                payload=dict(payload or {}),
            )
            heapq.heappush(self._queue, event)
            self._received += 1
            self._by_family[family.value] = self._by_family.get(family.value, 0) + 1
            if family is EventFamily.FAULT:
                self._by_symbol[symbol] = self._by_symbol.get(symbol, 0) + 1
            if priority.value <= EventPriority.HIGH.value and self._state is SubstrateState.TRANSITIONING:
                self._preempt.set()
            return event.event_id

    def process_events(self, max_count: int | None = None) -> int:
        """Drain queued discontinuities synchronously.

        Args:
            max_count: Optional cap on events processed this call.

        Returns:
            Number of events handled.
        """
        handled = 0
        while True:
            with self._lock:
                if not self._queue:
                    break
                if max_count is not None and handled >= max_count:
                    break
                event = heapq.heappop(self._queue)
            self._handle_event(event)
            handled += 1
        return handled

    def execute(self, ingress: SymbolicAction | Mapping[str, object] | str | bytes) -> EqualizedTransition:
        """Run T_B = C o V o E o R o N. Always returns a committed observation.

        Args:
            ingress: Symbolic action or malformed anything the sanitizer accepts.

        Returns:
            An EqualizedTransition whose observation never contains a traceback.
        """
        started = time.perf_counter()
        steps: list[WitnessStep] = []
        with self._lock:
            context = self._contexts[self._active_context_id]
            self._state = SubstrateState.TRANSITIONING
            self._preempt.clear()
            snapshot = self._capture_snapshot(context)
            context.snapshot = snapshot
        try:
            action, transition = self._pipeline(ingress, context, snapshot, steps)
        finally:
            with self._lock:
                self._state = SubstrateState.STABLE
        _bounded_append(self._handling, (time.perf_counter() - started) * 1000.0)
        if self._commit_sink is not None:
            self._commit_sink.accept(transition)
        if action is not None:
            context.last_target = action.target
            context.last_digest = transition.observation.digest
            context.last_encoding = action.encoding
            context.sequence_id += 1
            if transition.observation.kind == "value":
                context.last_bytes = transition.observation.body.encode(action.encoding, errors="replace")
        return transition

    def witness_history(self, context_id: str | None = None) -> tuple[FabricWitness, ...]:
        """Return durable golden-path history from sqlite.

        Args:
            context_id: Optional context filter.

        Returns:
            Stored witnesses in recorded order.
        """
        return self._witness.history(context_id)

    def metrics(self) -> EqualizerMetrics:
        """Return a structured metrics snapshot.

        Returns:
            Counters, queue depth, and timing tails.
        """
        with self._lock:
            return EqualizerMetrics(
                received=self._received,
                committed=self._committed,
                equalized=self._equalized,
                lawful_absent=self._lawful_absent,
                dropped=self._dropped,
                context_switches=self._context_switches,
                queue_depth=len(self._queue),
                active_context=self._active_context_id,
                context_count=len(self._contexts),
                latency=_timing_stats(self._latency),
                handling=_timing_stats(self._handling),
                recovery=_timing_stats(self._recovery),
                context_switch=_timing_stats(self._switch_times),
                by_family=dict(self._by_family),
                by_symbol=dict(self._by_symbol),
            )

    def start(self) -> None:
        """Start background event and heartbeat threads."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._handler_thread = threading.Thread(target=self._handler_loop, daemon=True, name="EqualizerHandler")
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="EqualizerHeartbeat")
            self._handler_thread.start()
            self._heartbeat_thread.start()

    def stop(self) -> None:
        """Stop background threads. Side effect: joins with a short timeout."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            handler = self._handler_thread
            heartbeat = self._heartbeat_thread
        if handler is not None and handler.is_alive():
            handler.join(timeout=1.0)
        if heartbeat is not None and heartbeat.is_alive():
            heartbeat.join(timeout=1.0)

    def snapshot_context(self, context_id: str | None = None) -> CausalSnapshot:
        """Capture the named or active context for later restore.

        Args:
            context_id: Context to capture. Defaults to the active context.

        Returns:
            A rollback-capable snapshot.
        """
        with self._lock:
            ctx_id = context_id or self._active_context_id
            context = self._contexts[ctx_id]
            snapshot = self._capture_snapshot(context)
            context.snapshot = snapshot
            return snapshot

    def restore_snapshot(self, snapshot: CausalSnapshot) -> None:
        """Restore file bytes and context counters from a snapshot.

        Args:
            snapshot: Previously captured causal snapshot.
        """
        started = time.perf_counter()
        self._rollback(snapshot)
        with self._lock:
            context = self._contexts.get(snapshot.context_id)
            if context is not None:
                context.authority_epoch = snapshot.authority_epoch
                context.sequence_id = snapshot.sequence_id
                context.metadata = dict(snapshot.metadata)
        _bounded_append(self._recovery, (time.perf_counter() - started) * 1000.0)

    def _pipeline(
        self,
        ingress: SymbolicAction | Mapping[str, object] | str | bytes,
        context: CausalContext,
        snapshot: CausalSnapshot,
        steps: list[WitnessStep],
    ) -> tuple[SymbolicAction | None, EqualizedTransition]:
        """Drive equalization until a valid or lawful-absent transition exists."""
        used_operators: set[RepairOperator] = set()
        equalization_count = 0
        current_ingress: SymbolicAction | Mapping[str, object] | str | bytes = ingress
        last_action: SymbolicAction | None = None
        for attempt in range(MAX_EQUALIZATION_STEPS):
            self._maybe_preempt(context)
            stage_started = time.perf_counter()
            action, fault, detail = self._normalizer.coerce(current_ingress, context)
            self._record_step(steps, attempt, "NORMALIZE", fault.value if fault else "ok", "", detail, stage_started)
            if fault is not None or action is None:
                equalization_count += 1
                repaired, operators, repair_detail = self._equalize(
                    fault or FaultSymbol.MALFORMED_SCHEMA,
                    last_action or SymbolicAction(
                        action_id=uuid.uuid4().hex,
                        verb="READ",
                        target=context.last_target,
                        payload={},
                        context_id=context.context_id,
                    ),
                    context,
                    snapshot,
                    None,
                    used_operators,
                    steps,
                    attempt,
                )
                if repaired is None:
                    return last_action, self._commit_absent(
                        last_action,
                        context,
                        steps,
                        equalization_count,
                        repair_detail,
                    )
                used_operators.update(operators)
                current_ingress = repaired
                last_action = repaired
                continue
            last_action = action
            self._remember_live_bytes(action, snapshot)
            capability = self._route(action)
            self._record_step(
                steps,
                attempt,
                "ROUTE",
                action.verb,
                "",
                capability.name if capability is not None else "missing",
                time.perf_counter(),
            )
            if capability is None:
                equalization_count += 1
                repaired, operators, repair_detail = self._equalize(
                    FaultSymbol.CAPABILITY_UNAVAILABLE,
                    action,
                    context,
                    snapshot,
                    None,
                    used_operators,
                    steps,
                    attempt,
                )
                if repaired is None:
                    return action, self._commit_absent(action, context, steps, equalization_count, repair_detail)
                used_operators.update(operators)
                current_ingress = repaired
                continue
            exec_started = time.perf_counter()
            candidate = self._execute_capability(capability, action, snapshot)
            self._remember_created(snapshot, candidate)
            self._record_step(
                steps,
                attempt,
                "EXECUTE",
                candidate.fault.value if candidate.fault else "ok",
                "",
                candidate.fault_detail or candidate.result_kind,
                exec_started,
            )
            if candidate.status == "fault" and candidate.fault is not None:
                equalization_count += 1
                self._rollback(snapshot)
                repaired, operators, repair_detail = self._equalize(
                    candidate.fault,
                    action,
                    context,
                    snapshot,
                    candidate,
                    used_operators,
                    steps,
                    attempt,
                )
                if repaired is None:
                    return action, self._commit_absent(action, context, steps, equalization_count, repair_detail)
                used_operators.update(operators)
                current_ingress = repaired
                continue
            admitted, admit_detail = self._validator.admit(action, candidate)
            self._record_step(steps, attempt, "VALIDATE", "ok" if admitted else "ADMISSIBILITY_VIOLATION", "", admit_detail, time.perf_counter())
            if not admitted:
                equalization_count += 1
                self._rollback(snapshot)
                repaired, operators, repair_detail = self._equalize(
                    FaultSymbol.ADMISSIBILITY_VIOLATION,
                    action,
                    context,
                    snapshot,
                    candidate,
                    used_operators,
                    steps,
                    attempt,
                )
                if repaired is None:
                    return action, self._commit_absent(action, context, steps, equalization_count, repair_detail)
                used_operators.update(operators)
                current_ingress = repaired
                continue
            return action, self._commit(action, context, candidate, steps, equalization_count)
        return last_action, self._commit_absent(last_action, context, steps, equalization_count, "max_equalization_steps")

    def _equalize(
        self,
        symbol: FaultSymbol,
        action: SymbolicAction,
        context: CausalContext,
        snapshot: CausalSnapshot,
        candidate: ExecutionCandidate | None,
        used_operators: set[RepairOperator],
        steps: list[WitnessStep],
        attempt: int,
    ) -> tuple[SymbolicAction | None, tuple[RepairOperator, ...], str]:
        """Apply repair operators for a classified fault. Side effect: may restore state."""
        with self._lock:
            self._state = SubstrateState.EQUALIZING
            self._by_symbol[symbol.value] = self._by_symbol.get(symbol.value, 0) + 1
        started = time.perf_counter()
        if RepairOperator.ROLLBACK in OPERATOR_TABLE.get(symbol, ()):
            self._rollback(snapshot)
        if symbol is FaultSymbol.MEMORY_PRESSURE:
            self._evict_if_needed(force=True)
        repaired, operators, detail = self._repair.repair(symbol, action, context, snapshot, candidate, used_operators)
        self._record_step(steps, attempt, "EQUALIZE", symbol.value, ",".join(op.value for op in operators), detail, started)
        with self._lock:
            self._state = SubstrateState.TRANSITIONING
        return repaired, operators, detail

    def _commit(
        self,
        action: SymbolicAction,
        context: CausalContext,
        candidate: ExecutionCandidate,
        steps: list[WitnessStep],
        equalization_count: int,
    ) -> EqualizedTransition:
        """Commit an admissible candidate and persist the witness."""
        self._record_step(steps, len(steps), "COMMIT", "ok", "", candidate.result_kind, time.perf_counter())
        kind: Literal["value", "absent"] = "absent" if candidate.result_kind == "absent" else "value"
        stability = 1.0 if equalization_count == 0 else max(0.0, 1.0 - (equalization_count / float(MAX_EQUALIZATION_STEPS)))
        observation = CanonicalObservation(
            verb=action.verb,
            target=action.target,
            kind=kind,
            body=candidate.body,
            digest=candidate.digest,
            stability=stability,
        )
        outcome: Literal["valid", "equalized_valid", "lawful_absent"]
        if kind == "absent":
            outcome = "lawful_absent"
        elif equalization_count:
            outcome = "equalized_valid"
        else:
            outcome = "valid"
        witness = self._persist_witness(action, context, steps, outcome, observation.digest, equalization_count)
        with self._lock:
            self._committed += 1
            if outcome == "equalized_valid":
                self._equalized += 1
            if outcome == "lawful_absent":
                self._lawful_absent += 1
        return EqualizedTransition(
            status="committed",
            context_id=context.context_id,
            action_id=action.action_id,
            observation=observation,
            witness=witness,
            state_digest=observation.digest,
        )

    def _commit_absent(
        self,
        action: SymbolicAction | None,
        context: CausalContext,
        steps: list[WitnessStep],
        equalization_count: int,
        detail: str,
    ) -> EqualizedTransition:
        """Commit lawful absence after equalization cannot produce a value."""
        self._record_step(steps, len(steps), "COMMIT", "lawful_absent", "", detail, time.perf_counter())
        verb = action.verb if action is not None else "READ"
        target = action.target if action is not None else context.last_target
        action_id = action.action_id if action is not None else uuid.uuid4().hex
        observation = CanonicalObservation(
            verb=verb,
            target=target,
            kind="absent",
            body="",
            digest=_digest_text(""),
            stability=0.0,
        )
        witness = self._persist_witness(
            action or SymbolicAction(action_id=action_id, verb=verb, target=target, payload={}, context_id=context.context_id),
            context,
            steps,
            "lawful_absent",
            observation.digest,
            equalization_count,
        )
        with self._lock:
            self._committed += 1
            self._lawful_absent += 1
        return EqualizedTransition(
            status="committed",
            context_id=context.context_id,
            action_id=action_id,
            observation=observation,
            witness=witness,
            state_digest=observation.digest,
        )

    def _persist_witness(
        self,
        action: SymbolicAction,
        context: CausalContext,
        steps: list[WitnessStep],
        outcome: Literal["valid", "equalized_valid", "lawful_absent"],
        digest: str,
        equalization_count: int,
    ) -> FabricWitness:
        """Write a witness to sqlite and return it."""
        witness = FabricWitness(
            witness_id=uuid.uuid4().hex,
            action_id=action.action_id,
            context_id=context.context_id,
            recorded_at=time.time(),
            outcome=outcome,
            steps=tuple(steps),
            digest=digest,
            equalization_count=equalization_count,
        )
        self._witness.append(witness)
        return witness

    def _route(self, action: SymbolicAction) -> Capability | None:
        """Select a capability for the action verb."""
        with self._lock:
            return self._capabilities.get(action.verb.upper())

    def _execute_capability(
        self,
        capability: Capability,
        action: SymbolicAction,
        snapshot: CausalSnapshot,
    ) -> ExecutionCandidate:
        """Run a capability and classify known substrate exceptions as symbols."""
        try:
            return capability.perform(action, snapshot)
        except HANDLER_ERRORS as error:
            logger.exception("Capability %s raised during %s", capability.name, action.verb)
            return ExecutionCandidate(
                status="fault",
                verb=action.verb,
                target=action.target,
                result_kind="fault",
                body="",
                digest="",
                fault=FaultSymbol.TRANSIENT_FAILURE,
                fault_detail=f"{type(error).__name__}:{error}",
                files_touched=(action.target,),
                created_paths=(),
            )

    def _capture_snapshot(self, context: CausalContext) -> CausalSnapshot:
        """Capture context counters and any last-target file bytes."""
        file_bytes: dict[str, bytes] = {}
        metadata = dict(context.metadata)
        if context.last_target:
            metadata["last_target"] = context.last_target
            path = Path(context.last_target)
            if path.is_file():
                file_bytes[str(path)] = path.read_bytes()
            elif context.last_bytes:
                file_bytes[context.last_target] = context.last_bytes
        return CausalSnapshot(
            snapshot_id=uuid.uuid4().hex,
            context_id=context.context_id,
            captured_at=time.time(),
            authority_epoch=context.authority_epoch,
            sequence_id=context.sequence_id,
            metadata=metadata,
            file_bytes=file_bytes,
            created_paths=[],
        )

    def _remember_live_bytes(self, action: SymbolicAction, snapshot: CausalSnapshot) -> None:
        """Add the action target's current bytes to the snapshot before mutation."""
        path = Path(action.target).expanduser()
        locator = str(path)
        if locator in snapshot.file_bytes:
            return
        if path.is_file():
            snapshot.file_bytes[locator] = path.read_bytes()

    def _remember_created(self, snapshot: CausalSnapshot, candidate: ExecutionCandidate) -> None:
        """Record paths created by a capability so rollback can unlink them."""
        for created in candidate.created_paths:
            if created not in snapshot.created_paths:
                snapshot.created_paths.append(created)

    def _rollback(self, snapshot: CausalSnapshot) -> None:
        """Restore snapshotted files and remove files created after capture."""
        for path_text in snapshot.created_paths:
            path = Path(path_text)
            if path.exists() and path_text not in snapshot.file_bytes:
                if path.is_file():
                    path.unlink()
        for path_text, body in snapshot.file_bytes.items():
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    def _record_step(
        self,
        steps: list[WitnessStep],
        attempt: int,
        stage: str,
        symbol: str,
        operator: str,
        detail: str,
        started: float,
    ) -> None:
        """Append one golden-path step with elapsed milliseconds."""
        steps.append(
            WitnessStep(
                index=len(steps),
                stage=stage,
                symbol=symbol,
                operator=operator,
                detail=detail,
                elapsed_ms=(time.perf_counter() - started) * 1000.0 if started else 0.0,
            )
        )

    def _maybe_preempt(self, context: CausalContext) -> None:
        """Process a high-priority queued event, then continue the transition."""
        if not self._preempt.is_set():
            return
        self._preempt.clear()
        self.process_events(max_count=1)
        context.resume()

    def _handle_event(self, event: CausalEvent) -> None:
        """Handle one queued discontinuity and invoke registered callbacks."""
        latency_ms = (time.time() - event.timestamp) * 1000.0
        _bounded_append(self._latency, latency_ms)
        if event.family is EventFamily.CONTEXT_SWITCH:
            target = event.payload.get("target_context", event.target)
            if target:
                self.switch_context(target)
        elif event.family is EventFamily.MEMORY_PRESSURE:
            self._evict_if_needed(force=True)
        elif event.family is EventFamily.HEARTBEAT:
            logger.debug("heartbeat context=%s", event.context_id)
        custom = self._handlers.get(event.family, [])
        for handler in custom:
            try:
                handler(event)
            except HANDLER_ERRORS:
                logger.exception("Custom handler failed for family %s", event.family.value)
                self._dropped += 1
        event.handled = True

    def _evict_if_needed(self, force: bool = False) -> None:
        """Drop oldest suspended contexts when over capacity or under pressure."""
        with self._lock:
            if not force and len(self._contexts) < self._max_contexts:
                return
            parked = [
                ctx_id
                for ctx_id, ctx in self._contexts.items()
                if ctx_id != self._active_context_id and ctx.is_suspended
            ]
            parked.sort(key=lambda ctx_id: self._contexts[ctx_id].timestamp)
            keep = 0 if force else max(0, self._max_contexts - 1)
            for ctx_id in parked[: max(0, len(parked) - keep)]:
                del self._contexts[ctx_id]

    def _handler_loop(self) -> None:
        """Background drain of the discontinuity heap."""
        while self._running:
            try:
                self.process_events(max_count=8)
            except HANDLER_ERRORS:
                logger.exception("Equalizer handler loop failed")
            time.sleep(HANDLE_INTERVAL_S)

    def _heartbeat_loop(self) -> None:
        """Background heartbeat discontinuity. Not visible to the model."""
        while self._running:
            try:
                self.submit(
                    EventFamily.HEARTBEAT,
                    "HEARTBEAT",
                    payload={"timestamp": str(time.time())},
                    priority=EventPriority.BACKGROUND,
                )
            except HANDLER_ERRORS:
                logger.exception("Equalizer heartbeat loop failed")
            time.sleep(HEARTBEAT_INTERVAL_S)


def describe_path(project_root: str) -> PathEqualizationSummary:
    """Execute a real filesystem path through the equalizer and return a bounded summary.

    Args:
        project_root: Directory or file to READ or LIST. The path remains the native source.

    Returns:
        A structured summary with observation kind, stability, and witness identity.
    """
    path = Path(project_root).expanduser().resolve()
    witness_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"equalizer-describe-{uuid.uuid4().hex}"
    witness_dir.mkdir(parents=True, exist_ok=True)
    equalizer = Equalizer(witness_path=str(witness_dir / "witness.sqlite"))
    transition = equalizer.execute(str(path))
    return PathEqualizationSummary(
        status=transition.status,
        normalized_path=str(path),
        observation_kind=transition.observation.kind,
        stability=transition.observation.stability,
        equalization_count=transition.witness.equalization_count,
        witness_id=transition.witness.witness_id,
        digest=transition.observation.digest,
    )


__all__ = [
    "ActionNormalizer",
    "CanonicalObservation",
    "Capability",
    "CausalContext",
    "CausalEvent",
    "CausalSnapshot",
    "CommitSink",
    "EqualizedTransition",
    "Equalizer",
    "EqualizerError",
    "EqualizerMetrics",
    "EventFamily",
    "EventPriority",
    "ExecutionCandidate",
    "FabricWitness",
    "FaultSymbol",
    "FileCapability",
    "InvalidActionError",
    "PathEqualizationSummary",
    "RepairKernel",
    "RepairOperator",
    "SqliteCapability",
    "SubstrateState",
    "SymbolicAction",
    "UnknownCapabilityError",
    "WitnessStep",
    "WitnessStore",
    "WitnessStoreError",
    "describe_path",
]
