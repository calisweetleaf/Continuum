"""
In-memory key-value substrate — the D5 test workhorse.

Source: written for cherry_ttt P1 per D5 (dict-clone snapshots: trivial by
    design, so every property of the contract is testable without I/O).
Integrated: 2026-07-05
Purpose: Reference TransactionalSubstrate. Snapshots are deep clones into
    a ledger keyed by opaque tokens; digest hashes touched keys only (D2)
    with tombstones for deletions; the touched set is part of the
    snapshot, so restore is bitwise-sound by construction and the §8.5
    property test proves it rather than assumes it.

Tools: kv.get (READ), kv.put / kv.delete / kv.increment
    (WRITE_REVERSIBLE), kv.burn (WRITE_IRREVERSIBLE test hook),
    kv.external (EXTERNAL test hook). The hooks exist so §8.6 effect
    enforcement is adversarially testable against a real adapter.
"""

from __future__ import annotations

import copy
import hashlib
import time
from typing import Any

from ...core.errors import SnapshotError, ValidationError
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

_TOMBSTONE = "__deleted__"

PROTECTED_PREFIX = "verifier:"
"""Keys under this prefix form the §9.7 protected namespace: readable by
predicates, unwritable by the action space (enforced in effect_class)."""

_EFFECTS: dict[str, EffectClass] = {
    "kv.get": EffectClass.READ,
    "kv.put": EffectClass.WRITE_REVERSIBLE,
    "kv.delete": EffectClass.WRITE_REVERSIBLE,
    "kv.increment": EffectClass.WRITE_REVERSIBLE,
    "kv.burn": EffectClass.WRITE_IRREVERSIBLE,
    "kv.external": EffectClass.EXTERNAL,
}


class MemoryKVSubstrate(TransactionalSubstrateBase):
    """Dict-backed Tier-T substrate with clone snapshots.

    Source: cherry_ttt P1 (module docstring carries full provenance).
    """

    def __init__(self, substrate_id: str = "memory_kv") -> None:
        self._id = substrate_id
        self._store: dict[str, Any] = {}
        self._touched: set[str] = set()
        self._snapshots: dict[str, tuple[dict[str, Any], frozenset[str]]] = {}
        self._seq = 0

    # -- contract surface ---------------------------------------------------

    def effect_class(self, a: ActionCandidate) -> EffectClass:
        """Classify by tool_id; unknown tools are a validation failure.

        §9.7 / standing invariant 4: any write into the protected
        verifier namespace (keys prefixed 'verifier:') classifies as
        WRITE_IRREVERSIBLE, so Tier T rejects it structurally before
        adapter code runs — the action space cannot touch what
        predicates read as reference data."""
        try:
            base = _EFFECTS[a.tool_id]
        except KeyError as exc:
            raise ValidationError(
                f"unknown tool {a.tool_id!r} for memory_kv; known: {sorted(_EFFECTS)}"
            ) from exc
        if base is EffectClass.WRITE_REVERSIBLE:
            key = a.args.get("k")
            if isinstance(key, str) and key.startswith(PROTECTED_PREFIX):
                return EffectClass.WRITE_IRREVERSIBLE
        return base

    def snapshot(self) -> SnapshotHandle:
        """Deep-clone store + touched set into the ledger; O(state) by design."""
        self._seq += 1
        token = f"kv-{self._seq}"
        self._snapshots[token] = (copy.deepcopy(self._store), frozenset(self._touched))
        return SnapshotHandle(substrate_id=self._id, token=token, seq=self._seq)

    def restore(self, h: SnapshotHandle) -> None:
        """Restore store and touched set exactly as captured; valid from any
        descendant because the clone is immutable in the ledger (D2)."""
        if h.substrate_id != self._id or h.token not in self._snapshots:
            raise SnapshotError(f"handle {h!r} unknown to substrate {self._id!r}")
        store, touched = self._snapshots[h.token]
        self._store = copy.deepcopy(store)
        self._touched = set(touched)

    def digest(self) -> EnvDigest:
        """Hash touched keys only (D2): sorted (key -> value|tombstone)."""
        view = {
            key: (self._store[key] if key in self._store else _TOMBSTONE)
            for key in sorted(self._touched)
        }
        return EnvDigest(hashlib.sha256(canonicalize(view).encode("utf-8")).hexdigest())

    def snapshot_cost_estimate(self) -> Cost:
        """Clone cost scales with store size; env_calls charged as one."""
        return Cost(wall_ms=0.01 * max(1, len(self._store)), env_calls=1)

    # -- gated execution ------------------------------------------------------

    def _do_execute(self, a: ActionCandidate) -> tuple[Observation, Cost]:
        """Perform an already-gated READ/WRITE_REVERSIBLE action.

        Malformed-but-legal actions (missing key on get, non-numeric
        increment) return error observations without touching state —
        errors are observations, not exceptions, so the fuzzer exercises
        them as ordinary trajectory events.
        """
        t0 = time.perf_counter()
        args = a.args
        key = args.get("k")
        if not isinstance(key, str):
            return self._err("missing or non-string key 'k'", t0)

        if a.tool_id == "kv.get":
            if key in self._store:
                obs = Observation(kind="result", payload=self._store[key])
            else:
                obs = Observation(kind="empty", payload=None)
        elif a.tool_id == "kv.put":
            self._store[key] = copy.deepcopy(args.get("v"))
            self._touched.add(key)
            obs = Observation(kind="result", payload={"ok": True})
        elif a.tool_id == "kv.delete":
            existed = self._store.pop(key, _TOMBSTONE) is not _TOMBSTONE
            self._touched.add(key)
            obs = Observation(kind="result", payload={"existed": existed})
        elif a.tool_id == "kv.increment":
            by = args.get("by", 1)
            current = self._store.get(key, 0)
            if not isinstance(by, (int, float)) or isinstance(by, bool) or \
                    not isinstance(current, (int, float)) or isinstance(current, bool):
                return self._err(f"non-numeric increment on {key!r}", t0)
            self._store[key] = current + by
            self._touched.add(key)
            obs = Observation(kind="scalar", payload=self._store[key])
        else:  # unreachable for gated classes; defensive completeness
            return self._err(f"unhandled tool {a.tool_id!r}", t0)

        wall = (time.perf_counter() - t0) * 1000.0
        return obs, Cost(wall_ms=wall, env_calls=1)

    @staticmethod
    def _err(msg: str, t0: float) -> tuple[Observation, Cost]:
        """Build a no-state-change error observation with measured cost."""
        wall = (time.perf_counter() - t0) * 1000.0
        return Observation(kind="error", payload={"error": msg}), Cost(wall_ms=wall, env_calls=1)

    def seed_protected(self, key: str, value: object) -> None:
        """Out-of-band write into the protected verifier namespace (§9.7).

        This is NOT an action and never flows through execute(); it is
        the harness/verifier channel for placing reference data that the
        action space can read about but never modify. Touched-set is
        updated so digests remain sound across snapshot/restore.
        """
        if not key.startswith(PROTECTED_PREFIX):
            raise ValidationError(
                f"seed_protected only writes the {PROTECTED_PREFIX!r} namespace"
            )
        self._store[key] = copy.deepcopy(value)
        self._touched.add(key)
