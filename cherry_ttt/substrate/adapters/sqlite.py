"""
SQLite substrate — D5's second adapter, SAVEPOINT-native reversibility.

Source: written for cherry_ttt P1; snapshot mechanics are SQLite's own
    SAVEPOINT / ROLLBACK TO (proposal §10.1: "snapshots are native and
    free"), which is precisely why this substrate is phase-1 eligible
    (proposal §9.1 mitigation: only surfaces with native reversibility).
Integrated: 2026-07-05
Purpose: Tier-T substrate over a real database. Touched-table tracking
    uses the sqlite3 authorizer hook — the engine reports writes; nothing
    is inferred by parsing SQL. Digest is a per-table content hash over
    touched tables only (D2), schema plus rows, combined canonically.

Effect classification (leading keyword, conservative):
    SELECT -> READ; INSERT/UPDATE/DELETE/CREATE/DROP/REPLACE/ALTER/WITH
    -> WRITE_REVERSIBLE (atomic inside the open savepoint stack);
    VACUUM/ATTACH/DETACH/PRAGMA -> WRITE_IRREVERSIBLE (escape or cannot
    run in a transaction); transaction-control keywords (BEGIN/COMMIT/
    ROLLBACK/SAVEPOINT/RELEASE) -> EXTERNAL (control-plane escape: the
    snapshot ledger owns the transaction machinery, never the action
    space); unknown -> WRITE_IRREVERSIBLE, conservatively.

Restore semantics vs D2: ROLLBACK TO sp keeps sp defined and releases
    savepoints nested inside it, so restore(h) is valid from any
    descendant of h. Handles on abandoned branches (created after h,
    invalidated by restoring h) raise SnapshotError loudly — they are
    ancestors' siblings, not descendants, and D2 does not owe them.
"""

from __future__ import annotations

import hashlib
import sqlite3
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

_REVERSIBLE_KEYWORDS = frozenset(
    {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "REPLACE", "ALTER", "WITH"}
)
_IRREVERSIBLE_KEYWORDS = frozenset({"VACUUM", "ATTACH", "DETACH", "PRAGMA", "REINDEX"})
PROTECTED_PREFIX = "verifier_"
"""Tables under this prefix form the §9.7 protected namespace (see
_authorize); predicates read them, actions cannot write them."""

_CONTROL_KEYWORDS = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "END"})

_WRITE_OPS = frozenset(
    {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_DROP_INDEX,
    }
)


def _leading_keyword(statement: str) -> str:
    """Return the first SQL keyword, upper-cased, comments stripped."""
    text = statement.lstrip()
    while text.startswith("--") or text.startswith("/*"):
        if text.startswith("--"):
            newline = text.find("\n")
            text = "" if newline < 0 else text[newline + 1 :].lstrip()
        else:
            close = text.find("*/")
            text = "" if close < 0 else text[close + 2 :].lstrip()
    head = text.split(None, 1)[0] if text else ""
    return head.upper().rstrip(";")


class SQLiteSubstrate(TransactionalSubstrateBase):
    """SAVEPOINT-backed Tier-T substrate over sqlite3.

    Source: cherry_ttt P1 (module docstring carries full provenance).

    Args:
        database: Path or ":memory:". Tests pass real temp files or use
            the in-memory engine — both are real databases, never mocks.
    """

    def __init__(self, database: str = ":memory:", substrate_id: str = "sqlite") -> None:
        self._id = substrate_id
        self._conn = sqlite3.connect(database, isolation_level=None)
        self._touched: set[str] = set()
        self._snapshots: dict[str, tuple[int, frozenset[str]]] = {}
        self._seq = 0
        self._live_seqs: set[int] = set()
        self._seeding = False
        self._conn.set_authorizer(self._authorize)

    def _authorize(self, op: int, arg1: Any, arg2: Any, dbname: Any, source: Any) -> int:
        """sqlite3 authorizer: mark tables the engine is about to write,
        and DENY action writes into the protected verifier namespace.

        §9.7 / standing invariant 4: tables prefixed 'verifier_' are the
        protected namespace — the engine itself refuses the write (the
        statement fails at prepare, state untouched), which is stronger
        than any Python-side check. The verifier seeding channel
        (seed_protected) lifts the deny flag for its own writes only.

        Side effect: adds arg1 to the touched set for write-class ops.
        """
        if op in _WRITE_OPS and isinstance(arg1, str) and not arg1.startswith("sqlite_"):
            if arg1.startswith(PROTECTED_PREFIX) and not self._seeding:
                return sqlite3.SQLITE_DENY
            self._touched.add(arg1)
        return sqlite3.SQLITE_OK

    # -- contract surface ---------------------------------------------------

    def effect_class(self, a: ActionCandidate) -> EffectClass:
        """Classify by leading keyword; unknown tools/keywords fail safe."""
        if a.tool_id != "sql.exec":
            raise ValidationError(f"unknown tool {a.tool_id!r} for sqlite; known: ['sql.exec']")
        statement = a.args.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ValidationError("sql.exec requires a non-empty 'statement' string")
        keyword = _leading_keyword(statement)
        if keyword == "SELECT":
            return EffectClass.READ
        if keyword in _REVERSIBLE_KEYWORDS:
            return EffectClass.WRITE_REVERSIBLE
        if keyword in _CONTROL_KEYWORDS:
            return EffectClass.EXTERNAL
        # IRREVERSIBLE keywords and anything unrecognized: fail safe.
        return EffectClass.WRITE_IRREVERSIBLE

    def snapshot(self) -> SnapshotHandle:
        """Open a named SAVEPOINT; capture the touched set alongside it."""
        self._seq += 1
        self._conn.execute(f"SAVEPOINT sp{self._seq}")
        token = f"sp{self._seq}"
        self._snapshots[token] = (self._seq, frozenset(self._touched))
        self._live_seqs.add(self._seq)
        return SnapshotHandle(substrate_id=self._id, token=token, seq=self._seq)

    def restore(self, h: SnapshotHandle) -> None:
        """ROLLBACK TO the named savepoint; restore the captured touched set.

        Raises:
            SnapshotError: Unknown handle, or a handle on an abandoned
                branch (its savepoint was released by an earlier restore
                to an ancestor).
        """
        if h.substrate_id != self._id or h.token not in self._snapshots:
            raise SnapshotError(f"handle {h!r} unknown to substrate {self._id!r}")
        seq, touched = self._snapshots[h.token]
        if seq not in self._live_seqs:
            raise SnapshotError(
                f"handle {h.token} lies on an abandoned branch; current state "
                "is not a descendant of it (D2 owes descendants only)"
            )
        try:
            self._conn.execute(f"ROLLBACK TO sp{seq}")
        except sqlite3.Error as exc:
            raise SnapshotError(f"engine rejected rollback to sp{seq}: {exc}") from exc
        self._live_seqs = {s for s in self._live_seqs if s <= seq}
        self._touched = set(touched)

    def digest(self) -> EnvDigest:
        """Per-table content hash over touched tables only (D2).

        Each touched table contributes sha256(schema SQL + all rows in
        rowid order); absent tables contribute a deterministic marker.
        """
        table_hashes: dict[str, str] = {}
        for table in sorted(self._touched):
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None:
                table_hashes[table] = "absent"
                continue
            hasher = hashlib.sha256((row[0] or "").encode("utf-8"))
            for record in self._conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                cells = [
                    {"__bytes_hex__": cell.hex()} if isinstance(cell, (bytes, bytearray))
                    else cell
                    for cell in record
                ]
                hasher.update(canonicalize(cells).encode("utf-8"))
            table_hashes[table] = hasher.hexdigest()
        combined = canonicalize(table_hashes).encode("utf-8")
        return EnvDigest(hashlib.sha256(combined).hexdigest())

    def snapshot_cost_estimate(self) -> Cost:
        """SAVEPOINT is O(1) in the engine; one env call, negligible wall."""
        return Cost(wall_ms=0.05, env_calls=1)

    # -- gated execution ------------------------------------------------------

    def _do_execute(self, a: ActionCandidate) -> tuple[Observation, Cost]:
        """Execute one already-gated statement atomically.

        SQLite statement failures (missing table, constraint breach) are
        error observations, not exceptions — a failed statement changes
        nothing, and the fuzzer treats it as an ordinary trajectory event.
        """
        t0 = time.perf_counter()
        statement = a.args["statement"]  # validated in effect_class
        try:
            cursor = self._conn.execute(statement)
            rows = cursor.fetchall()
            if rows:
                obs = Observation(kind="result", payload=[list(r) for r in rows])
            elif cursor.rowcount >= 0:
                obs = Observation(kind="scalar", payload=cursor.rowcount)
            else:
                obs = Observation(kind="empty", payload=None)
        except sqlite3.Error as exc:
            obs = Observation(kind="error", payload={"error": str(exc)})
        wall = (time.perf_counter() - t0) * 1000.0
        return obs, Cost(wall_ms=wall, env_calls=1)


    def seed_protected(self, statement: str) -> None:
        """Out-of-band DDL/DML into the protected verifier namespace (§9.7).

        NOT an action; the harness/verifier channel. Temporarily lifts the
        authorizer deny for this call only; the statement must reference
        only verifier-namespace tables (the authorizer still tracks touch).
        """
        self._seeding = True
        try:
            self._conn.execute(statement)
        finally:
            self._seeding = False
    def close(self) -> None:
        """Release the connection; the substrate is finished after this."""
        self._conn.close()
