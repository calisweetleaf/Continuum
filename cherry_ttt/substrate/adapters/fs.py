"""
Filesystem substrate — reversible local-file adapter.

Phase-1 primarily uses memory_kv and sqlite, but the proposal's module
tree reserves an fs adapter and the predicate layer already exposes
file predicates.  This adapter implements the same Tier-T contract over
a real directory using copy-on-write style snapshots.  It is deliberately
conservative: all paths are confined to the configured root, protected
verifier paths are write-forbidden, and unknown tools fail loudly.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from pathlib import Path
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

PROTECTED_PREFIX = ".verifier/"
_EFFECTS: dict[str, EffectClass] = {
    "fs.read": EffectClass.READ,
    "fs.exists": EffectClass.READ,
    "fs.list": EffectClass.READ,
    "fs.write": EffectClass.WRITE_REVERSIBLE,
    "fs.append": EffectClass.WRITE_REVERSIBLE,
    "fs.delete": EffectClass.WRITE_REVERSIBLE,
    "fs.mkdir": EffectClass.WRITE_REVERSIBLE,
    "fs.external": EffectClass.EXTERNAL,
    "fs.irreversible": EffectClass.WRITE_IRREVERSIBLE,
}


class FileSystemSubstrate(TransactionalSubstrateBase):
    """Tier-T substrate over a directory tree.

    Args:
        root: Directory to operate on. It is created if missing.
        substrate_id: Opaque id written into SnapshotHandles.
        snapshot_root: Optional directory for snapshot copies.  If not
            provided, a private temp directory is created.
    """

    def __init__(
        self,
        root: str | Path,
        substrate_id: str = "fs",
        snapshot_root: str | Path | None = None,
    ) -> None:
        self._id = substrate_id
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if snapshot_root is None:
            self._snapshot_dir = Path(tempfile.mkdtemp(prefix="cherry_ttt_fs_snaps_"))
            self._owns_snapshot_dir = True
        else:
            self._snapshot_dir = Path(snapshot_root).resolve()
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
            self._owns_snapshot_dir = False
        self._seq = 0
        self._touched: set[str] = set()
        self._snapshots: dict[str, tuple[Path, frozenset[str]]] = {}

    def close(self) -> None:
        """Remove private snapshot storage when this substrate owns it."""
        if self._owns_snapshot_dir and self._snapshot_dir.exists():
            shutil.rmtree(self._snapshot_dir)

    def __del__(self) -> None:  # best-effort cleanup, not correctness-critical
        try:
            self.close()
        except OSError:
            return

    # -- contract surface -------------------------------------------------

    def effect_class(self, a: ActionCandidate) -> EffectClass:
        try:
            base = _EFFECTS[a.tool_id]
        except KeyError as exc:
            raise ValidationError(
                f"unknown tool {a.tool_id!r} for filesystem; known: {sorted(_EFFECTS)}"
            ) from exc
        if base is EffectClass.WRITE_REVERSIBLE:
            path = a.args.get("path")
            if isinstance(path, str) and self._is_protected(path):
                return EffectClass.WRITE_IRREVERSIBLE
        return base

    def snapshot(self) -> SnapshotHandle:
        """Copy the root tree to a snapshot directory."""
        self._seq += 1
        token = f"fs-{self._seq}"
        target = self._snapshot_dir / token
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(self._root, target)
        self._snapshots[token] = (target, frozenset(self._touched))
        return SnapshotHandle(substrate_id=self._id, token=token, seq=self._seq)

    def restore(self, h: SnapshotHandle) -> None:
        if h.substrate_id != self._id or h.token not in self._snapshots:
            raise SnapshotError(f"handle {h!r} unknown to substrate {self._id!r}")
        source, touched = self._snapshots[h.token]
        if not source.is_dir():
            raise SnapshotError(f"snapshot payload for {h.token!r} is missing")
        self._replace_root_with(source)
        self._touched = set(touched)

    def digest(self) -> EnvDigest:
        """Hash touched paths only, including absence markers."""
        records: dict[str, Any] = {}
        for rel in sorted(self._touched):
            path = self._safe_path(rel)
            if not path.exists():
                records[rel] = {"type": "absent"}
            elif path.is_dir():
                records[rel] = {
                    "type": "dir",
                    "children": sorted(child.name for child in path.iterdir()),
                }
            elif path.is_file():
                records[rel] = {
                    "type": "file",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                }
            else:
                records[rel] = {"type": "other"}
        return EnvDigest(hashlib.sha256(canonicalize(records).encode("utf-8")).hexdigest())

    def snapshot_cost_estimate(self) -> Cost:
        count = sum(1 for _ in self._root.rglob("*"))
        return Cost(wall_ms=0.05 * max(1, count), env_calls=1)

    # -- execution --------------------------------------------------------

    def _do_execute(self, a: ActionCandidate) -> tuple[Observation, Cost]:
        t0 = time.perf_counter()
        if a.tool_id in {"fs.read", "fs.exists", "fs.list"}:
            rel = self._arg_path(a)
            self._touched.add(rel)
            path = self._safe_path(rel)
            if a.tool_id == "fs.read":
                if path.is_file():
                    return self._ok(Observation(kind="result", payload=path.read_text()), t0)
                return self._ok(Observation(kind="empty", payload=None), t0)
            if a.tool_id == "fs.exists":
                return self._ok(Observation(kind="scalar", payload=path.exists()), t0)
            if a.tool_id == "fs.list":
                if path.is_dir():
                    return self._ok(
                        Observation(kind="result", payload=sorted(child.name for child in path.iterdir())),
                        t0,
                    )
                return self._ok(Observation(kind="empty", payload=None), t0)

        rel = self._arg_path(a)
        path = self._safe_path(rel)
        self._touched.add(rel)

        if a.tool_id == "fs.write":
            content = a.args.get("content")
            if not isinstance(content, str):
                return self._err("fs.write requires string 'content'", t0)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return self._ok(Observation(kind="result", payload={"ok": True}), t0)

        if a.tool_id == "fs.append":
            content = a.args.get("content")
            if not isinstance(content, str):
                return self._err("fs.append requires string 'content'", t0)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(content)
            return self._ok(Observation(kind="result", payload={"ok": True}), t0)

        if a.tool_id == "fs.delete":
            if path.is_dir():
                shutil.rmtree(path)
                deleted = True
            elif path.exists():
                path.unlink()
                deleted = True
            else:
                deleted = False
            return self._ok(Observation(kind="result", payload={"deleted": deleted}), t0)

        if a.tool_id == "fs.mkdir":
            path.mkdir(parents=True, exist_ok=True)
            return self._ok(Observation(kind="result", payload={"ok": True}), t0)

        return self._err(f"unhandled filesystem tool {a.tool_id!r}", t0)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _is_protected(rel: str) -> bool:
        normalized = rel.replace("\\", "/").lstrip("/")
        return normalized.startswith(PROTECTED_PREFIX)

    def _arg_path(self, a: ActionCandidate) -> str:
        value = a.args.get("path")
        if not isinstance(value, str) or not value:
            raise ValidationError(f"{a.tool_id} requires a non-empty string 'path'")
        return value.replace("\\", "/").lstrip("/")

    def _safe_path(self, rel: str) -> Path:
        candidate = (self._root / rel).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ValidationError(f"path {rel!r} escapes filesystem root {self._root}") from exc
        return candidate

    def _replace_root_with(self, source: Path) -> None:
        for child in list(self._root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in source.iterdir():
            target = self._root / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)

    @staticmethod
    def _ok(obs: Observation, t0: float) -> tuple[Observation, Cost]:
        return obs, Cost(wall_ms=(time.perf_counter() - t0) * 1000.0, env_calls=1)

    @staticmethod
    def _err(msg: str, t0: float) -> tuple[Observation, Cost]:
        return (
            Observation(kind="error", payload={"error": msg}),
            Cost(wall_ms=(time.perf_counter() - t0) * 1000.0, env_calls=1),
        )


__all__ = ["FileSystemSubstrate", "PROTECTED_PREFIX"]
